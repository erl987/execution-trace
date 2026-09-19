# embedded-etrace

Decode and visualize **embedded execution traces** over RTT or any byte transport.

The companion Rust crate (`execution_trace`) provided by the Cargo 
package [`execution-trace`](https://crates.io/crates/execution-trace)
runs on your firmware and emits length-delimited protobuf frames for task activations, 
ISR entries, and point-in-time markers. This Python package decodes those frames on the 
host and produces interactive HTML timing diagrams.

## Example of a time trace diagram
![execution trace timing diagram](https://raw.githubusercontent.com/erl987/execution-trace/main/docs/screenshot_python_app_1.png)

## Installation

```bash
# Core decoder only (no diagram dependencies):
pip install embedded-etrace

# With the interactive Bokeh diagram generator:
pip install embedded-etrace[diagram]
```

Requires Python 3.11+.

## Quick start

### 1. Custom transport — decode a live stream

Implement a loop that appends raw bytes from your transport (RTT, UART, TCP…) into a
`bytearray` and call `decode_tracing_stream` each iteration:

```python
from execution_trace import (
    TraceEventBuffer, TraceStreamState, decode_tracing_stream, write_tracing_csv,
)

buf = bytearray()
state = TraceStreamState("my-device")               # one per connection
event_buffer = TraceEventBuffer()

while True:
    buf += transport.read()                         # append whatever arrived
    decode_tracing_stream(buf, state, event_buffer)

# When the session ends:
event_buffer.flush_pending()                        # warn about open spans
csv_path = write_tracing_csv(
    [*event_buffer.records, *event_buffer.markers, *event_buffer.gaps],
    output_dir="data",
)
print(f"Trace written → {csv_path}")
```

`decode_tracing_stream` consumes complete frames from `buf` in-place and handles
device resets transparently (the sequence number jumps backward on firmware reboot).

`TraceStreamState` is what makes the decode stateful, and it must be the **same object
across every call for one connection**: a frame carries a dictionary id rather than a
name and a delta rather than a timestamp, so the name table and the running clock live
there. Create a fresh one per connection — the device restarts both when it does.

Pass `event_buffer.gaps` to the CSV writer as above. A gap is a stretch where the
sequence numbers say frames were lost; writing those rows is what keeps missing data
looking missing rather than silently closing up.

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
| `name`         | string  | Span or marker name; `trace gap` on a gap row    |
| `type`         | string  | `task`, `isr`, `marker`, or `gap`                |
| `start_us`     | float   | Activation timestamp in µs; for a gap, the last frame before it |
| `end_us`       | float   | Completion timestamp in µs (= `start_us` for markers); for a gap, the first frame after it |
| `priority`     | int     | Scheduler priority (0 if unknown)                |
| `deadline_us`  | float   | Absolute deadline in µs (empty when absent)      |
| `value`        | int     | Optional u32 marker payload; frames lost on a gap row |
| `interrupted`  | int     | `1` when a gap cut the span short, so its real end is unknown and at least `end_us` |

An empty `deadline_us` or `value` cell means the field was **absent**. A `0` is a real
payload and is written as `0`.

## Wire format

Each frame is a standard protobuf length-delimited record:

```
[ varint: payload_length ][ proto bytes: TraceFrame ]
```

A frame is **not self-contained**, which is why the decoder is stateful. `TraceFrame` is
one flat message discriminated by `event_type`, and the stream carries three kinds:

- `TRACE_START`, once at the head of the stream, declaring the timebase (nanoseconds or
  raw core cycles plus the core frequency) and the source-group mask the firmware was
  built with. A group absent from the mask is silent by design — that is what tells it
  apart from a group whose frames were lost.
- `NAME_REGISTERED`, once per distinct name, assigning the dictionary id and everything
  fixed about that span or marker: its source type, priority and relative deadline.
- `SPAN_START`, `SPAN_END` and `MARKER`, which carry only a dictionary id, a signed
  timestamp delta against the previous frame, and a sequence number wrapping at 16384.

`TRACE_START` and `NAME_REGISTERED` carry an **absolute** timestamp and re-establish the
time origin; every other frame is a delta against it. The delta is signed because an
event is stamped when recorded rather than when queued, so a preempting ISR can put a
later timestamp ahead of an earlier one.

This is **v2, and it is not compatible with the v1 format** that `embedded-etrace` 0.1.x
decoded. A v2 stream is recognised by its leading `TRACE_START` frame.

The protobuf schema lives in `proto/tracing.proto` in the
[repository](https://github.com/erl987/execution-trace), alongside the Rust crate.

## Development

```bash
# Editable install with all dev dependencies:
pip install -e ".[diagram,dev]"

# Run the test suite:
pytest tests/ -v

# Type-check:
mypy src/execution_trace
```
