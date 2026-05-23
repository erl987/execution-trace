"""Tests for diagram.py — load_traces, assign_lanes, generate_diagram."""

import io
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# load_traces
# ---------------------------------------------------------------------------

class TestLoadTraces:
    def test_rejects_missing_file(self, tmp_path):
        pd = pytest.importorskip("pandas")
        from execution_trace.diagram import DiagramError, load_traces

        with pytest.raises(DiagramError, match="not found"):
            load_traces(tmp_path / "nonexistent.csv")

    def test_rejects_empty_csv(self, tmp_path):
        pytest.importorskip("pandas")
        from execution_trace.diagram import DiagramError, load_traces

        p = tmp_path / "empty.csv"
        p.write_text("name,type,start_us,end_us,priority,deadline_us,value\n")
        with pytest.raises(DiagramError, match="[Ee]mpty|no.*rows|0 rows"):
            load_traces(p)

    def test_rejects_missing_required_column(self, tmp_path):
        pytest.importorskip("pandas")
        from execution_trace.diagram import DiagramError, load_traces

        p = tmp_path / "missing_col.csv"
        # 'end_us' is missing
        p.write_text("name,type,start_us,priority\nmain_task,task,1000.0,4\n")
        with pytest.raises(DiagramError, match="[Mm]issing|column"):
            load_traces(p)

    def test_accepts_valid_csv(self, sample_csv_path):
        pytest.importorskip("pandas")
        from execution_trace.diagram import load_traces

        df = load_traces(sample_csv_path)
        assert len(df) == 3  # 2 spans + 1 marker

    def test_accepts_marker_rows_start_eq_end(self, tmp_path):
        pytest.importorskip("pandas")
        from execution_trace.diagram import load_traces

        p = tmp_path / "marker.csv"
        p.write_text(
            "name,type,start_us,end_us,priority,deadline_us,value\n"
            "tick,marker,1500.0,1500.0,0,,42\n"
        )
        df = load_traces(p)
        assert len(df) == 1

    def test_rejects_end_before_start(self, tmp_path):
        pytest.importorskip("pandas")
        from execution_trace.diagram import DiagramError, load_traces

        p = tmp_path / "bad_span.csv"
        p.write_text(
            "name,type,start_us,end_us,priority,deadline_us,value\n"
            "t,task,2000.0,1000.0,4,,\n"
        )
        with pytest.raises(DiagramError):
            load_traces(p)


# ---------------------------------------------------------------------------
# assign_lanes
# ---------------------------------------------------------------------------

class TestAssignLanes:
    def _df(self, rows):
        pd = pytest.importorskip("pandas")
        return pd.DataFrame(rows, columns=["name", "type", "start_us", "end_us", "priority"])

    def test_isrs_above_tasks(self):
        pytest.importorskip("pandas")
        from execution_trace.diagram import assign_lanes

        df = self._df([
            ("task_a", "task", 0.0, 1.0, 1),
            ("isr_b",  "isr",  2.0, 3.0, 8),
        ])
        df, _ = assign_lanes(df)
        isr_lane = df.loc[df["name"] == "isr_b", "lane"].iloc[0]
        task_lane = df.loc[df["name"] == "task_a", "lane"].iloc[0]
        assert isr_lane > task_lane

    def test_sorted_by_priority_descending(self):
        pytest.importorskip("pandas")
        from execution_trace.diagram import assign_lanes

        df = self._df([
            ("low",  "task", 0.0, 1.0, 1),
            ("high", "task", 0.0, 1.0, 9),
            ("mid",  "task", 0.0, 1.0, 4),
        ])
        df, _ = assign_lanes(df)
        lane_map = df.set_index("name")["lane"].to_dict()
        assert lane_map["high"] > lane_map["mid"] > lane_map["low"]

    def test_same_name_gets_same_lane(self):
        pytest.importorskip("pandas")
        from execution_trace.diagram import assign_lanes

        df = self._df([
            ("t", "task", 0.0, 1.0, 4),
            ("t", "task", 2.0, 3.0, 4),
        ])
        df, _ = assign_lanes(df)
        lanes = df.loc[df["name"] == "t", "lane"].unique()
        assert len(lanes) == 1


# ---------------------------------------------------------------------------
# generate_diagram
# ---------------------------------------------------------------------------

class TestGenerateDiagram:
    def test_writes_html_file(self, sample_csv_path, tmp_path):
        pytest.importorskip("bokeh")
        from execution_trace.diagram import generate_diagram

        out = tmp_path / "diagram.html"
        result = generate_diagram(csv_path=sample_csv_path, output_path=out)
        assert out.exists()
        assert out.stat().st_size > 0
        assert result.output_path == out

    def test_returns_correct_event_count(self, sample_csv_path, tmp_path):
        pytest.importorskip("bokeh")
        from execution_trace.diagram import generate_diagram

        out = tmp_path / "diagram.html"
        result = generate_diagram(csv_path=sample_csv_path, output_path=out)
        # sample_csv_path has 3 rows (2 spans + 1 marker)
        assert result.n_events == 3

    def test_returns_correct_lane_count(self, sample_csv_path, tmp_path):
        pytest.importorskip("bokeh")
        from execution_trace.diagram import generate_diagram

        out = tmp_path / "diagram.html"
        result = generate_diagram(csv_path=sample_csv_path, output_path=out)
        # main_task (task) and gyro_isr (isr) → 2 distinct lanes
        assert result.n_lanes == 2

    def test_no_deadline_means_zero_missed(self, sample_csv_path, tmp_path):
        pytest.importorskip("bokeh")
        from execution_trace.diagram import generate_diagram

        out = tmp_path / "diagram.html"
        result = generate_diagram(csv_path=sample_csv_path, output_path=out)
        assert result.n_missed == 0

    def test_deadline_miss_counted(self, tmp_path):
        pytest.importorskip("bokeh")
        from execution_trace.diagram import generate_diagram

        csv = tmp_path / "miss.csv"
        # deadline_us=1500 but end_us=2000 → deadline missed
        csv.write_text(
            "name,type,start_us,end_us,priority,deadline_us,value\n"
            "slow_task,task,1000.0,2000.0,4,1500.0,\n"
        )
        out = tmp_path / "diagram.html"
        result = generate_diagram(csv_path=csv, output_path=out)
        assert result.n_missed == 1

    def test_raises_diagram_error_on_missing_csv(self, tmp_path):
        pytest.importorskip("bokeh")
        from execution_trace.diagram import DiagramError, generate_diagram

        out = tmp_path / "diagram.html"
        with pytest.raises(DiagramError):
            generate_diagram(csv_path=tmp_path / "nosuch.csv", output_path=out)
