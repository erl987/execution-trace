"""Zoomable Bokeh timing diagram for embedded-system task/ISR execution traces.

Reads a CSV produced by :func:`execution_trace.decode.write_tracing_csv` (or any
compatible source) and renders an interactive HTML diagram.

CSV input format
----------------

Required columns:

+------------+---------------------------------------+
| Column     | Description                           |
+============+=======================================+
| ``name``   | span / marker name (string)           |
+------------+---------------------------------------+
| ``type``   | ``task``, ``isr``, or ``marker``      |
+------------+---------------------------------------+
| ``start_us``| activation timestamp in µs (float)  |
+------------+---------------------------------------+
| ``end_us`` | completion timestamp in µs (float);   |
|            | equal to ``start_us`` for markers     |
+------------+---------------------------------------+

Optional columns:

+--------------+--------------------------------------------------+
| Column       | Description                                      |
+==============+==================================================+
| ``priority`` | scheduler priority; higher = more urgent         |
+--------------+--------------------------------------------------+
| ``deadline_us``| absolute deadline in µs; enables deadline viz  |
+--------------+--------------------------------------------------+
| ``value``    | numeric marker payload shown in tooltip          |
+--------------+--------------------------------------------------+

Typical usage::

    from pathlib import Path
    from execution_trace.diagram import generate_diagram

    result = generate_diagram(
        csv_path=Path("trace_latest.csv"),
        output_path=Path("diagram.html"),
    )
    print(f"Written {result.output_path} — {result.n_events} events")
"""

import logging
import math
import os
from pathlib import Path
from typing import Callable, NamedTuple, NoReturn, SupportsFloat, cast

try:
    import pandas as pd
    from bokeh.layouts import column as bk_column
    from bokeh.models import (
        BoxZoomTool,
        ColumnDataSource,
        CustomJS,
        CustomJSTickFormatter,
        FixedTicker,
        HoverTool,
        PanTool,
        Range1d,
        RangeTool,
        ResetTool,
        SaveTool,
        Segment,
        WheelZoomTool,
    )
    from bokeh.models import Div, LabelSet, Toggle, UIElement  # type: ignore[attr-defined]
    from bokeh.plotting import figure, output_file, save
except ImportError as _err:
    raise ImportError(
        "execution_trace.diagram requires the [diagram] optional dependencies. "
        "Install them with:  pip install embedded-etrace[diagram]"
    ) from _err

logger = logging.getLogger(__name__)

# ── Colour palettes ────────────────────────────────────────────────────────────
_TASK_COLORS: list[str] = ["#4878CF", "#6ACC65", "#B47CC7", "#77BEDB", "#C4AD66"]
_ISR_COLORS: list[str] = ["#E07B54", "#F0A30A", "#E05C8A", "#C47A2E", "#D4782A"]
_MARKER_COLOR: str = "#9B59B6"

# ── Geometry constants ─────────────────────────────────────────────────────────
_HALF_H: float = 0.325   # half of bar height (65 % of unit lane)
_NORMAL_LW: float = 0.7  # border line-width, normal bar
_MISSED_LW: float = 2.5  # border line-width, missed-deadline bar
_DL_OFFSET: float = 0.10  # triangle offset above lane top
_MARKER_H: float = 0.28   # half-height of marker tick line


class DiagramError(ValueError):
    """Raised by library functions when input data is invalid."""


class DiagramResult(NamedTuple):
    """Summary counts returned by :func:`generate_diagram`.

    Attributes:
        output_path: Path of the written HTML file.
        n_events: Total number of span and marker rows in the diagram.
        n_lanes: Number of distinct task / ISR lanes.
        n_missed: Number of spans that exceeded their deadline.
    """

    output_path: Path
    n_events: int
    n_lanes: int
    n_missed: int


def _default_warn(msg: str) -> None:
    logger.warning("[warning] %s", msg)


def _default_die(msg: str) -> NoReturn:
    raise DiagramError(msg)


# ── Data loading and validation ────────────────────────────────────────────────

