"""High-level decoder for execution-trace protobuf streams (wire format v2).

Receives raw bytes from any transport (RTT, UART, TCP, …), parses
length-delimited :class:`tracing_pb2.TraceFrame` frames, matches
``SPAN_START`` / ``SPAN_END`` pairs into :class:`TraceEvent` records, and
records :class:`MarkerRecord` annotations directly.

A v2 frame is **not** self-contained: its name is a dictionary id assigned by an
earlier ``NAME_REGISTERED`` frame, and its timestamp is a delta against the
previous frame. Both are resolved here rather than on the device, which is why
:class:`TraceStreamState` carries the dictionary, the running clock and the
sequence tracker across every frame of one connection.

Typical usage::

    buf = bytearray()
    state = TraceStreamState("tracing")
    event_buffer = TraceEventBuffer()

    # ... fill buf from hardware ...
    decode_tracing_stream(buf, state, event_buffer)

    csv_path = write_tracing_csv(
        event_buffer.records + event_buffer.markers,
        output_dir="data",
    )
"""

import csv
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union

from execution_trace._proto import tracing_pb2
from execution_trace.stream import SequenceTracker, iter_frames

logger = logging.getLogger(__name__)

# The firmware's sequence counter wraps here rather than at 2**32: it exists only
# to detect gaps, a gap is read modulo the wrap, and a full 32-bit counter would
# spend up to five varint bytes per frame to buy nothing.
SEQUENCE_MODULUS: int = 16_384

# Reserved dictionary id, emitted when the device's name registry is full. The
# stream stays decodable; the name of that one event is lost.
UNKNOWN_NAME_ID: int = 0

UNKNOWN_NAME: str = "<unknown>"


@dataclass
class TraceEvent:
    """A completed execution span (task activation or ISR execution).

    Produced when a matching ``SPAN_START`` / ``SPAN_END`` pair is observed.

    Attributes:
        name: Task or ISR identifier (max 32 bytes on the firmware side).
        type: ``"task"`` or ``"isr"``.
        start_us: Activation timestamp in microseconds.
        end_us: Completion timestamp in microseconds.
        priority: Scheduler priority (e.g. RTIC priority 1–9); 0 if unknown.
        deadline_us: Absolute deadline in microseconds, or ``None`` if not set.
        value: Optional user payload attached to the span; unused for spans.
    """

    name: str
    type: str
    start_us: float
    end_us: float
    priority: int = 0
    deadline_us: Optional[float] = None
    value: Optional[int] = None


@dataclass
class MarkerRecord:
    """A point-in-time annotation emitted by :py:meth:`TraceSink.record_marker`.

    No matching ``END`` event is required.

    Attributes:
        name: Marker label (max 32 bytes on the firmware side).
        type: Always ``"marker"``.
        timestamp_us: Event timestamp in microseconds.
        value: Optional u32 payload attached by the firmware.
    """

    name: str
    type: str = field(default="marker", init=False)
    timestamp_us: float = 0.0
    value: Optional[int] = None


@dataclass
class NameEntry:
    """One dictionary entry: everything fixed by a span or marker name.

    These attributes are sent once, on the ``NAME_REGISTERED`` frame that assigns
    the id, rather than repeated on every occurrence of the event.

    Attributes:
        name: Task, ISR or marker label.
        source_type: ``tracing_pb2.ISR`` or ``tracing_pb2.TASK``.
        priority: Scheduler priority; 0 when the name was first seen on a frame
            that carries no attributes (a ``SPAN_END`` or ``MARKER``).
        relative_deadline_ms: Deadline relative to activation, or ``None``.
    """

    name: str
    source_type: int = tracing_pb2.TRACE_EVENT_SOURCE_TYPE_UNSPECIFIED
    priority: int = 0
    relative_deadline_ms: Optional[float] = None


