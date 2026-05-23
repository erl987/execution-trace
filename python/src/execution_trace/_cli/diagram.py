"""etrace-diagram — generate a zoomable HTML timing diagram from a trace CSV.

Input CSV columns (as written by ``etrace-decode`` or :func:`write_tracing_csv`):

    name         Task or ISR name  (string)
    type         ``"task"``, ``"isr"``, or ``"marker"``  (case-insensitive)
    start_us     Activation timestamp in µs  (float ≥ 0)
    end_us       Completion timestamp in µs  (float ≥ start_us)
    priority     RTIC task priority (int, optional — 0 if absent)
    deadline_us  Absolute deadline in µs  (float, optional)
    value        Optional u32 payload  (int, optional)

Usage::

    etrace-diagram
    etrace-diagram trace_latest.csv
    etrace-diagram trace_latest.csv --output diagram.html --title "My Trace"
"""

import argparse
import sys
from pathlib import Path
from typing import NoReturn

from execution_trace.diagram import DiagramError, generate_diagram


def _die(msg: str) -> NoReturn:
    print(f"[error] {msg}", file=sys.stderr)
    sys.exit(1)


def _warn(msg: str) -> None:
    print(f"[warning] {msg}", file=sys.stderr)


def main() -> None:
    """Entry point for the ``etrace-diagram`` console script."""
    ap = argparse.ArgumentParser(
        description="Generate a zoomable embedded-system task/ISR time trace diagram.",
    )
    ap.add_argument(
        "csv",
        nargs="?",
        default="trace_latest.csv",
        help="Tracing data CSV  (default: trace_latest.csv)",
    )
    ap.add_argument(
        "--output",
        default="diagram.html",
        metavar="HTML",
        help="Output HTML file  (default: diagram.html)",
    )
    ap.add_argument(
        "--title",
        default="Task Time Trace Diagram",
        help="Diagram title",
    )
    args = ap.parse_args()

    try:
        result = generate_diagram(
            csv_path=Path(args.csv),
            output_path=Path(args.output),
            title=args.title,
            warn=_warn,
            die=_die,
        )
    except DiagramError as exc:
        _die(str(exc))

    print(
        f"Written → {result.output_path}  "
        f"({result.n_events} events, {result.n_lanes} lanes, "
        f"{result.n_missed} deadline miss{'es' if result.n_missed != 1 else ''})"
    )


if __name__ == "__main__":
    main()
