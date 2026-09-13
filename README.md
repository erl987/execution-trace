# Embedded execution tracing with transport-agnostic span/marker recording

The graphical visualization and analysis tool is available in the companion Python package
[`embedded-etrace`](https://pypi.org/project/embedded-etrace) and can be easily installed.

Records *named time spans* (task activations, ISR executions) and *point-in-time markers* on
**bare-metal targets without a heap or OS**. 

Events are serialized as length-delimited protobuf frames that you forward over any byte 
transport — RTT, UART, USB, or a ring buffer **for post-mortem analysis in the GUI**.

## Example of a time trace diagram
![execution trace timing diagram](https://raw.githubusercontent.com/erl987/execution-trace/main/docs/screenshot_python_app_1.png)

## Quick start

### 1. Implement `TraceTransport` and `TraceSink` for your transport

`TraceTransport` is the low-level primitive: it receives a pre-constructed [`TraceEvent`] and
forwards it over your chosen transport. `TraceSink` is the recording layer built on top: it reads
the hardware clock and calls the `record_*` helpers.

Types that only move pre-built events (e.g., a downstream serialiser driven by a task queue)
implement `TraceTransport` alone. Types that also originate recordings implement both.

```rust
use execution_trace::{TraceTransport, TraceSink, TracingError, TraceEvent};

struct MyRttSink { /* ... */ }

impl TraceTransport for MyRttSink {
    fn write_event(&mut self, event: TraceEvent) -> Result<(), TracingError> {
        // encode and forward the event bytes over your chosen transport
        Ok(())
    }
}

impl TraceSink for MyRttSink {
    fn get_elapsed_nanoseconds(&self) -> u64 {
        0 // replace with your hardware timer
    }
}
```

### 2. Record spans and markers

```rust
use execution_trace::{TraceSink, SourceType};

fn my_isr(sink: &mut impl TraceSink) {
    // at priority 8, with a relative deadline of 10 ms from activation
    sink.record_span_start("my_isr", SourceType::Isr, 8, Some(10.0)).ok();
    // ... work ...
    sink.record_span_end("my_isr").ok();
}

fn ukf_step(sink: &mut impl TraceSink) {
    // with a payload of 3 (e.g., an iteration counter value or similar)
    sink.record_marker("predict", Some(3)).ok();
    // ...
}
```

### 3. Encode with `TraceEncoder`

[`TraceEncoder`] holds the three pieces of per-stream state the wire format needs: the name
dictionary, the timestamp base the deltas are taken against, and the sequence counter that
lets the host detect dropped frames. Emit the stream header once, then encode events:

```rust
use execution_trace::{SourceType, TraceEncoder, TraceEvent};
use execution_trace::encode::{MAX_TRACE_BURST_SIZE, TimeBase};

let mut enc = TraceEncoder::new();
let mut buf = [0u8; MAX_TRACE_BURST_SIZE];

// Once, at startup: declares the tick unit and the active source mask.
if let Ok(n) = enc.encode_trace_start(0, TimeBase::Nanoseconds, 0, 0x1F, &mut buf) {
    let _ = &buf[..n];
}

let mut name = heapless::String::<32>::new();
name.push_str("my_task").unwrap();
let event = TraceEvent::SpanStart {
    timestamp_ns: 1_000,
    name,
    source_type: SourceType::Task,
    sequence: 0,
    priority: 4,
    relative_deadline_ms: None,
};
if let Ok(encoded) = enc.encode(&event, &mut buf) {
    if encoded.name_registry_full {
        // The dictionary is full: the event went out with the reserved "unknown"
        // id. Count it — the stream stays decodable, but this name is lost.
    }
    // forward buf[..encoded.len] over your transport (RTT, UART, USB, etc.)
    let _ = &buf[..encoded.len];
}
```

The first sight of a name writes **two** frames — the dictionary entry, then the event — which
is why the buffer is [`encode::MAX_TRACE_BURST_SIZE`] rather than one frame.

### 4. Decode on the host

A frame is not self-contained: its name is a dictionary id and its timestamp is a delta against
the previous frame. [`decode_trace_frame`] therefore returns a [`RawTraceFrame`], and the reader
resolves both from state it carries across the stream:

```rust,ignore
use execution_trace::{FrameKind, encode::decode_trace_frame};

// raw_bytes arrives from your transport (RTT, UART, file, etc.)
let (frame, consumed) = decode_trace_frame(raw_bytes).unwrap();

// A dictionary entry or the stream header carries an absolute timestamp and
// re-establishes the origin; every other frame is a delta against it.
clock = match frame.kind {
    FrameKind::NameRegistered | FrameKind::TraceStart => frame.timestamp_ticks,
    _ => clock + frame.timestamp_ticks,
};
if frame.kind == FrameKind::NameRegistered {
    names.insert(frame.name_id, frame.name);
}
```

`examples/simulate.rs` carries a complete worked reader; the `execution-trace` Python package
does the same job and renders a timing diagram from it.

## Wire format

Each frame is a standard protobuf length-delimited record:

```text
[ varint: payload byte count ][ protobuf-encoded TraceFrame ]
```

One flat message carries every frame class, discriminated by its `event_type`: the three
per-occurrence events, the dictionary entry that assigns a name its id, and the stream header.
proto3 omits unset fields, so an event pays nothing for the dictionary fields it does not use.

A steady-state event costs **11-13 bytes** — the name, the priority and the deadline are sent
once per name rather than on every occurrence, and the timestamp is a delta.

Maximum frame size is [`encode::MAX_TRACE_FRAME_SIZE`] (128 bytes), and one `encode` call writes
at most [`encode::MAX_TRACE_BURST_SIZE`]. Name strings are capped at 32 bytes; longer names cause
`record_*` to return [`TracingError::MessageDropped`] before sending. The dictionary holds
[`encode::NAME_REGISTRY_CAPACITY`] distinct names, after which events fall back to a reserved
"unknown" id rather than to a wrong decode.

## Host-side tooling

The companion Python package `embedded-etrace` decodes the binary stream, matches span start/end pairs,
and renders a zoomable Bokeh timing diagram as a standalone HTML file. The intermediate format is
a CSV with columns `name`, `type`, `start_us`, `end_us`, `priority`, `deadline_us`, and `value`.

## End-to-end example

The `examples/` directory contains a self-contained simulation that demonstrates the full
workflow without any hardware:

```bash
# 1. Install the Python package with diagram support
pip install embedded-etrace[diagram]

# 2. Simulate an embedded trace and write it to trace.bin
cargo run --example simulate --features std

# 3. Decode the binary trace and render an interactive HTML timing diagram
python examples/visualize.py
```

`simulate` records two control-loop iterations — a `gyro_isr` (ISR, priority 8) and a
`control_task` (task, priority 4) with `ukf_predict`/`ukf_update` markers — where the
first iteration meets its deadline and the second misses it. `visualize.py` reads
`trace.bin`, writes a temporary CSV, and produces `diagram.html` that you can open in any
browser.

Both scripts accept `--help` for available options (e.g. `--input`, `--output`, `--title`
for `visualize.py`).

## `no_std` usage

The crate is `no_std` by default. Enable the `std` feature for tests:

```toml
[dev-dependencies]
execution-trace = { version = "0.1", features = ["std"] }
```

## Compile-time on/off switch (`enabled` feature)

The `enabled` feature (on by default) gates the entire implementation. When you disable it, every
`TraceSink` method becomes a zero-cost no-op and the compiler eliminates every call site — no
overhead in production builds, no `#[cfg]` guards in your application code.

```toml
# your crate's Cargo.toml
[features]
trace = ["execution-trace/enabled"]

[dependencies]
execution-trace = { version = "0.1", default-features = false }
```

Then pass `--features trace` (or your own feature name) to enable tracing for a specific build:

```bash
cargo build --features trace
```

Your call sites require no changes: `record_span_start`, `record_span_end`, and `record_marker`
are always present on `TraceSink` regardless of the feature flag — they just compile away when
`enabled` is off.