def load_traces(
    csv_path: Path,
    *,
    warn: Callable[[str], None] = _default_warn,
    die: Callable[[str], NoReturn] = _default_die,
) -> "pd.DataFrame":
    """Load and validate a trace CSV into a :class:`pandas.DataFrame`.

    Rows with non-numeric timestamps are dropped with a warning. Rows where
    ``end_us <= start_us`` (for spans) are fatal errors. Marker rows
    (``type == "marker"``) are exempt from the end > start check.

    Args:
        csv_path: Path to the input CSV file.
        warn: Callback for non-fatal issues; defaults to :mod:`logging`.
        die: Callback for fatal issues; defaults to raising :exc:`DiagramError`.

    Returns:
        Validated DataFrame with computed ``duration_us``, ``effective_deadline_us``,
        and ``missed`` columns added.

    Raises:
        DiagramError: If the file is missing, required columns are absent, or any
            span has ``end_us <= start_us``.
    """
    def _coerce_numeric(df: "pd.DataFrame", col: str, *, required: bool = False) -> "pd.DataFrame":
        orig_na = df[col].isna()
        df[col] = pd.to_numeric(df[col], errors="coerce")
        bad_conv = df[col].isna() & ~orig_na
        bad_empty = orig_na if required else pd.Series(False, index=df.index)
        for csv_row in df.index[bad_conv]:
            warn(f"row {csv_row + 2}: non-numeric value in '{col}', row dropped")
        for csv_row in df.index[bad_empty]:
            warn(f"row {csv_row + 2}: missing required value in '{col}', row dropped")
        return df[~(bad_conv | bad_empty)].copy()

    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError as exc:
        raise DiagramError(f"file not found: {csv_path}") from exc

    for col in ("name", "type", "start_us", "end_us"):
        if col not in df.columns:
            die(f"required column missing from CSV: '{col}'")

    if len(df) == 0:
        die("tracing CSV contains no data rows")

    df["type"] = df["type"].str.strip().str.lower()

    df = _coerce_numeric(df, "start_us", required=True)
    df = _coerce_numeric(df, "end_us", required=True)

    if "priority" in df.columns:
        df = _coerce_numeric(df, "priority")
        df["priority"] = df["priority"].fillna(0).astype(int)
    else:
        df["priority"] = 0

    if "deadline_us" in df.columns:
        df = _coerce_numeric(df, "deadline_us")
    else:
        df["deadline_us"] = float("nan")

    if "value" in df.columns:
        df = _coerce_numeric(df, "value")
    else:
        df["value"] = float("nan")

    if len(df) == 0:
        die("tracing CSV contains no data rows")

    if "interrupted" in df.columns:
        df["interrupted"] = (
            pd.to_numeric(df["interrupted"], errors="coerce").fillna(0).astype(bool)
        )
    else:
        df["interrupted"] = False

    # Markers have start_us == end_us == timestamp_us, and a gap row is a band
    # rather than an interval that ran; only validate spans.
    spans = df[df["type"].isin(["task", "isr"])]
    # An interrupted span is cut at the last frame before a gap, which can be the
    # very frame that opened it — so it may be zero-length. Its end is a lower
    # bound on when it finished, not a measurement, and zero is a true one.
    bad = spans[
        (spans["end_us"] < spans["start_us"])
        | ((spans["end_us"] == spans["start_us"]) & ~spans["interrupted"])
    ]
    if not bad.empty:
        csv_row = bad.index[0] + 2
        die(f"row {csv_row}: end_us must be > start_us")

    for name, grp in df.groupby("name", sort=False):
        if grp["type"].nunique() > 1:
            die(f"name '{name}' has conflicting types")

    df["duration_us"] = df["end_us"] - df["start_us"]
    df["effective_deadline_us"] = df["deadline_us"]
    df["missed"] = (
        df["effective_deadline_us"].notna()
        & (df["end_us"] > df["effective_deadline_us"])
        # An interrupted span's end is unknown and only a lower bound, so it
        # cannot be said to have overrun (§5.9).
        & ~df["interrupted"]
    )
    return df.reset_index(drop=True)


# ── Lane assignment ────────────────────────────────────────────────────────────