class TraceStreamState:
    """Per-connection decoder state for one v2 trace stream.

    A v2 frame carries a dictionary id instead of a name and a delta instead of a
    timestamp, so decoding is stateful: this holds the name dictionary, the
    running clock the deltas accumulate into, the timebase declared by the
    ``TRACE_START`` frame, and the sequence tracker that detects gaps and resets.

    Create one per connection — the device's dictionary and clock both restart
    when it does.

    Args:
        label: Human-readable stream name used in log messages.

    Attributes:
        source_mask: The source-group mask the firmware was built with, or
            ``None`` until a ``TRACE_START`` frame arrives. A group absent from
            the mask is silent by design, which is what distinguishes it from a
            group whose frames were lost.
    """

    def __init__(self, label: str = "Trace event") -> None:
        self.label = label
        self.tracker = SequenceTracker(label, modulus=SEQUENCE_MODULUS)
        self.names: dict[int, NameEntry] = {}
        self.clock_ticks: int = 0
        self.timebase: int = tracing_pb2.NANOSECONDS
        self.core_frequency_hz: int = 0
        self.source_mask: Optional[int] = None
        # Ids already reported as unresolvable. At 1300+ events/s an unregistered
        # id would otherwise log thousands of identical lines per second.
        self._warned_ids: set[int] = set()
        #: Events dropped because their name id could not be resolved.
        self.unresolved_events: int = 0

    def reset(self) -> None:
        """Discard all per-stream state after a device reset.

        The dictionary must go with it: the device reassigns ids from one on
        reboot, so a stale entry would resolve a new id to the wrong name.
        """
        self.tracker = SequenceTracker(self.label, modulus=SEQUENCE_MODULUS)
        self.names.clear()
        self._warned_ids.clear()
        self.unresolved_events = 0
        self.clock_ticks = 0
        self.timebase = tracing_pb2.NANOSECONDS
        self.core_frequency_hz = 0
        self.source_mask = None

    def ticks_to_us(self, ticks: int) -> float:
        """Convert a tick count to microseconds using the declared timebase.

        Args:
            ticks: A tick count in the unit the ``TRACE_START`` frame declared.

        Returns:
            The equivalent duration in microseconds.
        """
        if self.timebase == tracing_pb2.CYCLES and self.core_frequency_hz > 0:
            return ticks * 1_000_000.0 / self.core_frequency_hz
        return ticks / 1_000.0

    def advance(self, frame: tracing_pb2.TraceFrame) -> float:
        """Advance the stream clock by *frame* and return its absolute time in µs.

        ``NAME_REGISTERED`` and ``TRACE_START`` frames carry an absolute
        timestamp and re-establish the origin; every other frame carries a delta
        against the frame before it.

        Args:
            frame: The decoded frame.

        Returns:
            The frame's absolute timestamp in microseconds.
        """
        if frame.event_type in (tracing_pb2.NAME_REGISTERED, tracing_pb2.TRACE_START):
            self.clock_ticks = frame.timestamp_ticks
        else:
            self.clock_ticks += frame.timestamp_ticks
        return self.ticks_to_us(self.clock_ticks)

    def resolve(self, name_id: int) -> Optional[NameEntry]:
        """Look up a dictionary id.

        Args:
            name_id: The id carried by the frame.

        Returns:
            The registered entry, or ``None`` when the id cannot be resolved —
            either the reserved "unknown" id, meaning the device's registry was
            full, or an id whose ``NAME_REGISTERED`` frame the host never saw
            because it attached after the entry was sent.

            Callers must **drop** an unresolvable event rather than attribute it
            to a placeholder name: distinct ids would otherwise share one lane,
            and their spans would interleave into pairs that never existed.
        """
        entry = self.names.get(name_id)
        if entry is not None:
            return entry
        if name_id != UNKNOWN_NAME_ID and name_id not in self._warned_ids:
            self._warned_ids.add(name_id)
            logger.warning(
                "Tracing: name id %d not resolvable yet — dropping its events "
                "until the device's next dictionary refresh", name_id,
            )
        self.unresolved_events += 1
        return None


