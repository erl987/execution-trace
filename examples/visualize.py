#!/usr/bin/env python3
"""Decode a binary trace file and render an interactive Bokeh timing diagram.

Prerequisites:
    pip install execution-trace[diagram]

Usage:
    python examples/visualize.py [--input trace.bin] [--output diagram.html] [--title TITLE]

Typical workflow:
    cargo run --example simulate --features std   # produces trace.bin
    python examples/visualize.py                   # produces diagram.html
"""

import argparse
import tempfile
from pathlib import Path

from execution_trace import (
    SequenceTracker,
    TraceEventBuffer,
    decode_tracing_stream,
    write_tracing_csv,
)
from execution_trace.diagram import generate_diagram


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decode a binary execution trace and render a Bokeh diagram."
    )
    parser.add_argument(
        "--input",
        default="../trace.bin",
        metavar="FILE",
        help="Binary trace file produced by the 'simulate' Rust example (default: ../trace.bin)",
    )
    parser.add_argument(
        "--output",
        default="diagram.html",
        metavar="FILE",
        help="Output HTML file for the timing diagram (default: diagram.html)",
    )
    parser.add_argument(
        "--title",
        default="Execution Trace",
        help="Diagram title (default: 'Execution Trace')",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise SystemExit(
            f"Input file not found: {input_path}\n"
            "Run 'cargo run --example simulate --features std' first."
        )

    buf = bytearray(input_path.read_bytes())
    print(f"Read {len(buf)} bytes from {input_path}")

    tracker = SequenceTracker("simulate")
    event_buffer = TraceEventBuffer()
    decode_tracing_stream(buf, tracker, event_buffer)
    event_buffer.flush_pending()

    records = event_buffer.records + event_buffer.markers
    print(f"Decoded {len(event_buffer.records)} span(s) and {len(event_buffer.markers)} marker(s)")

    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = write_tracing_csv(records, output_dir=tmp_dir)
        if csv_path is None:
            raise SystemExit("No events to render — trace file may be empty or malformed.")

        result = generate_diagram(
            csv_path=Path(csv_path),
            output_path=output_path,
            title=args.title,
        )

    print(
        f"Diagram written to {output_path} "
        f"({result.n_events} events, {result.n_lanes} lanes, {result.n_missed} deadline misses)"
    )


if __name__ == "__main__":
    main()
