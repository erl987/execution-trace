"""Tests for stream.py — SequenceTracker, iter_frames, decode_varint, encode_varint."""

import logging

import pytest

from execution_trace.stream import (
    RESET_THRESHOLD,
    SequenceTracker,
    decode_varint,
    encode_varint,
    iter_frames,
)


# ---------------------------------------------------------------------------
# SequenceTracker
# ---------------------------------------------------------------------------

class TestSequenceTracker:
    def test_first_frame_returns_false(self):
        t = SequenceTracker("Test")
        assert t.observe(0) is False

    def test_sequential_frames_return_false(self):
        t = SequenceTracker("Test")
        t.observe(0)
        assert t.observe(1) is False
        assert t.observe(2) is False

    def test_small_drop_returns_false_and_logs(self, caplog):
        t = SequenceTracker("Test")
        t.observe(10)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            result = t.observe(13)
        assert result is False
        assert "drop detected" in caplog.text
        assert "expected #11" in caplog.text
        assert "got #13" in caplog.text

    def test_reset_to_zero_returns_true(self, caplog):
        t = SequenceTracker("Attitude frame")
        t.observe(912)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            result = t.observe(0)
        assert result is True
        assert "device reset detected" in caplog.text
        assert "resuming from #0" in caplog.text

    def test_reset_clears_tracker_so_next_frame_accepted_silently(self, caplog):
        t = SequenceTracker("Test")
        t.observe(500)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            t.observe(0)
        assert "device reset detected" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            assert t.observe(0) is False
            assert t.observe(1) is False
        assert caplog.text == ""

    def test_drop_count_exactly_at_threshold_is_not_a_reset(self):
        t = SequenceTracker("Test")
        t.observe(0)
        boundary_seq = (1 + RESET_THRESHOLD) & 0xFFFFFFFF
        assert t.observe(boundary_seq) is False

    def test_drop_count_one_above_threshold_is_a_reset(self):
        t = SequenceTracker("Test")
        t.observe(0)
        reset_seq = (1 + RESET_THRESHOLD + 1) & 0xFFFFFFFF
        assert t.observe(reset_seq) is True

    def test_wraparound_is_not_a_reset(self):
        t = SequenceTracker("Test")
        t.observe(0xFFFFFFFF)
        assert t.observe(0) is False

    def test_no_warning_on_first_frame(self, caplog):
        t = SequenceTracker("Test")
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            t.observe(42)
        assert caplog.text == ""

    def test_no_warning_on_sequential_frames(self, caplog):
        t = SequenceTracker("Test")
        t.observe(0)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            t.observe(1)
        assert caplog.text == ""


# ---------------------------------------------------------------------------
# decode_varint
# ---------------------------------------------------------------------------

class TestDecodeVarint:
    def test_single_byte_zero(self):
        assert decode_varint(b"\x00") == (0, 1)

    def test_single_byte_one(self):
        assert decode_varint(b"\x01") == (1, 1)

    def test_single_byte_max(self):
        # 0x7F = 127, all 7 low bits set, MSB clear
        assert decode_varint(b"\x7f") == (127, 1)

    def test_two_byte_value_128(self):
        # 128 = 0x80 0x01 in LEB128
        assert decode_varint(b"\x80\x01") == (128, 2)

    def test_two_byte_value_300(self):
        # 300 = 0xAC 0x02
        assert decode_varint(b"\xac\x02") == (300, 2)

    def test_max_32bit(self):
        # 0xFFFFFFFF = 4294967295 → 5 bytes in LEB128
        val, n = decode_varint(encode_varint(0xFFFFFFFF))
        assert val == 0xFFFFFFFF
        assert n == 5

    def test_empty_returns_zero_zero(self):
        assert decode_varint(b"") == (0, 0)

    def test_incomplete_returns_zero_zero(self):
        # MSB set but no continuation byte
        assert decode_varint(b"\x80") == (0, 0)

    def test_trailing_bytes_ignored(self):
        # Extra bytes after the varint are not consumed
        val, n = decode_varint(b"\x01\xff\xff")
        assert val == 1
        assert n == 1

    def test_accepts_bytearray(self):
        assert decode_varint(bytearray(b"\x05")) == (5, 1)


# ---------------------------------------------------------------------------
# encode_varint
# ---------------------------------------------------------------------------

class TestEncodeVarint:
    def test_zero(self):
        assert encode_varint(0) == b"\x00"

    def test_one(self):
        assert encode_varint(1) == b"\x01"

    def test_127(self):
        assert encode_varint(127) == b"\x7f"

    def test_128(self):
        assert encode_varint(128) == b"\x80\x01"

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            encode_varint(-1)

    def test_round_trip_small(self):
        for v in range(256):
            enc = encode_varint(v)
            dec, _ = decode_varint(enc)
            assert dec == v

    def test_round_trip_large(self):
        for v in [0, 1, 127, 128, 255, 256, 16383, 16384, 0xFFFFFFFF]:
            assert decode_varint(encode_varint(v))[0] == v


