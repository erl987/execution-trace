"""etrace-decode — decode a raw binary execution-trace file to CSV.

The binary file must contain length-delimited protobuf frames as produced by
the ``execution_trace`` Rust crate (each frame is a varint-prefixed
serialised ``TraceEvent`` protobuf).

Usage::

    etrace-decode recording.bin
    etrace-decode recording.bin --output my_trace.csv
"""

import argparse
import sys
from pathlib import Path
from typing import NoReturn

from execution_trace.decode import (
    TraceEventBuffer,
    decode_tracing_stream,
    write_tracing_csv,
)
from execution_trace.stream import SequenceTracker


def _die(msg: str) -> NoReturn:
    print(f"[error] {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    """Entry point for the ``etrace-decode`` console script."""
    ap = argparse.ArgumentParser(
        description=(
            "Decode a raw binary execution-trace file (length-delimited protobuf "
            "frames) and write span/marker records to a CSV file."
        ),
    )
    ap.add_argument(
        "binary",
        help="Path to the raw binary trace file",
    )
    ap.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help=(
            "Directory to write the CSV into (default: same directory as the "
            "input file, or 'data' if stdin is used)"
        ),
    )
    args = ap.parse_args()

    binary_path = Path(args.binary)
    if not binary_path.exists():
        _die(f"File not found: {binary_path}")
    if not binary_path.is_file():
        _die(f"Not a file: {binary_path}")

    output_dir = args.output if args.output is not None else str(binary_path.parent)

    buf = bytearray(binary_path.read_bytes())
    tracker = SequenceTracker("etrace-decode")
    event_buffer = TraceEventBuffer()
    decode_tracing_stream(buf, tracker, event_buffer)
    event_buffer.flush_pending()

    records = event_buffer.records + event_buffer.markers
    csv_path = write_tracing_csv(records, output_dir=output_dir)

    if csv_path is None:
        print("[warning] No completed trace records found — CSV not written.", file=sys.stderr)
        sys.exit(1)

    print(f"Written → {csv_path}  ({len(records)} records)")


if __name__ == "__main__":
    main()
