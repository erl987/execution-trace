"""High-level decoder for execution-trace protobuf streams.

Receives raw bytes from any transport (RTT, UART, TCP, …), parses
length-delimited :class:`TraceEvent` protobuf frames, matches
``SPAN_START`` / ``SPAN_END`` pairs into :class:`TraceEvent` records,
and records :class:`MarkerRecord` annotations directly.

Typical usage::

    buf = bytearray()
    tracker = SequenceTracker("tracing")
    event_buffer = TraceEventBuffer()

    # ... fill buf from hardware ...
    decode_tracing_stream(buf, tracker, event_buffer)

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


class TraceEventBuffer:
    """Accumulates :class:`TraceEvent` records by matching ``SPAN_START`` / ``SPAN_END`` pairs.

    ``MARKER`` events are recorded immediately without any matching.

    Thread safety:
        Not thread-safe. Protect access with a lock if multiple threads write to it.
    """

    def __init__(self) -> None:
        # name → (timestamp_ns, priority, deadline_ms)
        self._pending: dict[str, tuple[int, int, Optional[float]]] = {}
        self._records: list[TraceEvent] = []
        self._markers: list[MarkerRecord] = []

    def push(self, msg: tracing_pb2.TraceEvent) -> None:  # type: ignore[name-defined]
        """Process one decoded :class:`tracing_pb2.TraceEvent`.

        Args:
            msg: Decoded protobuf message from the firmware.
        """
        name = msg.name
        if msg.event_type == tracing_pb2.SPAN_START:
            if name in self._pending:
                logger.warning("Tracing: duplicate START for '%s' — discarding previous", name)
            deadline_ms = msg.deadline_ms if msg.HasField("deadline_ms") else None
            self._pending[name] = (msg.timestamp_ns, int(msg.priority), deadline_ms)
        elif msg.event_type == tracing_pb2.SPAN_END:
            if name not in self._pending:
                logger.warning("Tracing: END for '%s' with no matching START — discarding", name)
                return
            start_ns, priority, deadline_ms = self._pending.pop(name)
            type_str = "isr" if msg.source_type == tracing_pb2.ISR else "task"
            deadline_us = deadline_ms * 1_000.0 if deadline_ms is not None else None
            self._records.append(
                TraceEvent(
                    name=name,
                    type=type_str,
                    start_us=start_ns / 1_000.0,
                    end_us=msg.timestamp_ns / 1_000.0,
                    priority=priority,
                    deadline_us=deadline_us,
                )
            )
        elif msg.event_type == tracing_pb2.MARKER:
            value = int(msg.marker_value) if msg.HasField("marker_value") else None
            self._markers.append(
                MarkerRecord(
                    name=name,
                    timestamp_us=msg.timestamp_ns / 1_000.0,
                    value=value,
                )
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
    tracker: SequenceTracker,
    event_buffer: TraceEventBuffer,
) -> None:
    """Decode all complete frames from *buf* and push events into *event_buffer*.

    Consumes *buf* in-place. Stops and clears *buf* if a device reset is detected
    (sequence number jumped backward), also calling :meth:`TraceEventBuffer.flush_pending`.

    Args:
        buf: Mutable byte buffer containing raw RTT / transport bytes.
        tracker: Sequence number tracker shared across calls for the same stream.
        event_buffer: Accumulator for decoded events.
    """
    for raw in iter_frames(buf):
        msg = tracing_pb2.TraceEvent()
        try:
            msg.ParseFromString(raw)
        except Exception as exc:
            logger.warning("Failed to decode TraceEvent: %s", exc)
            continue
        if tracker.observe(msg.sequence):
            buf.clear()
            event_buffer.flush_pending()
            break
        event_buffer.push(msg)


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

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "type", "start_us", "end_us", "priority", "deadline_us", "value"])
        for r in records:
            if isinstance(r, MarkerRecord):
                writer.writerow([
                    r.name, "marker",
                    r.timestamp_us, r.timestamp_us,
                    0, "", r.value or "",
                ])
            else:
                writer.writerow([
                    r.name, r.type,
                    r.start_us, r.end_us,
                    r.priority, r.deadline_us or "", r.value or "",
                ])

    # Atomically replace the symlink so trace_latest.csv always points to newest.
    tmp = latest + ".tmp"
    os.symlink(filename, tmp)
    os.replace(tmp, latest)

    logger.info("Tracing: CSV written → %s  (%d records)", path, len(records))
    logger.info("Visualise: etrace-diagram %s", latest)

    return path