class TraceEventBuffer:
    """Accumulates :class:`TraceEvent` records by matching ``SPAN_START`` / ``SPAN_END`` pairs.

    ``MARKER`` events are recorded immediately without any matching.

    Thread safety:
        Not thread-safe. Protect access with a lock if multiple threads write to it.
    """

    def __init__(self) -> None:
        # name → (start_us, NameEntry)
        self._pending: dict[str, tuple[float, NameEntry]] = {}
        self._records: list[TraceEvent] = []
        self._markers: list[MarkerRecord] = []

    def push(
        self,
        frame: tracing_pb2.TraceFrame,
        entry: NameEntry,
        timestamp_us: float,
    ) -> None:
        """Process one decoded frame whose name and timestamp are already resolved.

        Args:
            frame: The decoded frame, for its event type and marker value.
            entry: The dictionary entry its ``name_id`` resolves to.
            timestamp_us: Its absolute timestamp in microseconds.
        """
        name = entry.name
        if frame.event_type == tracing_pb2.SPAN_START:
            if name in self._pending:
                logger.warning("Tracing: duplicate START for '%s' — discarding previous", name)
            self._pending[name] = (timestamp_us, entry)
        elif frame.event_type == tracing_pb2.SPAN_END:
            if name not in self._pending:
                logger.warning("Tracing: END for '%s' with no matching START — discarding", name)
                return
            start_us, start_entry = self._pending.pop(name)
            type_str = "isr" if start_entry.source_type == tracing_pb2.ISR else "task"
            deadline_us = (
                start_us + start_entry.relative_deadline_ms * 1_000.0
                if start_entry.relative_deadline_ms is not None
                else None
            )
            self._records.append(
                TraceEvent(
                    name=name,
                    type=type_str,
                    start_us=start_us,
                    end_us=timestamp_us,
                    priority=start_entry.priority,
                    deadline_us=deadline_us,
                )
            )
        elif frame.event_type == tracing_pb2.MARKER:
            value = int(frame.marker_value) if frame.HasField("marker_value") else None
            self._markers.append(
                MarkerRecord(name=name, timestamp_us=timestamp_us, value=value)
            )

    def flush_pending(self) -> None:
        """Warn about any unmatched ``SPAN_START`` events and clear pending state.

        Call this on device reset or session end to surface incomplete spans.
        """
        for name in list(self._pending):
            logger.warning(
                "Tracing: unmatched START for '%s' at shutdown — no END received", name
            )
        self._pending.clear()

    @property
    def records(self) -> list[TraceEvent]:
        """Completed span records in arrival order."""
        return list(self._records)

    @property
    def markers(self) -> list[MarkerRecord]:
        """Marker records in arrival order."""
        return list(self._markers)


def decode_tracing_stream(
    buf: bytearray,
    state: TraceStreamState,
    event_buffer: TraceEventBuffer,
) -> None:
    """Decode all complete frames from *buf* and push events into *event_buffer*.

    Consumes *buf* in-place. Resolves each frame's dictionary id and delta
    timestamp against *state*, which must be the same object across every call
    for one connection.

    Stops and clears *buf* if a device reset is detected — either a backwards
    sequence jump or a second ``TRACE_START`` frame — also resetting *state* and
    calling :meth:`TraceEventBuffer.flush_pending`.

    Args:
        buf: Mutable byte buffer containing raw RTT / transport bytes.
        state: Per-connection decoder state, shared across calls for one stream.
        event_buffer: Accumulator for decoded events.
    """
    for raw in iter_frames(buf):
        frame = tracing_pb2.TraceFrame()
        try:
            frame.ParseFromString(raw)
        except Exception as exc:
            logger.warning("Failed to decode TraceFrame: %s", exc)
            continue

        # The device re-emits the header periodically so that a host attaching
        # mid-run learns the timebase and the mask. That is not a reset — only a
        # header whose absolute timestamp has gone *backwards* is, because the
        # device clock restarts at zero on reboot and nothing carried over from
        # before it is still valid.
        if (
            frame.event_type == tracing_pb2.TRACE_START
            and state.source_mask is not None
            and frame.timestamp_ticks < state.clock_ticks
        ):
            logger.warning("Tracing: device clock restarted — device reset")
            buf.clear()
            event_buffer.flush_pending()
            state.reset()
            break

        if state.tracker.observe(frame.sequence):
            buf.clear()
            event_buffer.flush_pending()
            state.reset()
            break

        timestamp_us = state.advance(frame)

        if frame.event_type == tracing_pb2.TRACE_START:
            state.timebase = frame.timebase
            state.core_frequency_hz = frame.core_frequency_hz
            first_header = state.source_mask is None
            state.source_mask = frame.source_mask
            # The clock was set from the raw ticks before the timebase was known;
            # it is a tick count either way, so only the conversion changes.
            if first_header:
                logger.info(
                    "Tracing: stream start, timebase=%s core=%d Hz source_mask=0x%02X",
                    tracing_pb2.TimeBase.Name(frame.timebase),
                    frame.core_frequency_hz,
                    frame.source_mask,
                )
        elif frame.event_type == tracing_pb2.NAME_REGISTERED:
            state._warned_ids.discard(frame.name_id)
            state.names[frame.name_id] = NameEntry(
                name=frame.name,
                source_type=frame.source_type,
                priority=int(frame.priority),
                relative_deadline_ms=(
                    frame.relative_deadline_ms
                    if frame.HasField("relative_deadline_ms")
                    else None
                ),
            )
        else:
            entry = state.resolve(frame.name_id)
            # An unresolvable id is dropped, not bucketed under a placeholder:
            # see TraceStreamState.resolve.
            if entry is not None:
                event_buffer.push(frame, entry, timestamp_us)


