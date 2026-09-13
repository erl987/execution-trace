"""Frame-level streaming utilities for length-delimited protobuf streams.

These primitives are transport-agnostic: they work with any byte source that
accumulates data into a ``bytearray`` (RTT, UART, TCP, etc.).

Typical usage::

    buf = bytearray()
    tracker = SequenceTracker("my stream")

    # ... append bytes received from hardware into buf ...

    for raw_proto_bytes in iter_frames(buf):
        msg = MyProto()
        msg.ParseFromString(raw_proto_bytes)
        if tracker.observe(msg.sequence):
            buf.clear()   # discard partial data after a device reset
            break
        process(msg)
"""

import logging
from typing import Generator

logger = logging.getLogger(__name__)

# The default counter width, for streams that carry a full 32-bit sequence.
DEFAULT_MODULUS: int = 1 << 32

# A "drop" count exceeding half the counter range means the sequence went backward —
# a device reset rather than actual packet loss.
RESET_THRESHOLD: int = DEFAULT_MODULUS >> 1


class SequenceTracker:
    """Tracks a frame sequence, reporting loss and device resets.

    Embedded firmware increments a counter with every frame. A hole in the
    numbers is loss; a large backwards jump is the counter starting over, which
    means the device reset.

    Reordering:
        With a *reorder_window* above zero the tracker tolerates frames arriving
        slightly out of order before calling a hole loss. The execution-trace v2
        format needs this: a frame is numbered by its producer, before it reaches
        the queue the transport drains (EXEC-TRACE-002 §5.7), so an ISR that
        preempts a task between those two points takes a later number and reaches
        the wire first. The transport's own frames — the stream header and the
        dictionary — are numbered when written and can likewise overtake events
        already queued.

        A hole is therefore only reported once a number arrives more than
        *reorder_window* ahead of it, which bounds how long the report is
        delayed. Single-producer streams should leave the window at zero and get
        the report immediately.

    Args:
        label: Human-readable stream name used in log messages.
        modulus: The value the counter wraps at. Defaults to the full 32-bit
            range; the execution-trace v2 format wraps far earlier, at
            :data:`execution_trace.decode.SEQUENCE_MODULUS`, because the counter
            exists only to detect gaps and a gap is read modulo the wrap.
        reorder_window: How far ahead a number may arrive before the numbers it
            skipped are declared lost. Zero means strictly ordered.

    Attributes:
        dropped: Running total of frames reported lost.

    Raises:
        ValueError: If *modulus* is not a positive power of two, which the
            masking arithmetic below assumes.
    """

    def __init__(
        self,
        label: str = "Frame",
        modulus: int = DEFAULT_MODULUS,
        reorder_window: int = 0,
    ) -> None:
        if modulus <= 0 or modulus & (modulus - 1):
            raise ValueError(f"modulus must be a positive power of two, got {modulus}")
        if not 0 <= reorder_window < modulus // 2:
            raise ValueError(
                f"reorder_window must be in [0, {modulus // 2}), got {reorder_window}"
            )
        self._label = label
        self._modulus = modulus
        self._mask = modulus - 1
        # Half the range: a larger apparent forward jump is really a backward one.
        self._reset_threshold = modulus >> 1
        self._window = reorder_window
        self._expected: int | None = None
        # Numbers seen ahead of _expected, still within the window.
        self._pending: set[int] = set()
        self.dropped = 0

    @property
    def _last(self) -> int | None:
        """The last number consumed in order, or ``None`` before the first."""
        return None if self._expected is None else (self._expected - 1) & self._mask

    def observe(self, sequence: int) -> bool:
        """Record a sequence number and detect resets or drops.

        Args:
            sequence: The sequence number from the received frame.

        Returns:
            ``True`` if a device reset was detected (the counter jumped far
            backwards), ``False`` otherwise — including for ordinary loss, which
            is logged and counted rather than signalled.
        """
        if self._expected is None:
            self._expected = (sequence + 1) & self._mask
            return False
        expected = self._expected

        # Distance forward from what we expect next, in the counter's own
        # arithmetic. Anything past the halfway point is really a step backwards.
        ahead = (sequence - expected) & self._mask

        if ahead > self._reset_threshold:
            behind = self._modulus - ahead
            if behind <= self._window:
                # A frame that overtook us earlier and is only now arriving, or a
                # duplicate. Either way it fills nothing we have not moved past.
                self._pending.discard(sequence)
                return False
            logger.warning(
                "%s: device reset detected, resuming from #%d", self._label, sequence
            )
            self._reset_to(sequence)
            return True

        if ahead == 0:
            self._expected = self._absorb_pending((sequence + 1) & self._mask)
            return False

        if ahead <= self._window:
            # Early: hold it and wait for the numbers it skipped.
            self._pending.add(sequence)
            return False

        # Past the window, so whatever is still missing is genuinely lost.
        missing = ahead - sum(
            1 for p in self._pending if (p - expected) & self._mask < ahead
        )
        if missing > 0:
            self.dropped += missing
            logger.warning(
                "%s drop detected: expected #%d, got #%d (%d dropped)",
                self._label, expected, sequence, missing,
            )
        resumed = (sequence + 1) & self._mask
        self._discard_passed(resumed)
        self._expected = self._absorb_pending(resumed)
        return False

    def _reset_to(self, sequence: int) -> None:
        """Drop all state so the next frame, whatever its number, is the baseline.

        The resetting frame is not itself taken as the baseline: the counter has
        restarted and the first numbers of the new run are as likely to be
        reordered as any others, so anchoring on one of them would manufacture a
        gap. Callers that want an exact anchor re-observe on a fresh tracker.
        """
        del sequence
        self._expected = None
        self._pending.clear()

    def _absorb_pending(self, expected: int) -> int:
        """Advance *expected* past any numbers already held that continue the run."""
        while expected in self._pending:
            self._pending.discard(expected)
            expected = (expected + 1) & self._mask
        return expected

    def _discard_passed(self, expected: int) -> None:
        """Forget held numbers that now sit behind *expected*."""
        self._pending = {
            p
            for p in self._pending
            if (p - expected) & self._mask <= self._reset_threshold
        }