def assign_lanes(df: "pd.DataFrame") -> "tuple[pd.DataFrame, list[str]]":
    """Assign a vertical lane index to each row for diagram rendering.

    ISR lanes sit above task lanes. Within each group, names are sorted by
    priority descending (highest priority at top), then by first appearance
    for ties. Lane numbers are zero-based from the bottom of the chart.

    Marker rows are assigned to the lane of the highest-priority span that
    contains the marker's timestamp. Uncontained markers receive lane ``-1``
    (rendered below all spans). Gap rows span every lane and are carried through
    with lane ``-1`` as well.

    Args:
        df: Validated DataFrame from :func:`load_traces` (spans and markers).

    Returns:
        A ``(augmented_df, lane_names)`` pair where *augmented_df* has a
        ``"lane"`` column added and *lane_names* is the top-to-bottom ordered
        list of span names.
    """
    spans = df[df["type"].isin(["task", "isr"])].copy()
    markers = df[df["type"] == "marker"].copy()
    gaps = df[df["type"] == "gap"].copy()

    name_type = spans.groupby("name", sort=False)["type"].first()
    name_priority = spans.groupby("name", sort=False)["priority"].first()
    first_seen = {name: i for i, name in enumerate(spans["name"].unique())}

    def sort_key(name: str) -> tuple[int, int]:
        return -int(name_priority[name]), first_seen[name]

    isrs = sorted(name_type[name_type == "isr"].index, key=sort_key)
    tasks = sorted(name_type[name_type == "task"].index, key=sort_key)
    top_to_bottom = isrs + tasks

    n = len(top_to_bottom)
    lane_map = {name: n - 1 - i for i, name in enumerate(top_to_bottom)}

    spans = spans.copy()
    spans["lane"] = spans["name"].map(lane_map)

    if not markers.empty:
        def _infer_lane(ts: float) -> int:
            containing = spans[(spans["start_us"] <= ts) & (spans["end_us"] >= ts)]
            if containing.empty:
                return -1
            idx = containing["priority"].idxmax()
            lane = cast(SupportsFloat, containing.loc[idx, "lane"])
            return int(float(lane))

        markers = markers.copy()
        markers["lane"] = markers["start_us"].apply(_infer_lane)

    if not gaps.empty:
        gaps["lane"] = -1
    parts = [part for part in (spans, markers, gaps) if not part.empty]
    df = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    return df, top_to_bottom


# ── Colour assignment ──────────────────────────────────────────────────────────

def build_color_map(lane_names: list[str], df: "pd.DataFrame") -> dict[str, str]:
    """Build a ``{name: hex_color}`` mapping for span rendering.

    ISR lanes receive warm colours; task lanes receive cool colours.

    Args:
        lane_names: Ordered list of span names (from :func:`assign_lanes`).
        df: Augmented DataFrame with a ``"type"`` column.

    Returns:
        Dict mapping each name to a hex colour string.
    """
    spans = df[df["type"].isin(["task", "isr"])]
    name_type = spans.groupby("name", sort=False)["type"].first()
    cmap: dict[str, str] = {}
    isr_i = task_i = 0
    for name in lane_names:
        if name_type[name] == "isr":
            cmap[name] = _ISR_COLORS[isr_i % len(_ISR_COLORS)]
            isr_i += 1
        else:
            cmap[name] = _TASK_COLORS[task_i % len(_TASK_COLORS)]
            task_i += 1
    return cmap


# ── Figure construction ────────────────────────────────────────────────────────

def _fmt_us(v: float) -> str:
    return "—" if pd.isna(v) else f"{v:,.1f} µs"


def _fmt_ms(v: float) -> str:
    return "—" if pd.isna(v) else f"{v / 1_000:,.3f} ms"


