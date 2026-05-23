"""Tests for decode.py — TraceEventBuffer, decode_tracing_stream, write_tracing_csv."""

import csv
import logging
import os

import pytest

from execution_trace._proto import tracing_pb2
from execution_trace.decode import (
    MarkerRecord,
    TraceEvent,
    TraceEventBuffer,
    decode_tracing_stream,
    write_tracing_csv,
)
from execution_trace.stream import SequenceTracker, encode_varint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_frame(
    event_type: int,
    source_type: int,
    name: str,
    timestamp_ns: int,
    sequence: int = 0,
    priority: int = 0,
    deadline_ms: float | None = None,
    marker_value: int | None = None,
) -> bytes:
    msg = tracing_pb2.TraceEvent()
    msg.timestamp_ns = timestamp_ns
    msg.name = name
    msg.source_type = source_type
    msg.event_type = event_type
    msg.sequence = sequence
    msg.priority = priority
    if deadline_ms is not None:
        msg.deadline_ms = deadline_ms
    if marker_value is not None:
        msg.marker_value = marker_value
    raw = msg.SerializeToString()
    return encode_varint(len(raw)) + raw


# ---------------------------------------------------------------------------
# TraceEventBuffer
# ---------------------------------------------------------------------------

class TestTraceEventBuffer:
    def test_matches_start_end_pair(self):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "task_a"
        msg.event_type = tracing_pb2.SPAN_START
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 1_000_000
        msg.priority = 4
        buf.push(msg)

        msg2 = tracing_pb2.TraceEvent()
        msg2.name = "task_a"
        msg2.event_type = tracing_pb2.SPAN_END
        msg2.source_type = tracing_pb2.TASK
        msg2.timestamp_ns = 2_000_000
        msg2.priority = 4
        buf.push(msg2)

        records = buf.records
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, TraceEvent)
        assert r.name == "task_a"
        assert r.type == "task"
        assert r.start_us == pytest.approx(1_000.0)
        assert r.end_us == pytest.approx(2_000.0)
        assert r.priority == 4

    def test_isr_source_type_sets_type_field(self):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "gyro_isr"
        msg.event_type = tracing_pb2.SPAN_START
        msg.source_type = tracing_pb2.ISR
        msg.timestamp_ns = 100
        buf.push(msg)

        msg2 = tracing_pb2.TraceEvent()
        msg2.name = "gyro_isr"
        msg2.event_type = tracing_pb2.SPAN_END
        msg2.source_type = tracing_pb2.ISR
        msg2.timestamp_ns = 200
        buf.push(msg2)

        assert buf.records[0].type == "isr"

    def test_unmatched_end_discarded_and_logs(self, caplog):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "ghost"
        msg.event_type = tracing_pb2.SPAN_END
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 999
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            buf.push(msg)
        assert buf.records == []
        assert "no matching START" in caplog.text

    def test_marker_recorded_directly(self):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "loop_tick"
        msg.event_type = tracing_pb2.MARKER
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 5_000_000
        msg.marker_value = 7
        buf.push(msg)

        assert buf.records == []
        markers = buf.markers
        assert len(markers) == 1
        m = markers[0]
        assert isinstance(m, MarkerRecord)
        assert m.name == "loop_tick"
        assert m.type == "marker"
        assert m.timestamp_us == pytest.approx(5_000.0)
        assert m.value == 7

    def test_marker_without_value_is_none(self):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "tick"
        msg.event_type = tracing_pb2.MARKER
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 1_000
        buf.push(msg)
        assert buf.markers[0].value is None

    def test_deadline_ms_converted_to_us(self):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "t"
        msg.event_type = tracing_pb2.SPAN_START
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 0
        msg.deadline_ms = 2.5
        buf.push(msg)

        msg2 = tracing_pb2.TraceEvent()
        msg2.name = "t"
        msg2.event_type = tracing_pb2.SPAN_END
        msg2.source_type = tracing_pb2.TASK
        msg2.timestamp_ns = 1_000
        buf.push(msg2)

        assert buf.records[0].deadline_us == pytest.approx(2_500.0)

    def test_flush_pending_warns_and_clears(self, caplog):
        buf = TraceEventBuffer()
        msg = tracing_pb2.TraceEvent()
        msg.name = "hanging_task"
        msg.event_type = tracing_pb2.SPAN_START
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 0
        buf.push(msg)

        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            buf.flush_pending()

        assert "hanging_task" in caplog.text
        assert "unmatched START" in caplog.text
        # A second flush should produce no warnings
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            buf.flush_pending()
        assert caplog.text == ""

    def test_duplicate_start_warns_and_replaces(self, caplog):
        buf = TraceEventBuffer()
        for ts in (100, 200):
            msg = tracing_pb2.TraceEvent()
            msg.name = "dup"
            msg.event_type = tracing_pb2.SPAN_START
            msg.source_type = tracing_pb2.TASK
            msg.timestamp_ns = ts
            with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
                buf.push(msg)

        # The second START replaces the first; final END should use ts=200
        msg = tracing_pb2.TraceEvent()
        msg.name = "dup"
        msg.event_type = tracing_pb2.SPAN_END
        msg.source_type = tracing_pb2.TASK
        msg.timestamp_ns = 300
        buf.push(msg)

        assert buf.records[0].start_us == pytest.approx(200.0 / 1_000)
        assert "duplicate START" in caplog.text


