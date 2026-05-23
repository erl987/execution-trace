# execution-trace

Decode and visualise **no_std embedded execution traces** over RTT or any byte transport.

The companion Rust crate (`execution-trace`) runs on your firmware and emits
length-delimited protobuf frames for task activations, ISR entries, and point-in-time
markers. This Python package decodes those frames on the host and produces interactive
HTML timing diagrams.

![screenshot_python_app_1.png](../docs/screenshot_python_app_1.png)

## Installation

```bash
# Core decoder only (no diagram dependencies):
pip install execution-trace

# With the interactive Bokeh diagram generator:
pip install execution-trace[diagram]
```

Requires Python 3.11+.

## Quick start

### 1. Custom transport — decode a live stream

Implement a loop that appends raw bytes from your transport (RTT, UART, TCP…) into a
`bytearray` and call `decode_tracing_stream` each iteration:

```python
from execution_trace import (
    SequenceTracker, TraceEventBuffer, decode_tracing_stream, write_tracing_csv,
)

buf = bytearray()
tracker = SequenceTracker("my-device")
event_buffer = TraceEventBuffer()

while True:
    buf += transport.read()                         # append whatever arrived
    decode_tracing_stream(buf, tracker, event_buffer)

# When the session ends:
event_buffer.flush_pending()                        # warn about open spans
csv_path = write_tracing_csv(
    event_buffer.records + event_buffer.markers,    # type: ignore[operator]
    output_dir="data",
)
print(f"Trace written → {csv_path}")
```

`decode_tracing_stream` consumes complete frames from `buf` in-place and handles
device resets transparently (the sequence number jumps backward on firmware reboot).

### 2. Decode a recorded binary file

Use the `etrace-decode` console script:

```bash
etrace-decode recording.bin
etrace-decode recording.bin --output ./data
```

This produces a timestamped `trace_YYYYMMDD_HHMMSS.csv` and updates the
`trace_latest.csv` symlink.

### 3. Generate a timing diagram

```bash
etrace-diagram                          # reads trace_latest.csv, writes diagram.html
etrace-diagram data/trace_latest.csv --output out/diagram.html --title "Flight run #7"
```

Or call the API directly:

```python
from pathlib import Path
from execution_trace.diagram import generate_diagram

result = generate_diagram(
    csv_path=Path("trace_latest.csv"),
    output_path=Path("diagram.html"),
    title="Flight run #7",
)
print(f"{result.n_events} events, {result.n_lanes} lanes, {result.n_missed} deadline misses")
```

## CSV format

| Column         | Type    | Description                                      |
|----------------|---------|--------------------------------------------------|
| `name`         | string  | Span or marker name                              |
| `type`         | string  | `task`, `isr`, or `marker`                       |
| `start_us`     | float   | Activation timestamp in µs                       |
| `end_us`       | float   | Completion timestamp in µs (= `start_us` for markers) |
| `priority`     | int     | Scheduler priority (0 if unknown)                |
| `deadline_us`  | float   | Absolute deadline in µs (optional)               |
| `value`        | int     | Optional u32 marker payload                      |

## Wire format

Each frame is a standard protobuf length-delimited record:

```
[ varint: payload_length ][ proto bytes: TraceEvent ]
```

The protobuf schema lives in :file:`execution-trace/proto/tracing.proto` (sibling
Rust crate in the same repository).

## Development

```bash
# Editable install with all dev dependencies:
pip install -e ".[diagram,dev]"

# Run the test suite:
pytest tests/ -v

# Type-check:
mypy src/execution_trace
```