def _bar_source(
    sub: "pd.DataFrame", colors: dict[str, str], min_width: float = 0.0
) -> ColumnDataSource:
    # An interrupted span can be zero-length; without a floor it would vanish,
    # which is the opposite of what §5.9 is for.
    right = sub["end_us"] if min_width <= 0 else (
        sub[["end_us", "start_us"]].max(axis=1).combine(
            sub["start_us"] + min_width, max
        )
    )
    return ColumnDataSource({
        "left": sub["start_us"].tolist(),
        "right": right.tolist(),
        "top": (sub["lane"] + _HALF_H).tolist(),
        "bottom": (sub["lane"] - _HALF_H).tolist(),
        "name": sub["name"].tolist(),
        "kind": sub["type"].tolist(),
        "start_fmt": sub["start_us"].apply(_fmt_ms).tolist(),
        "end_fmt": sub["end_us"].apply(_fmt_ms).tolist(),
        "duration_fmt": sub["duration_us"].apply(_fmt_us).tolist(),
        "deadline_fmt": sub["effective_deadline_us"].apply(_fmt_us).tolist(),
        "missed_str": ["True" if m else "False" for m in sub["missed"]],
        "color": [colors[n] for n in sub["name"]],
    })


def build_figure(
    df: "pd.DataFrame",
    lane_names: list[str],
    colors: dict[str, str],
    title: str,
) -> "tuple[figure, Toggle | None]":
    """Construct the main Bokeh timing diagram figure.

    Args:
        df: Augmented DataFrame with ``"lane"`` column (from :func:`assign_lanes`).
        lane_names: Top-to-bottom ordered list of span names.
        colors: Colour mapping from :func:`build_color_map`.
        title: Diagram title string.

    Returns:
        A ``(figure, marker_label_toggle)`` pair. The toggle is ``None`` when
        no markers are present.
    """
    spans = df[df["type"].isin(["task", "isr"])]
    markers = df[df["type"] == "marker"]
    gaps = df[df["type"] == "gap"]

    n = len(lane_names)
    x_lo = float(spans["start_us"].min())
    x_hi = float(spans["end_us"].max())
    x_pad = max(1.0, (x_hi - x_lo) * 0.025)

    p = figure(  # type: ignore[call-arg]
        width=1400,
        height=max(300, 90 + n * 54),
        x_range=Range1d(start=x_lo - x_pad, end=x_hi + x_pad),
        y_range=Range1d(start=-0.6, end=n - 0.4),
        tools="",
        toolbar_location="above",
        title=title,
    )
    p.title.text_font_size = "14pt"  # type: ignore[union-attr]
    p.background_fill_color = "#fafafa"
    p.outline_line_color = None

    for i in range(n):
        p.quad(
            left=x_lo - x_pad, right=x_hi + x_pad,
            bottom=i - 0.5, top=i + 0.5,
            fill_color="#e8eaf6" if i % 2 == 0 else "#f5f5f5",
            fill_alpha=0.55, line_color=None,
        )

    # Gap bands go down before the bars so the spans either side stay legible on
    # top of them. A trace that is missing data has to look like one (§5.9).
    if not gaps.empty:
        # A gap of zero width would be invisible; give it a hairline so the
        # annotation still has something to sit on.
        min_w = max((x_hi - x_lo) * 0.0008, 1.0)
        widths = (gaps["end_us"] - gaps["start_us"]).clip(lower=min_w)
        gap_src = ColumnDataSource({
            "left": gaps["start_us"].tolist(),
            "right": (gaps["start_us"] + widths).tolist(),
            "bottom": [-0.6] * len(gaps),
            "top": [n - 0.4] * len(gaps),
            "frames_lost": [
                int(v) if pd.notna(v) else 0 for v in gaps["value"]
            ],
            "start_us": gaps["start_us"].tolist(),
        })
        gap_renderer = p.quad(
            left="left", right="right", bottom="bottom", top="top",
            source=gap_src,
            fill_color="#b71c1c", fill_alpha=0.13,
            hatch_pattern="/", hatch_color="#b71c1c", hatch_alpha=0.45, hatch_scale=12,
            line_color="#b71c1c", line_width=1.0, line_alpha=0.55,
            legend_label="Trace gap",
        )
        p.add_tools(HoverTool(
            renderers=[gap_renderer],
            tooltips=[("trace gap", "@frames_lost frames lost"), ("at", "@start_us{0,0.0} µs")],
        ))
        labels = LabelSet(
            x="start_us", y=n - 0.45, text="frames_lost",
            source=gap_src, text_color="#b71c1c", text_font_size="9pt",
            x_offset=3, y_offset=-12,
        )
        p.add_layout(labels)

    span_renderers = []
    marker_renderers = []

    def _add_bars(
        mask: "pd.Series[bool]",
        hatch: str | None,
        line_color: str,
        line_width: float,
        legend_label: str,
        min_width: float = 0.0,
    ) -> None:
        sub = spans[mask]
        if sub.empty:
            return
        src = _bar_source(sub, colors, min_width=min_width)
        hatch_kw = (
            dict(hatch_pattern=hatch, hatch_color="white", hatch_alpha=0.40, hatch_scale=9)
            if hatch is not None else {}
        )
        r = p.quad(
            left="left", right="right", top="top", bottom="bottom",
            fill_color="color",
            line_color=line_color, line_width=line_width,
            source=src, legend_label=legend_label,
            **hatch_kw,  # type: ignore[arg-type]
        )
        span_renderers.append(r)

    cut = spans["interrupted"]
    _add_bars((spans["type"] == "task") & ~spans["missed"] & ~cut,
              None, "#555555", _NORMAL_LW, "Task")
    _add_bars((spans["type"] == "isr") & ~spans["missed"] & ~cut,
              "/", "#555555", _NORMAL_LW, "ISR")
    _add_bars((spans["type"] == "task") & spans["missed"] & ~cut,
              None, "crimson", _MISSED_LW, "Missed deadline")
    _add_bars((spans["type"] == "isr") & spans["missed"] & ~cut,
              "/", "crimson", _MISSED_LW, "ISR — missed deadline")
    # Cut short by a gap: the end is a lower bound, not a measurement, so it is
    # drawn open-ended rather than as a span of that length.
    _add_bars(
        cut, "x", "#b71c1c", _MISSED_LW, "Interrupted by gap",
        min_width=max((x_hi - x_lo) * 0.0008, 1.0),
    )

    dl = spans[spans["effective_deadline_us"].notna()].copy()
    if not dl.empty:
        dl_colors = ["crimson" if m else "#ff6600" for m in dl["missed"]]
        p.segment(
            x0=dl["effective_deadline_us"].tolist(),
            x1=dl["effective_deadline_us"].tolist(),
            y0=(dl["lane"] - _HALF_H).tolist(),
            y1=(dl["lane"] + _HALF_H).tolist(),
            color=dl_colors, line_dash="dashed", line_width=1.5, alpha=0.85,
        )
        p.scatter(
            x=dl["effective_deadline_us"].tolist(),
            y=(dl["lane"] + _HALF_H + _DL_OFFSET).tolist(),
            marker="inverted_triangle", size=10,
            color=dl_colors, alpha=0.90,
            legend_label="Deadline marker",
        )

    marker_label_toggle = None
    if not markers.empty:
        visible = markers[markers["lane"] >= 0]
        if not visible.empty:
            value_strs = [
                f" ={int(v)}" if pd.notna(v) else ""
                for v in visible["value"]
            ]
            labels = [f"{nm}{v}" for nm, v in zip(visible["name"], value_strs)]
            marker_src = ColumnDataSource({
                "x0": visible["start_us"].tolist(),
                "x1": visible["start_us"].tolist(),
                "y0": (visible["lane"] - _MARKER_H).tolist(),
                "y1": (visible["lane"] + _MARKER_H).tolist(),
                "x_lbl": visible["start_us"].tolist(),
                "y_lbl": (visible["lane"] + _MARKER_H + 0.05).tolist(),
                "label": labels,
                "name": visible["name"].tolist(),
                "ts_fmt": visible["start_us"].apply(_fmt_ms).tolist(),
                "value_str": [str(int(v)) if pd.notna(v) else "—" for v in visible["value"]],
            })
            for _ in range(2):  # two renderers so HoverTool activates on the glyph
                seg = Segment(
                    x0="x0", y0="y0", x1="x1", y1="y1",
                    line_color=_MARKER_COLOR, line_width=2.0, line_alpha=0.85,
                )
                marker_renderers.append(p.add_glyph(marker_src, seg))

            marker_labels = LabelSet(
                x="x_lbl", y="y_lbl", text="label",
                source=marker_src,
                text_font_size="8pt", text_color=_MARKER_COLOR,
                text_align="left", text_baseline="bottom",
                angle=math.pi / 4,
            )
            p.add_layout(marker_labels)

            marker_label_toggle = Toggle(
                label="Marker labels", active=True,
                button_type="default", width=140,
            )
            marker_label_toggle.js_on_change("active", CustomJS(
                args={"labels": marker_labels},
                code="labels.visible = cb_obj.active;",
            ))

    p.xaxis.axis_label = "Time (ms)"
    p.xaxis.formatter = CustomJSTickFormatter(
        code="return (tick / 1000).toLocaleString(undefined, {maximumFractionDigits: 3}) + ' ms';"
    )
    p.xaxis.minor_tick_line_color = None

    name_priority = spans.groupby("name")["priority"].first()
    p.yaxis.ticker = FixedTicker(ticks=list(range(n)))
    p.yaxis.major_label_overrides = {
        n - 1 - i: f"{name}  P{name_priority[name]}" for i, name in enumerate(lane_names)
    }
    p.yaxis.axis_label = ""

    p.xgrid.grid_line_alpha = 0.25
    p.xgrid.grid_line_dash = "dotted"
    p.ygrid.ticker = FixedTicker(ticks=[i + 0.5 for i in range(n - 1)])
    p.ygrid.grid_line_alpha = 0.40
    p.ygrid.grid_line_dash = "dotted"

    span_hover = HoverTool(
        renderers=span_renderers,  # type: ignore[arg-type]
        tooltips=[
            ("", "<b>@name</b> (@kind)"),
            ("Start", "@start_fmt"),
            ("End", "@end_fmt"),
            ("Duration", "@duration_fmt"),
            ("Deadline", "@deadline_fmt"),
            ("Missed", "@missed_str"),
        ],
    )

    wheel = WheelZoomTool(dimensions="width")
    box_zoom = BoxZoomTool()
    pan = PanTool()
    reset = ResetTool()
    save_tool = SaveTool()
    tools = [wheel, box_zoom, pan, reset, save_tool, span_hover]
    if marker_renderers:
        tools.append(HoverTool(
            renderers=marker_renderers,  # type: ignore[arg-type]
            tooltips=[
                ("", "<b>@name</b> (marker)"),
                ("Time", "@ts_fmt"),
                ("Value", "@value_str"),
            ],
        ))

    p.add_tools(*tools)
    p.toolbar.active_scroll = wheel
    p.toolbar.active_drag = pan

    p.legend.location = "top_right"
    p.legend.click_policy = "hide"
    p.legend.background_fill_alpha = 0.85

    return p, marker_label_toggle


