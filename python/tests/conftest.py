"""Shared pytest fixtures for the embedded-etrace test suite."""

import pytest

from execution_trace._proto import tracing_pb2
from execution_trace.stream import encode_varint


def _make_frame(
    event_type: int,
    source_type: int,
    name: str,
    timestamp_ns: int,
    sequence: int = 0,
    priority: int = 0,
    relative_deadline_ms: float | None = None,
    marker_value: int | None = None,
) -> bytes:
    """Encode a single TraceEvent as a length-delimited protobuf frame."""
    msg = tracing_pb2.TraceEvent()
    msg.timestamp_ns = timestamp_ns
    msg.name = name
    msg.source_type = source_type
    msg.event_type = event_type
    msg.sequence = sequence
    msg.priority = priority
    if relative_deadline_ms is not None:
        msg.relative_deadline_ms = relative_deadline_ms
    if marker_value is not None:
        msg.marker_value = marker_value
    raw = msg.SerializeToString()
    return encode_varint(len(raw)) + raw


@pytest.fixture()
def sample_span_start_frame() -> bytes:
    """A well-formed SPAN_START frame for a task named 'main_task'."""
    return _make_frame(
        event_type=tracing_pb2.SPAN_START,
        source_type=tracing_pb2.TASK,
        name="main_task",
        timestamp_ns=1_000_000,
        sequence=0,
        priority=4,
    )


@pytest.fixture()
def sample_span_end_frame() -> bytes:
    """A well-formed SPAN_END frame for a task named 'main_task'."""
    return _make_frame(
        event_type=tracing_pb2.SPAN_END,
        source_type=tracing_pb2.TASK,
        name="main_task",
        timestamp_ns=2_000_000,
        sequence=1,
    )


@pytest.fixture()
def sample_marker_frame() -> bytes:
    """A well-formed MARKER frame."""
    return _make_frame(
        event_type=tracing_pb2.MARKER,
        source_type=tracing_pb2.TASK,
        name="loop_tick",
        timestamp_ns=1_500_000,
        sequence=2,
        marker_value=42,
    )


@pytest.fixture()
def sample_csv_path(tmp_path):
    """Write a minimal valid trace CSV and return its Path."""
    p = tmp_path / "trace.csv"
    p.write_text(
        "name,type,start_us,end_us,priority,deadline_us,value\n"
        "main_task,task,1000.0,2000.0,4,,\n"
        "gyro_isr,isr,500.0,600.0,8,,\n"
        "loop_tick,marker,1500.0,1500.0,0,,42\n"
    )
    return p


@pytest.fixture()
def sample_trace_df(sample_csv_path):
    """A minimal DataFrame suitable for diagram building functions."""
    pd = pytest.importorskip("pandas")
    return pd.read_csv(sample_csv_path)
