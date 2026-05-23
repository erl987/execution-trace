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

# A "drop" count exceeding half the u32 range means the sequence went backward —
# a device reset rather than actual packet loss.
RESET_THRESHOLD: int = 1 << 31


class SequenceTracker:
    """Detects device resets by watching for backwards sequence number jumps.

    Embedded firmware typically increments a 32-bit sequence counter with every
    frame. When the device resets (panic, watchdog, or reflash), the counter
    starts over at zero on the same connection. This class distinguishes a reset
    (counter jumped backward by more than half the u32 range) from ordinary
    packet loss (small forward gap).

    Args:
        label: Human-readable stream name used in log messages.
    """

    def __init__(self, label: str = "Frame") -> None:
        self._last: int | None = None
        self._label = label

    def observe(self, sequence: int) -> bool:
        """Record a sequence number and detect resets or drops.

        Args:
            sequence: The 32-bit sequence number from the received frame.

        Returns:
            ``True`` if a device reset was detected (sequence jumped backward),
            ``False`` for normal sequential frames or ordinary packet drops.

        Note:
            After a reset is detected, ``_last`` is cleared so the very next
            frame — whatever its sequence number — is accepted silently as the
            new baseline.
        """
        # Use & 0xFFFFFFFF to emulate unsigned 32-bit counter math in Python
        # (mod 2^32), so increment/subtraction behave correctly across counter
        # wraparound.
        if self._last is not None:
            expected = (self._last + 1) & 0xFFFFFFFF
            if sequence != expected:
                dropped = (sequence - expected) & 0xFFFFFFFF
                if dropped > RESET_THRESHOLD:
                    logger.warning(
                        "%s: device reset detected, resuming from #%d",
                        self._label, sequence,
                    )
                    self._last = None
                    return True
                logger.warning(
                    "%s drop detected: expected #%d, got #%d (%d dropped)",
                    self._label, expected, sequence, dropped,
                )
        self._last = sequence
        return False


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