def iter_frames(buf: bytearray) -> Generator[bytes, None, None]:
    """Yield raw protobuf bytes for each complete length-delimited frame in *buf*.

    Consumes *buf* in-place as frames are yielded. Incomplete frames at the end
    of *buf* are left untouched so the caller can append more data and call again.

    Args:
        buf: Mutable byte buffer; modified in-place as complete frames are consumed.

    Yields:
        Raw protobuf message bytes (without the varint length prefix).
    """
    while buf:
        msg_len, varint_size = decode_varint(buf)
        if varint_size == 0:
            break  # incomplete or malformed varint — wait for more data
        if len(buf) < varint_size + msg_len:
            break  # incomplete message body — wait for more data
        yield bytes(buf[varint_size: varint_size + msg_len])
        del buf[: varint_size + msg_len]


def decode_varint(buf: bytearray | bytes) -> tuple[int, int]:
    """Decode a LEB128 (protobuf varint) from the start of *buf*.

    Args:
        buf: Buffer whose first bytes encode a varint.

    Returns:
        A ``(value, n_bytes_consumed)`` pair.  Returns ``(0, 0)`` if *buf* is
        empty, incomplete, or contains a malformed varint (shift overflow).
    """
    value = 0
    shift = 0
    for i, byte in enumerate(buf):
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, i + 1
        shift += 7
        if shift >= 64:
            return 0, 0  # malformed
    return 0, 0  # incomplete


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as LEB128 (protobuf varint).

    Args:
        value: Non-negative integer to encode.

    Returns:
        Bytes encoding *value* as a variable-length unsigned integer.

    Raises:
        ValueError: If *value* is negative.
    """
    if value < 0:
        raise ValueError(f"encode_varint requires a non-negative integer, got {value}")
    buf = []
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            buf.append(byte | 0x80)
        else:
            buf.append(byte)
            break
    return bytes(buf)
