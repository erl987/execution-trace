"""Shared pytest fixtures for the embedded-etrace test suite."""

import pytest

from execution_trace._proto import tracing_pb2
from execution_trace.decode import SEQUENCE_MODULUS
from execution_trace.stream import encode_varint


def make_frame(
    event_type: int,
    *,
    timestamp_ticks: int = 0,
    name_id: int = 0,
    sequence: int = 0,
    name: str = "",
    source_type: int = tracing_pb2.TRACE_EVENT_SOURCE_TYPE_UNSPECIFIED,
    priority: int = 0,
    relative_deadline_ms: float | None = None,
    marker_value: int | None = None,
    timebase: int = tracing_pb2.NANOSECONDS,
    core_frequency_hz: int = 0,
    source_mask: int = 0,
) -> bytes:
    """Encode a single v2 TraceFrame as a length-delimited protobuf frame.

    Every field is optional so a test can build exactly the frame class it means:
    a per-occurrence event carries only a delta, an id and a sequence; a
    NAME_REGISTERED frame carries the dictionary entry; a TRACE_START frame
    carries the timebase and mask.
    """
    msg = tracing_pb2.TraceFrame()
    msg.timestamp_ticks = timestamp_ticks
    msg.name_id = name_id
    msg.event_type = event_type
    msg.sequence = sequence
    msg.name = name
    msg.source_type = source_type
    msg.priority = priority
    msg.timebase = timebase
    msg.core_frequency_hz = core_frequency_hz
    msg.source_mask = source_mask
    if relative_deadline_ms is not None:
        msg.relative_deadline_ms = relative_deadline_ms
    if marker_value is not None:
        msg.marker_value = marker_value
    raw = msg.SerializeToString()
    return encode_varint(len(raw)) + raw


class StreamBuilder:
    """Builds a v2 byte stream the way the firmware encoder does.

    Mirrors `TraceEncoder`: it assigns dictionary ids on first sight of a name,
    delta-encodes timestamps against the previous frame, and advances a sequence
    counter that wraps at :data:`SEQUENCE_MODULUS`. Tests describe events in
    absolute nanoseconds and by name; this turns them into wire bytes.
    """

    def __init__(self, timebase: int = tracing_pb2.NANOSECONDS, core_frequency_hz: int = 0):
        self.buf = bytearray()
        self._names: dict[str, int] = {}
        self._attrs: dict[str, tuple[int, int, float | None]] = {}
        self._last_ticks = 0
        self._sequence = 0
        self._timebase = timebase
        self._core_frequency_hz = core_frequency_hz

    def _next_sequence(self) -> int:
        seq = self._sequence
        self._sequence = (self._sequence + 1) % SEQUENCE_MODULUS
        return seq

    def start(self, timestamp_ticks: int = 0, source_mask: int = 0x1F) -> "StreamBuilder":
        """Append the TRACE_START header."""
        self.buf += make_frame(
            tracing_pb2.TRACE_START,
            timestamp_ticks=timestamp_ticks,
            sequence=self._next_sequence(),
            timebase=self._timebase,
            core_frequency_hz=self._core_frequency_hz,
            source_mask=source_mask,
        )
        self._last_ticks = timestamp_ticks
        return self

    def _name_id(
        self,
        name: str,
        ticks: int,
        source_type: int,
        priority: int,
        relative_deadline_ms: float | None,
    ) -> int:
        if name in self._names:
            return self._names[name]
        name_id = len(self._names) + 1
        self._names[name] = name_id
        self.buf += make_frame(
            tracing_pb2.NAME_REGISTERED,
            timestamp_ticks=ticks,
            name_id=name_id,
            sequence=self._next_sequence(),
            name=name,
            source_type=source_type,
            priority=priority,
            relative_deadline_ms=relative_deadline_ms,
        )
        self._last_ticks = ticks
        return name_id

    def event(
        self,
        event_type: int,
        name: str,
        ticks: int,
        *,
        source_type: int = tracing_pb2.TASK,
        priority: int = 0,
        relative_deadline_ms: float | None = None,
        marker_value: int | None = None,
        name_id: int | None = None,
    ) -> "StreamBuilder":
        """Append one per-occurrence event, registering its name if new.

        Pass *name_id* to emit a specific id without registering it — used to
        exercise the unknown-id fallback.
        """
        if name_id is None:
            name_id = self._name_id(name, ticks, source_type, priority, relative_deadline_ms)
        delta = ticks - self._last_ticks
        self._last_ticks = ticks
        self.buf += make_frame(
            event_type,
            timestamp_ticks=delta,
            name_id=name_id,
            sequence=self._next_sequence(),
            marker_value=marker_value,
        )
        return self

    def span(
        self,
        name: str,
        start_ticks: int,
        end_ticks: int,
        *,
        source_type: int = tracing_pb2.TASK,
        priority: int = 0,
        relative_deadline_ms: float | None = None,
    ) -> "StreamBuilder":
        """Append a matched SPAN_START / SPAN_END pair."""
        self.event(
            tracing_pb2.SPAN_START, name, start_ticks,
            source_type=source_type, priority=priority,
            relative_deadline_ms=relative_deadline_ms,
        )
        return self.event(tracing_pb2.SPAN_END, name, end_ticks)

    def marker(self, name: str, ticks: int, value: int | None = None) -> "StreamBuilder":
        """Append a MARKER."""
        return self.event(tracing_pb2.MARKER, name, ticks, marker_value=value)

    def bytes(self) -> bytearray:
        """The stream built so far."""
        return bytearray(self.buf)


@pytest.fixture()
def builder() -> StreamBuilder:
    """A fresh v2 stream builder with the header already written."""
    return StreamBuilder().start()


@pytest.fixture()
def sample_stream(builder: StreamBuilder) -> bytearray:
    """A complete stream: one task span, one ISR span and one marker."""
    builder.span("main_task", 1_000_000, 2_000_000,
                 source_type=tracing_pb2.TASK, priority=4)
    builder.span("gyro_isr", 2_100_000, 2_200_000,
                 source_type=tracing_pb2.ISR, priority=8)
    builder.marker("loop_tick", 2_300_000, 42)
    return builder.bytes()


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