# ── Overview navigator strip ───────────────────────────────────────────────────

def build_overview(
    df: "pd.DataFrame",
    colors: dict[str, str],
    main_p: "figure",
) -> "figure":
    """Build an 80 px overview strip that mirrors the main figure's viewport.

    A navy RangeTool overlay can be dragged left/right to scroll the main figure.

    Args:
        df: Augmented DataFrame (must include ``"lane"`` column).
        colors: Colour mapping from :func:`build_color_map`.
        main_p: The main timing diagram figure (its x-range is linked).

    Returns:
        A small Bokeh figure suitable for stacking below the main figure.
    """
    spans = df[df["type"].isin(["task", "isr"])]
    n = int(spans["lane"].max()) + 1
    x_lo = float(spans["start_us"].min())
    x_hi = float(spans["end_us"].max())
    x_pad = max(1.0, (x_hi - x_lo) * 0.025)

    ov = figure(  # type: ignore[call-arg]
        width=main_p.width,
        height=80,
        x_range=Range1d(start=x_lo - x_pad, end=x_hi + x_pad),
        y_range=Range1d(start=-0.6, end=n - 0.4),
        toolbar_location=None,
        y_axis_type=None,
    )
    ov.xaxis.formatter = CustomJSTickFormatter(
        code="return (tick / 1000).toLocaleString(undefined, {maximumFractionDigits: 3}) + ' ms';"
    )
    ov.xaxis.minor_tick_line_color = None
    ov.background_fill_color = "#f0f0f0"
    ov.outline_line_color = "#cccccc"
    ov.xgrid.grid_line_color = None
    ov.ygrid.grid_line_color = None

    half_h = 0.38
    norm = spans[~spans["missed"]]
    if not norm.empty:
        ov.quad(
            left=norm["start_us"].tolist(), right=norm["end_us"].tolist(),
            top=(norm["lane"] + half_h).tolist(),
            bottom=(norm["lane"] - half_h).tolist(),
            fill_color=[colors[nm] for nm in norm["name"]],
            line_color=None,
        )
    miss = spans[spans["missed"]]
    if not miss.empty:
        ov.quad(
            left=miss["start_us"].tolist(), right=miss["end_us"].tolist(),
            top=(miss["lane"] + half_h).tolist(),
            bottom=(miss["lane"] - half_h).tolist(),
            fill_color=[colors[nm] for nm in miss["name"]],
            line_color="crimson", line_width=1.5,
        )

    rt = RangeTool(x_range=main_p.x_range)
    rt.overlay.fill_color = "navy"
    rt.overlay.fill_alpha = 0.20
    ov.add_tools(rt)

    return ov