# ---------------------------------------------------------------------------
# decode_tracing_stream
# ---------------------------------------------------------------------------

class TestDecodeTracingStream:
    def test_happy_path(self, sample_span_start_frame, sample_span_end_frame):
        buf = bytearray(sample_span_start_frame + sample_span_end_frame)
        tracker = SequenceTracker("test")
        event_buffer = TraceEventBuffer()
        decode_tracing_stream(buf, tracker, event_buffer)
        assert len(event_buffer.records) == 1
        assert buf == bytearray()

    def test_reset_detected_clears_buffer(self):
        # Frame with sequence 1000 followed by sequence 0 (reset)
        frame_high = _make_frame(tracing_pb2.SPAN_START, tracing_pb2.TASK, "t", 0, sequence=1000)
        frame_reset = _make_frame(tracing_pb2.SPAN_START, tracing_pb2.TASK, "t", 0, sequence=0)
        buf = bytearray(frame_high + frame_reset + b"\xaa\xbb")  # trailing garbage
        tracker = SequenceTracker("test")
        tracker.observe(999)  # pretend we've seen up to 999
        event_buffer = TraceEventBuffer()

        decode_tracing_stream(buf, tracker, event_buffer)

        # buf is cleared on reset detection
        assert buf == bytearray()


# ---------------------------------------------------------------------------
# write_tracing_csv
# ---------------------------------------------------------------------------

class TestWriteTracingCsv:
    def test_returns_none_for_empty_records(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            result = write_tracing_csv([], output_dir=str(tmp_path))
        assert result is None

    def test_columns_present(self, tmp_path):
        records = [
            TraceEvent(name="t", type="task", start_us=1.0, end_us=2.0, priority=4),
        ]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            reader = csv.DictReader(f)
            assert set(reader.fieldnames or []) >= {
                "name", "type", "start_us", "end_us", "priority", "deadline_us", "value"
            }

    def test_marker_row_start_eq_end(self, tmp_path):
        records = [MarkerRecord(name="tick", timestamp_us=1500.0, value=99)]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["start_us"] == rows[0]["end_us"]
        assert rows[0]["type"] == "marker"

    def test_symlink_updated(self, tmp_path):
        records = [
            TraceEvent(name="t", type="task", start_us=0.0, end_us=1.0, priority=1),
        ]
        write_tracing_csv(records, output_dir=str(tmp_path))
        symlink = tmp_path / "trace_latest.csv"
        assert symlink.is_symlink()
        assert symlink.exists()

    def test_creates_output_directory(self, tmp_path):
        new_dir = str(tmp_path / "subdir" / "traces")
        records = [
            TraceEvent(name="t", type="task", start_us=0.0, end_us=1.0, priority=1),
        ]
        path = write_tracing_csv(records, output_dir=new_dir)
        assert path is not None
        assert os.path.isfile(path)

    def test_span_row_values(self, tmp_path):
        records = [
            TraceEvent(
                name="main_task", type="task",
                start_us=1000.0, end_us=2000.0,
                priority=4, deadline_us=5000.0,
            ),
        ]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["name"] == "main_task"
        assert float(row["start_us"]) == pytest.approx(1000.0)
        assert float(row["end_us"]) == pytest.approx(2000.0)
        assert row["priority"] == "4"
        assert float(row["deadline_us"]) == pytest.approx(5000.0)