def write_tracing_csv(
    records: list[Union[TraceEvent, MarkerRecord]],
    output_dir: str = "data",
) -> Optional[str]:
    """Write span and marker records to a timestamped CSV file.

    The output directory is created if it does not exist. A ``trace_latest.csv``
    symlink in the same directory is atomically updated to point at the new file.

    CSV columns:

    +---------------+------------------------------------------+
    | Column        | Description                              |
    +===============+==========================================+
    | ``name``      | span / marker name                       |
    +---------------+------------------------------------------+
    | ``type``      | ``task``, ``isr``, or ``marker``         |
    +---------------+------------------------------------------+
    | ``start_us``  | start (or marker) timestamp in µs        |
    +---------------+------------------------------------------+
    | ``end_us``    | end timestamp in µs (= start for markers)|
    +---------------+------------------------------------------+
    | ``priority``  | scheduler priority (0 for markers)       |
    +---------------+------------------------------------------+
    | ``deadline_us``| absolute deadline in µs, or empty       |
    +---------------+------------------------------------------+
    | ``value``     | optional u32 payload, or empty           |
    +---------------+------------------------------------------+

    Args:
        records: Mixed list of :class:`TraceEvent` and :class:`MarkerRecord` objects.
        output_dir: Directory to write the CSV into. Created if absent.

    Returns:
        Absolute path of the written CSV, or ``None`` if *records* is empty.
    """
    if not records:
        logger.warning("Tracing: no completed tracing records to write — CSV not created")
        return None

    os.makedirs(output_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"trace_{stamp}.csv"
    path = os.path.join(output_dir, filename)
    latest = os.path.join(output_dir, "trace_latest.csv")

    # An optional column is empty only when the field is absent. Testing the value for
    # truth instead would write 0 as empty, and zero is a legitimate payload: a reason
    # code, a count of nothing, a deadline at the activation instant.
    def _optional(field: float | int | None) -> float | int | str:
        return "" if field is None else field

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "type", "start_us", "end_us", "priority", "deadline_us", "value"])
        for r in records:
            if isinstance(r, MarkerRecord):
                writer.writerow([
                    r.name, "marker",
                    r.timestamp_us, r.timestamp_us,
                    0, "", _optional(r.value),
                ])
            else:
                writer.writerow([
                    r.name, r.type,
                    r.start_us, r.end_us,
                    r.priority, _optional(r.deadline_us), _optional(r.value),
                ])

    # Atomically replace the symlink so trace_latest.csv always points to newest.
    tmp = latest + ".tmp"
    os.symlink(filename, tmp)
    os.replace(tmp, latest)

    logger.info("Tracing: CSV written → %s  (%d records)", path, len(records))
    logger.info("Visualise: etrace-diagram %s", latest)

    return path