# ── Summary bar ────────────────────────────────────────────────────────────────

def build_summary_div(df: "pd.DataFrame") -> Div:
    """Build a one-line HTML summary showing event counts and missed-deadline rate.

    Args:
        df: Augmented DataFrame (must include ``"missed"`` and
            ``"effective_deadline_us"`` columns).

    Returns:
        A Bokeh :class:`~bokeh.models.Div` suitable for placing above the figure.
    """
    spans = df[df["type"].isin(["task", "isr"])]
    n_total = len(spans)
    n_with_dl = int(spans["effective_deadline_us"].notna().sum())
    n_missed = int(spans["missed"].sum())
    pct = f"{100 * n_missed / n_with_dl:.0f}%" if n_with_dl else "—"
    missed_style = "color:crimson" if n_missed > 0 else ""
    html = (
        "<p style='font-family:monospace;font-size:12px;color:#444;margin:4px 0'>"
        f"Events: <b>{n_total}</b>"
        " &nbsp;|&nbsp; "
        f"With deadline: <b>{n_with_dl}</b>"
        " &nbsp;|&nbsp; "
        f"Missed: <b style='{missed_style}'>{n_missed}</b> ({pct})"
        "</p>"
    )
    return Div(text=html)


# ── Top-level pipeline ─────────────────────────────────────────────────────────

