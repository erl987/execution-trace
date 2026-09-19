"""Tests for decode.py — TraceEventBuffer, decode_tracing_stream, write_tracing_csv."""

import csv
import logging
import os

import pytest

from conftest import StreamBuilder, make_frame

from execution_trace._proto import tracing_pb2
from execution_trace.decode import (
    GapRecord,
    REORDER_WINDOW,
    RESET_BACKWARD_MARGIN_US,
    SEQUENCE_MODULUS,
    UNKNOWN_NAME,
    MarkerRecord,
    NameEntry,
    TraceEvent,
    TraceEventBuffer,
    TraceStreamState,
    decode_tracing_stream,
    write_tracing_csv,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _push(
    buf: TraceEventBuffer,
    event_type: int,
    name: str,
    timestamp_us: float,
    *,
    source_type: int = tracing_pb2.TASK,
    priority: int = 0,
    relative_deadline_ms: float | None = None,
    marker_value: int | None = None,
) -> None:
    """Push one already-resolved event into *buf*.

    `TraceEventBuffer.push` takes a frame plus the dictionary entry and absolute
    timestamp the stream decoder resolved for it, so these unit tests supply
    those directly rather than going through a stream.
    """
    frame = tracing_pb2.TraceFrame()
    frame.event_type = event_type
    if marker_value is not None:
        frame.marker_value = marker_value
    entry = NameEntry(
        name=name,
        source_type=source_type,
        priority=priority,
        relative_deadline_ms=relative_deadline_ms,
    )
    buf.push(frame, entry, timestamp_us)


def _decode(stream: bytearray) -> tuple[TraceStreamState, TraceEventBuffer]:
    """Decode a whole stream and return the resulting state and buffer."""
    state = TraceStreamState("test")
    event_buffer = TraceEventBuffer()
    decode_tracing_stream(stream, state, event_buffer)
    return state, event_buffer


# ---------------------------------------------------------------------------
# TraceEventBuffer
# ---------------------------------------------------------------------------

class TestTraceEventBuffer:
    def test_matches_start_end_pair(self):
        buf = TraceEventBuffer()
        _push(buf, tracing_pb2.SPAN_START, "task_a", 1_000.0, priority=4)
        _push(buf, tracing_pb2.SPAN_END, "task_a", 2_000.0)

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
        _push(buf, tracing_pb2.SPAN_START, "gyro_isr", 0.1, source_type=tracing_pb2.ISR)
        _push(buf, tracing_pb2.SPAN_END, "gyro_isr", 0.2)
        assert buf.records[0].type == "isr"

    def test_span_attributes_come_from_the_start_not_the_end(self):
        # In v2 only the dictionary carries priority and deadline, and a SPAN_END
        # resolves to the same entry — but the record must be built from the
        # entry seen at the START, which is the one the span was opened with.
        buf = TraceEventBuffer()
        _push(buf, tracing_pb2.SPAN_START, "t", 0.0, priority=7, relative_deadline_ms=1.0)
        _push(buf, tracing_pb2.SPAN_END, "t", 500.0)
        assert buf.records[0].priority == 7
        assert buf.records[0].deadline_us == pytest.approx(1_000.0)

    def test_unmatched_end_discarded_and_logs(self, caplog):
        buf = TraceEventBuffer()
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            _push(buf, tracing_pb2.SPAN_END, "ghost", 999.0)
        assert buf.records == []
        assert "no matching START" in caplog.text

    def test_marker_recorded_directly(self):
        buf = TraceEventBuffer()
        _push(buf, tracing_pb2.MARKER, "loop_tick", 5_000.0, marker_value=7)

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
        _push(buf, tracing_pb2.MARKER, "tick", 1.0)
        assert buf.markers[0].value is None

    def test_marker_value_of_zero_is_kept(self):
        buf = TraceEventBuffer()
        _push(buf, tracing_pb2.MARKER, "tick", 1.0, marker_value=0)
        assert buf.markers[0].value == 0

    def test_relative_deadline_ms_converted_to_us(self):
        buf = TraceEventBuffer()
        _push(buf, tracing_pb2.SPAN_START, "t", 0.0, relative_deadline_ms=2.5)
        _push(buf, tracing_pb2.SPAN_END, "t", 1.0)
        assert buf.records[0].deadline_us == pytest.approx(2_500.0)

    def test_flush_pending_warns_and_clears(self, caplog):
        buf = TraceEventBuffer()
        _push(buf, tracing_pb2.SPAN_START, "hanging_task", 0.0)

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
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            _push(buf, tracing_pb2.SPAN_START, "dup", 0.1)
            _push(buf, tracing_pb2.SPAN_START, "dup", 0.2)
        _push(buf, tracing_pb2.SPAN_END, "dup", 0.3)

        assert buf.records[0].start_us == pytest.approx(0.2)
        assert "duplicate START" in caplog.text


# ---------------------------------------------------------------------------
# TraceStreamState
# ---------------------------------------------------------------------------

class TestTraceStreamState:
    def test_nanosecond_ticks_convert_to_us(self):
        state = TraceStreamState()
        assert state.ticks_to_us(1_500) == pytest.approx(1.5)

    def test_cycle_ticks_convert_using_the_core_frequency(self):
        state = TraceStreamState()
        state.timebase = tracing_pb2.CYCLES
        state.core_frequency_hz = 72_000_000
        # 72 000 cycles at 72 MHz is exactly 1 ms.
        assert state.ticks_to_us(72_000) == pytest.approx(1_000.0)

    def test_cycles_without_a_frequency_fall_back_to_nanoseconds(self):
        # Rather than dividing by zero: a header that declares CYCLES but no
        # frequency is malformed, and a wrong-but-finite scale is still readable.
        state = TraceStreamState()
        state.timebase = tracing_pb2.CYCLES
        state.core_frequency_hz = 0
        assert state.ticks_to_us(1_000) == pytest.approx(1.0)

    def test_unregistered_id_resolves_to_a_placeholder_and_warns(self, caplog):
        state = TraceStreamState()
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            entry = state.resolve(7)
        assert entry is None
        assert "not resolvable yet" in caplog.text
        assert state.unresolved_events == 1

    def test_an_unresolvable_id_warns_only_once(self, caplog):
        # At 1300+ events/s a per-event warning buries the log; the run that
        # found this produced thousands of identical lines a second.
        state = TraceStreamState()
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            for _ in range(50):
                state.resolve(7)
        assert caplog.text.count("not resolvable yet") == 1

    def test_the_reserved_unknown_id_resolves_silently(self, caplog):
        # Id 0 means the device's registry was full. That is reported by a
        # firmware fault counter, so the host need not warn per event.
        state = TraceStreamState()
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            entry = state.resolve(0)
        assert entry is None
        assert caplog.text == ""

    def test_reset_clears_the_dictionary(self):
        # Ids restart at one on reboot, so a stale entry would resolve a new id
        # to the wrong name.
        state = TraceStreamState()
        state.names[1] = NameEntry(name="old_task")
        state.clock_ticks = 12345
        state.source_mask = 0x1F
        state.reset()
        assert state.names == {}
        assert state.clock_ticks == 0
        assert state.source_mask is None


# ---------------------------------------------------------------------------
# decode_tracing_stream
# ---------------------------------------------------------------------------

class TestDecodeTracingStream:
    def test_happy_path(self, sample_stream):
        state, event_buffer = _decode(sample_stream)
        assert len(event_buffer.records) == 2
        assert len(event_buffer.markers) == 1
        assert sample_stream == bytearray()

    def test_header_fields_reach_the_state(self):
        stream = StreamBuilder().start(timestamp_ticks=1_000, source_mask=0x17).bytes()
        state, _ = _decode(stream)
        assert state.source_mask == 0x17
        assert state.timebase == tracing_pb2.NANOSECONDS

    def test_a_cycles_header_rescales_every_timestamp(self):
        builder = StreamBuilder(timebase=tracing_pb2.CYCLES, core_frequency_hz=72_000_000)
        builder.start(timestamp_ticks=0)
        builder.span("t", 0, 72_000)  # 72 000 cycles at 72 MHz = 1 ms
        state, event_buffer = _decode(builder.bytes())
        assert state.core_frequency_hz == 72_000_000
        assert event_buffer.records[0].end_us == pytest.approx(1_000.0)

    def test_names_resolve_through_the_dictionary(self, sample_stream):
        state, event_buffer = _decode(sample_stream)
        assert {e.name for e in state.names.values()} == {"main_task", "gyro_isr", "loop_tick"}
        assert [r.name for r in event_buffer.records] == ["main_task", "gyro_isr"]
        assert event_buffer.markers[0].name == "loop_tick"

    def test_a_name_is_registered_once_and_reused(self, builder):
        builder.span("t", 0, 1_000)
        builder.span("t", 2_000, 3_000)
        state, event_buffer = _decode(builder.bytes())
        assert len(state.names) == 1
        assert len(event_buffer.records) == 2

    def test_deltas_accumulate_into_absolute_timestamps(self, builder):
        builder.span("main_task", 1_000_000, 2_000_000, priority=4)
        builder.span("main_task", 5_000_000, 5_500_000)
        _, event_buffer = _decode(builder.bytes())
        spans = [(r.start_us, r.end_us) for r in event_buffer.records]
        assert spans == [
            pytest.approx((1_000.0, 2_000.0)),
            pytest.approx((5_000.0, 5_500.0)),
        ]

    def test_attributes_are_carried_by_the_dictionary_not_the_event(self, builder):
        builder.span(
            "gyro_isr", 0, 400_000,
            source_type=tracing_pb2.ISR, priority=8, relative_deadline_ms=1.0,
        )
        _, event_buffer = _decode(builder.bytes())
        r = event_buffer.records[0]
        assert r.type == "isr"
        assert r.priority == 8
        assert r.deadline_us == pytest.approx(1_000.0)

    def test_an_event_with_the_reserved_unknown_id_is_dropped(self, builder, caplog):
        # What the firmware emits when its name registry is full. The event is
        # real, but nothing can say what it was, and attributing it to a shared
        # placeholder would interleave unrelated spans into one lane. The
        # firmware's TracingNameRegistryFull counter is what reports the loss.
        builder.event(tracing_pb2.MARKER, "", 1_000, marker_value=5, name_id=0)
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            state, event_buffer = _decode(builder.bytes())
        assert event_buffer.markers == [], "an unattributable event is dropped"
        assert state.unresolved_events == 1

    def test_partial_frame_is_left_in_the_buffer(self, builder):
        builder.span("t", 0, 1_000)
        stream = builder.bytes()
        head, tail = stream[:-2], stream[-2:]
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        decode_tracing_stream(head, state, event_buffer)
        assert len(head) > 0, "the incomplete frame must be kept for the next read"
        assert event_buffer.records == []
        # Feeding the rest completes the span.
        head.extend(tail)
        decode_tracing_stream(head, state, event_buffer)
        assert len(event_buffer.records) == 1
        assert head == bytearray()

    def test_a_frame_lost_at_the_producer_queue_is_reported(self, caplog):
        """AC 7: a frame dropped before the transport still shows as a gap.

        The number is taken when the event is recorded, upstream of the queue the
        transport drains, so a drop there burns a number and leaves a hole
        (§5.7). Before increment 5 the transport numbered frames itself and this
        loss was invisible — the host saw a contiguous stream with events simply
        absent (§2.6).
        """
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        state.names[1] = NameEntry(name="main_task")

        stream = bytearray()
        seq = 0
        for i in range(REORDER_WINDOW + 4):
            if i == 1:
                seq += 1  # this one never reached the transport
                continue
            stream += make_frame(tracing_pb2.MARKER, name_id=1, sequence=seq)
            seq += 1

        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            decode_tracing_stream(stream, state, event_buffer)

        assert "drop detected" in caplog.text
        assert "1 dropped" in caplog.text
        assert state.tracker.dropped == 1

    def test_reordered_frames_are_not_reported_as_loss(self, caplog):
        # Producer-side numbering means an ISR can take a later number and reach
        # the wire first. That is not loss and must not be reported as any.
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        state.names[1] = NameEntry(name="main_task")

        stream = bytearray()
        for seq in (0, 2, 1, 3, 5, 4, 6):
            stream += make_frame(tracing_pb2.MARKER, name_id=1, sequence=seq)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            decode_tracing_stream(stream, state, event_buffer)

        assert caplog.text == ""
        assert state.tracker.dropped == 0
        assert len(event_buffer.markers) == 7, "every frame is still delivered"

    def test_sequence_wraps_at_the_modulus_without_a_false_reset(self, caplog):
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        # Walk the tracker up to the last sequence before the wrap, then feed the
        # wrapped frame. A u32 tracker would read 0 after 16383 as a huge jump.
        state.names[1] = NameEntry(name="tick")
        state.tracker.observe(SEQUENCE_MODULUS - 1)
        stream = bytearray(make_frame(tracing_pb2.MARKER, name_id=1, sequence=0))
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            decode_tracing_stream(stream, state, event_buffer)
        assert caplog.text == ""
        assert len(event_buffer.markers) == 1

    def test_backwards_sequence_is_a_reset_that_clears_the_dictionary(self):
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        state.tracker.observe(1_000)
        state.names[1] = NameEntry(name="stale")
        # Restarting at 0 from 1000 is an apparent forward jump of 15 383, more
        # than half the modulus, so it reads as a backwards step: a reset.
        stream = bytearray(make_frame(tracing_pb2.MARKER, name_id=1, sequence=0))
        decode_tracing_stream(stream, state, event_buffer)
        assert state.names == {}, "a stale entry would resolve new ids to old names"

    def test_a_reset_does_not_discard_the_byte_buffer(self):
        # The stream is length-prefixed with no sync marker, so dropping bytes
        # mid-frame misaligns everything after it permanently — which showed up
        # on hardware as a burst of "Failed to decode TraceFrame" immediately
        # after every reset (§19.10).
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        state.tracker.observe(1_000)
        state.names[1] = NameEntry(name="stale")

        stream = bytearray(make_frame(tracing_pb2.MARKER, name_id=1, sequence=0))
        # A complete, well-formed frame arriving after the reset point.
        stream += make_frame(
            tracing_pb2.NAME_REGISTERED, name_id=1, sequence=1, name="fresh",
            source_type=tracing_pb2.TASK,
        )
        stream += make_frame(tracing_pb2.MARKER, name_id=1, sequence=2, marker_value=9)
        decode_tracing_stream(stream, state, event_buffer)

        assert stream == bytearray(), "every complete frame is still consumed"
        assert state.names[1].name == "fresh", "frames after the reset still parse"
        assert [m.value for m in event_buffer.markers] == [9]

    def test_a_reset_from_high_in_the_range_is_not_visible_in_the_sequence(self, caplog):
        # A consequence of wrapping at 16 384 rather than 2**32: restarting at 0
        # only looks backwards when the pre-reset counter was below half the
        # modulus. From 9 000 it reads as an ordinary 7 383-frame gap, and the
        # stale dictionary survives. This is why TRACE_START, not the sequence,
        # is the reliable reset signal — see the test below.
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()
        state.tracker.observe(9_000)
        state.names[1] = NameEntry(name="stale")
        stream = bytearray(make_frame(tracing_pb2.MARKER, name_id=1, sequence=0))
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            decode_tracing_stream(stream, state, event_buffer)
        assert "drop detected" in caplog.text
        assert "reset" not in caplog.text
        assert state.names == {1: NameEntry(name="stale")}

    def test_a_dictionary_refresh_resolves_events_seen_before_it(self, builder):
        # What a host attaching mid-run sees: events for ids it never saw
        # registered, then the device's periodic re-emission of the dictionary.
        state = TraceStreamState("test")
        event_buffer = TraceEventBuffer()

        # Events arrive with an id the host has no entry for.
        orphan = bytearray()
        orphan += make_frame(tracing_pb2.SPAN_START, name_id=1, sequence=0)
        orphan += make_frame(tracing_pb2.SPAN_END, timestamp_ticks=500, name_id=1, sequence=1)
        decode_tracing_stream(orphan, state, event_buffer)
        assert event_buffer.records == [], "unresolvable events are dropped"
        assert state.unresolved_events == 2

        # The refresh arrives: the entry is now known, and later events resolve.
        refresh = bytearray()
        refresh += make_frame(
            tracing_pb2.NAME_REGISTERED,
            timestamp_ticks=10_000,
            name_id=1,
            sequence=2,
            name="main_task",
            source_type=tracing_pb2.TASK,
            priority=4,
        )
        refresh += make_frame(tracing_pb2.SPAN_START, name_id=1, sequence=3)
        refresh += make_frame(tracing_pb2.SPAN_END, timestamp_ticks=500, name_id=1, sequence=4)
        decode_tracing_stream(refresh, state, event_buffer)

        assert [r.name for r in event_buffer.records] == ["main_task"]
        assert event_buffer.records[0].priority == 4

    def test_a_repeated_header_is_not_a_reset(self, caplog):
        # The device re-emits the header periodically so a late host learns the
        # timebase and mask. Treating that as a reset would clear the dictionary
        # every refresh and undo the very thing the refresh exists to fix.
        builder = StreamBuilder().start(timestamp_ticks=1_000)
        builder.span("t", 2_000, 3_000)
        state, event_buffer = _decode(builder.bytes())
        assert len(state.names) == 1

        later = bytearray(
            make_frame(
                tracing_pb2.TRACE_START,
                timestamp_ticks=9_000,
                sequence=state.tracker._last + 1,
                source_mask=0x1F,
            )
        )
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            decode_tracing_stream(later, state, event_buffer)
        assert "reset" not in caplog.text
        assert len(state.names) == 1, "the dictionary must survive a refresh"

    def test_a_header_slightly_behind_the_clock_is_not_a_reset(self, caplog):
        # The header's timestamp is read after the producer queue is drained, but
        # an event recorded just before that read can still be encoded after it,
        # putting the header microseconds behind the reconstructed clock. Reading
        # that as a reboot would discard the dictionary every refresh (§19.10).
        builder = StreamBuilder().start(timestamp_ticks=1_000)
        builder.span("t", 2_000_000, 3_000_000)
        state, event_buffer = _decode(builder.bytes())
        assert len(state.names) == 1

        behind = bytearray(
            make_frame(
                tracing_pb2.TRACE_START,
                timestamp_ticks=state.clock_ticks - 300_000,  # 300 µs behind
                sequence=state.tracker._last + 1,
                source_mask=0x1F,
            )
        )
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            decode_tracing_stream(behind, state, event_buffer)
        assert "reset" not in caplog.text
        assert len(state.names) == 1

    def test_a_second_header_is_treated_as_a_device_reset(self, caplog):
        # A real device has been up for seconds before it reboots, so the clock
        # drops by its whole uptime — far past RESET_BACKWARD_MARGIN_US.
        builder = StreamBuilder().start(timestamp_ticks=30_000_000_000)
        builder.span("t", 30_001_000_000, 30_002_000_000)
        stream = builder.bytes()
        state, event_buffer = _decode(stream)
        assert len(state.names) == 1

        reboot = StreamBuilder().start(timestamp_ticks=0, source_mask=0x1F)
        reboot.span("t", 0, 500)
        buf = reboot.bytes()
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            decode_tracing_stream(buf, state, event_buffer)
        assert "device reset" in caplog.text
        # The pre-reboot dictionary is gone, and the reboot's own frames — which
        # follow in the same buffer — are parsed rather than thrown away.
        assert state.names[1].name == "t"
        assert state.source_mask == 0x1F
        assert buf == bytearray()

    def test_malformed_payload_is_skipped_without_killing_the_stream(self, caplog):
        stream = StreamBuilder().start().bytes()
        # A frame whose length prefix is right but whose payload is not a
        # TraceFrame: field 1 declared as a length-delimited string.
        stream += bytes([3, 0x0A, 0x7F, 0x7F])
        with caplog.at_level(logging.WARNING, logger="execution_trace.decode"):
            _decode(stream)
        assert stream == bytearray(), "the bad frame is consumed, not left to jam the buffer"


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

    def test_marker_value_of_zero_is_written(self, tmp_path):
        # Zero is a legitimate payload — a reason code, or a count of nothing — and a
        # truth test on the value would write it as empty, which reads downstream as
        # "no value" and hides exactly the case the marker was emitted to report.
        records = [MarkerRecord(name="ctl_skip", timestamp_us=1500.0, value=0)]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["value"] == "0"

    def test_marker_without_value_is_written_empty(self, tmp_path):
        records = [MarkerRecord(name="tick", timestamp_us=1500.0, value=None)]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["value"] == ""

    def test_span_deadline_and_value_of_zero_are_written(self, tmp_path):
        records = [
            TraceEvent(
                name="t", type="task", start_us=0.0, end_us=1.0,
                priority=1, deadline_us=0.0, value=0,
            ),
        ]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert float(row["deadline_us"]) == pytest.approx(0.0)
        assert row["value"] == "0"

    def test_span_without_deadline_or_value_is_written_empty(self, tmp_path):
        records = [
            TraceEvent(name="t", type="task", start_us=0.0, end_us=1.0, priority=1),
        ]
        path = write_tracing_csv(records, output_dir=str(tmp_path))
        assert path is not None
        with open(path) as f:
            row = list(csv.DictReader(f))[0]
        assert row["deadline_us"] == ""
        assert row["value"] == ""

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


# ---------------------------------------------------------------------------
# Gap handling (§5.9)
# ---------------------------------------------------------------------------

class TestGapHandling:
    """AC 8: a stream with an injected gap renders every span open at the gap as
    interrupted, and the gap as an annotated band."""

    def _stream_with_lost_span_end(self) -> tuple[TraceStreamState, TraceEventBuffer]:
        b = StreamBuilder().start(timestamp_ticks=0)
        t = 1_000_000
        for _ in range(5):
            b.span("main_task", t, t + 400_000, priority=4)
            t += 1_000_000
        # A cycle whose SPAN_END never reached the transport: its number is
        # burned, which is what makes the loss visible at all (§5.7).
        b.event(tracing_pb2.SPAN_START, "main_task", t, priority=4)
        b._next_sequence()
        t += 1_000_000
        # Enough traffic afterwards to carry past the reorder window.
        for _ in range(REORDER_WINDOW + 4):
            b.span("gyro_isr", t, t + 40_000, source_type=tracing_pb2.ISR, priority=8)
            t += 1_000_000
        state, event_buffer = TraceStreamState("gap"), TraceEventBuffer()
        decode_tracing_stream(b.bytes(), state, event_buffer)
        return state, event_buffer

    def test_a_gap_is_recorded_with_its_frame_count(self):
        state, eb = self._stream_with_lost_span_end()
        assert state.tracker.dropped == 1
        assert len(eb.gaps) == 1
        gap = eb.gaps[0]
        assert isinstance(gap, GapRecord)
        assert gap.frames_lost == 1
        assert gap.type == "gap"
        assert gap.end_us >= gap.start_us

    def test_the_gap_is_placed_between_the_frames_either_side_of_it(self):
        # Not where the reorder window happened to notice: that is up to 64
        # frames later, which on this trace would be tens of milliseconds off.
        state, eb = self._stream_with_lost_span_end()
        gap = eb.gaps[0]
        assert gap.start_us == pytest.approx(6_000.0), "the lost END's own SPAN_START"
        assert gap.end_us == pytest.approx(7_000.0), "the next frame that did arrive"

    def test_the_span_open_at_the_gap_is_interrupted(self):
        state, eb = self._stream_with_lost_span_end()
        cut = [r for r in eb.records if r.interrupted]
        assert len(cut) == 1
        assert cut[0].name == "main_task"
        assert cut[0].start_us == pytest.approx(6_000.0)
        assert cut[0].end_us == pytest.approx(6_000.0), "cut at the last good frame"

    def test_an_uninterrupted_span_is_untouched(self):
        state, eb = self._stream_with_lost_span_end()
        whole = [r for r in eb.records if not r.interrupted and r.name == "main_task"]
        assert len(whole) == 5
        assert all(r.end_us - r.start_us == pytest.approx(400.0) for r in whole)

    def test_the_first_end_after_the_gap_is_discarded_not_paired(self):
        # Otherwise it closes whatever START opens next and invents a span that
        # never ran — the damage §5.9 exists to prevent.
        b = StreamBuilder().start(timestamp_ticks=0)
        b.event(tracing_pb2.SPAN_START, "main_task", 1_000_000, priority=4)
        b._next_sequence()  # a frame lost at the queue
        for i in range(REORDER_WINDOW + 4):
            b.span("gyro_isr", 2_000_000 + i * 1_000_000,
                   2_040_000 + i * 1_000_000,
                   source_type=tracing_pb2.ISR, priority=8)
        # main_task's END finally arrives, long after its START was closed.
        b.event(tracing_pb2.SPAN_END, "main_task", 90_000_000)
        state, eb = TraceStreamState("gap"), TraceEventBuffer()
        decode_tracing_stream(b.bytes(), state, eb)

        main = [r for r in eb.records if r.name == "main_task"]
        assert len(main) == 1, "the orphaned END must not produce a second span"
        assert main[0].interrupted
        assert main[0].end_us == pytest.approx(1_000.0)

    def test_a_clean_stream_records_no_gap(self, builder):
        builder.span("t", 0, 1_000)
        builder.span("t", 2_000, 3_000)
        _, eb = _decode(builder.bytes())
        assert eb.gaps == []
        assert not any(r.interrupted for r in eb.records)

    def test_gap_rows_reach_the_csv(self, tmp_path):
        _, eb = self._stream_with_lost_span_end()
        path = write_tracing_csv(eb.records + eb.markers + eb.gaps, output_dir=str(tmp_path))
        assert path is not None
        rows = list(csv.DictReader(open(path)))
        gaps = [r for r in rows if r["type"] == "gap"]
        assert len(gaps) == 1
        assert gaps[0]["value"] == "1", "frames lost travels in the value column"
        assert any(r["interrupted"] == "1" for r in rows)
