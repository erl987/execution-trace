"""embedded-etrace — decode and visualise no_std embedded execution traces.

Public API re-exported for convenience::

    from execution_trace import (
        SequenceTracker, iter_frames, encode_varint, decode_varint,
        TraceEvent, MarkerRecord, GapRecord, NameEntry, TraceEventBuffer, TraceStreamState,
        decode_tracing_stream, write_tracing_csv,
    )

The :mod:`execution_trace.diagram` module is **not** re-exported here because it
requires the optional ``[diagram]`` extra (``pip install embedded-etrace[diagram]``).
Import it directly when needed::

    from execution_trace.diagram import generate_diagram
"""

from execution_trace.decode import (
    SEQUENCE_MODULUS,
    GapRecord,
    MarkerRecord,
    NameEntry,
    TraceEvent,
    TraceEventBuffer,
    TraceStreamState,
    decode_tracing_stream,
    write_tracing_csv,
)
from execution_trace.stream import (
    SequenceTracker,
    decode_varint,
    encode_varint,
    iter_frames,
)

__all__ = [
    "SequenceTracker",
    "iter_frames",
    "encode_varint",
    "decode_varint",
    "TraceEvent",
    "MarkerRecord",
    "GapRecord",
    "NameEntry",
    "TraceEventBuffer",
    "TraceStreamState",
    "SEQUENCE_MODULUS",
    "decode_tracing_stream",
    "write_tracing_csv",
]