def generate_diagram(
    csv_path: Path,
    output_path: Path,
    *,
    title: str = "Task Time Trace Diagram",
    warn: Callable[[str], None] = _default_warn,
    die: Callable[[str], NoReturn] = _default_die,
) -> DiagramResult:
    """Load a trace CSV, build a Bokeh figure, and write an interactive HTML file.

    Args:
        csv_path: Path to the input CSV (see module docstring for format).
        output_path: Destination for the generated HTML file. Parent directories
            are created if needed.
        title: Title string shown at the top of the diagram.
        warn: Callback for non-fatal data issues (e.g. invalid rows).
            Defaults to :mod:`logging` warnings.
        die: Callback for fatal data issues. Defaults to raising
            :exc:`DiagramError`. The CLI passes ``sys.exit`` here.

    Returns:
        A :class:`DiagramResult` with the output path and event/lane/miss counts.

    Raises:
        DiagramError: If the CSV is missing or invalid (when using the default *die*).
    """
    os.makedirs(output_path.parent, exist_ok=True)

    df = load_traces(csv_path, warn=warn, die=die)
    df, lane_names = assign_lanes(df)
    colors = build_color_map(lane_names, df)
    p, marker_label_toggle = build_figure(df, lane_names, colors, title)
    overview = build_overview(df, colors, p)
    summary = build_summary_div(df)

    layout_items: list[UIElement] = [summary]
    if marker_label_toggle is not None:
        layout_items.append(marker_label_toggle)
    layout_items.extend([p, overview])

    output_file(str(output_path), title=title)
    save(bk_column(*layout_items))

    return DiagramResult(
        output_path=output_path,
        n_events=len(df),
        n_lanes=len(lane_names),
        n_missed=int(df["missed"].sum()),
    )