# ---------------------------------------------------------------------------
# iter_frames
# ---------------------------------------------------------------------------

class TestIterFrames:
    def _make_frame(self, payload: bytes) -> bytes:
        return encode_varint(len(payload)) + payload

    def test_single_frame_consumed(self):
        payload = b"hello"
        buf = bytearray(self._make_frame(payload))
        frames = list(iter_frames(buf))
        assert frames == [payload]
        assert buf == bytearray()

    def test_two_concatenated_frames(self):
        p1, p2 = b"first", b"second"
        buf = bytearray(self._make_frame(p1) + self._make_frame(p2))
        frames = list(iter_frames(buf))
        assert frames == [p1, p2]
        assert buf == bytearray()

    def test_incomplete_frame_not_consumed(self):
        payload = b"incomplete"
        complete = self._make_frame(b"done")
        # Truncate the second frame: only the varint prefix, no body
        truncated = encode_varint(len(payload))
        buf = bytearray(complete + truncated)
        frames = list(iter_frames(buf))
        assert frames == [b"done"]
        # The incomplete frame (varint prefix) remains
        assert len(buf) > 0

    def test_empty_buffer_yields_nothing(self):
        buf = bytearray()
        assert list(iter_frames(buf)) == []

    def test_frame_with_empty_payload(self):
        buf = bytearray(self._make_frame(b""))
        frames = list(iter_frames(buf))
        assert frames == [b""]
        assert buf == bytearray()


class TestSequenceTrackerReordering:
    """The v2 trace stream numbers frames at their producer, so they can arrive
    slightly out of order; see SequenceTracker's Reordering note."""

    def _tracker(self, window: int = 4) -> SequenceTracker:
        return SequenceTracker("Test", modulus=64, reorder_window=window)

    def test_a_swapped_pair_is_not_reported_as_loss(self, caplog):
        t = self._tracker()
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            for s in (10, 12, 11, 13):  # 12 overtook 11
                assert t.observe(s) is False
        assert caplog.text == ""
        assert t.dropped == 0

    def test_a_frame_arriving_late_within_the_window_closes_its_hole(self, caplog):
        t = self._tracker()
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            for s in (10, 14, 13, 12, 11, 15):
                t.observe(s)
        assert caplog.text == ""
        assert t.dropped == 0

    def test_a_real_hole_is_reported_once_the_window_is_passed(self, caplog):
        # 12 never arrives. Nothing is said until a frame lands more than the
        # window past it, which bounds how late the report can be.
        t = self._tracker(window=4)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            for s in (10, 11, 13, 14, 15, 16):
                t.observe(s)
            assert t.dropped == 0, "still inside the window"
            t.observe(17)
        assert t.dropped == 1
        assert "1 dropped" in caplog.text

    def test_the_count_is_right_when_several_are_missing(self, caplog):
        t = self._tracker(window=2)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            t.observe(10)
            t.observe(20)  # 11..19 missing, far past the window
        assert t.dropped == 9

    def test_a_window_does_not_hide_a_device_reset(self, caplog):
        # Detectable only when the pre-reset counter was low in the range: from
        # high in it a restart at zero is indistinguishable from a forward gap,
        # which is why the trace decoder leans on TRACE_START instead
        # (EXEC-TRACE-002 §19.6).
        t = self._tracker()
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            t.observe(10)
            assert t.observe(0) is True
        assert "device reset" in caplog.text

    def test_a_late_arrival_after_its_hole_was_reported_is_ignored(self, caplog):
        # The window has already passed the hole and called it lost; the straggler
        # must not then be read as a backwards jump.
        t = self._tracker(window=2)
        t.observe(10)
        t.observe(15)  # 11..14 declared lost
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            assert t.observe(14) is False
        assert "reset" not in caplog.text

    def test_zero_window_reports_immediately(self, caplog):
        # Single-producer streams — attitude, status — keep strict ordering and
        # should not have their gap reports delayed.
        t = SequenceTracker("Strict", modulus=64, reorder_window=0)
        with caplog.at_level(logging.WARNING, logger="execution_trace.stream"):
            t.observe(10)
            t.observe(12)
        assert t.dropped == 1
        assert "1 dropped" in caplog.text

    def test_window_must_be_sane(self):
        with pytest.raises(ValueError):
            SequenceTracker("Test", modulus=64, reorder_window=32)
        with pytest.raises(ValueError):
            SequenceTracker("Test", modulus=64, reorder_window=-1)
