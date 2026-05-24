// Simulate an embedded execution trace on the desktop and write it to `trace.bin`.
//
// Run:
//   cargo run --example simulate --features std
//
// This produces `trace.bin` in the current working directory. To render a timing
// diagram from it, run the companion Python script:
//   python examples/visualize.py

use execution_trace::encode::{MAX_TRACE_FRAME_SIZE, decode_trace_frame};
use execution_trace::{
    SequenceEncoder, SourceType, TraceEvent, TraceSink, TraceTransport, TracingError,
};

// A sink that encodes each event into a byte buffer that can be written to a file or
// forwarded over a transport (RTT, UART, USB). On real hardware this would wrap the
// transport driver; here it wraps a Vec<u8> so we can write the bytes to disk.
struct FileSink {
    buf: Vec<u8>,
    encoder: SequenceEncoder,
    pub tick_ns: u64,
}

impl FileSink {
    fn new() -> Self {
        Self {
            buf: Vec::new(),
            encoder: SequenceEncoder::new(),
            tick_ns: 0,
        }
    }

    fn into_bytes(self) -> Vec<u8> {
        self.buf
    }
}

impl TraceTransport for FileSink {
    fn write_event(&mut self, event: TraceEvent) -> Result<(), TracingError> {
        let mut frame = [0u8; MAX_TRACE_FRAME_SIZE];
        let n = self
            .encoder
            .encode(&event, &mut frame)
            .map_err(|_| TracingError::MessageDropped)?;
        self.buf.extend_from_slice(&frame[..n]);
        Ok(())
    }
}

impl TraceSink for FileSink {
    fn get_elapsed_nanoseconds(&self) -> u64 {
        self.tick_ns
    }
}

fn main() -> std::io::Result<()> {
    let mut sink = FileSink::new();

    // --- Simulated timeline (nanosecond timestamps, two control-loop iterations) ---
    //
    //  0.0 ms   gyro_isr  START  (ISR, priority 8)
    //  0.4 ms   gyro_isr  END
    //  1.0 ms   control_task START  (TASK, priority 4, relative deadline 10 ms)
    //  2.0 ms   ukf_predict MARKER
    //  3.0 ms   gyro_isr  START  (second activation)
    //  3.4 ms   gyro_isr  END
    //  4.5 ms   ukf_update  MARKER  (value = iteration counter)
    //  7.0 ms   control_task END                         ← meets deadline (7 ms < 1+10 ms)
    // 10.0 ms   gyro_isr  START  (third activation, second loop)
    // 10.4 ms   gyro_isr  END
    // 11.0 ms   control_task START  (relative deadline 7 ms)
    // 12.0 ms   ukf_predict MARKER
    // 13.5 ms   ukf_update  MARKER  (value = 2)
    // 18.5 ms   control_task END                         ← misses deadline (18.5 ms > 11+7.0 ms)

    let ms = 1_000_000u64; // nanoseconds per millisecond

    sink.tick_ns = 0;
    sink.record_span_start("gyro_isr", SourceType::Isr, 8, None)
        .ok();

    sink.tick_ns = 400 * (ms / 1000);
    sink.record_span_end("gyro_isr").ok();

    sink.tick_ns = ms;
    sink.record_span_start("control_task", SourceType::Task, 4, Some(10.0))
        .ok();

    sink.tick_ns = 2 * ms;
    sink.record_marker("ukf_predict", None).ok();

    sink.tick_ns = 3 * ms;
    sink.record_span_start("gyro_isr", SourceType::Isr, 8, None)
        .ok();

    sink.tick_ns = 3 * ms + 400 * (ms / 1000);
    sink.record_span_end("gyro_isr").ok();

    sink.tick_ns = 4 * ms + 500 * (ms / 1000);
    sink.record_marker("ukf_update", Some(1)).ok();

    sink.tick_ns = 7 * ms;
    sink.record_span_end("control_task").ok();

    // Second loop iteration
    sink.tick_ns = 10 * ms;
    sink.record_span_start("gyro_isr", SourceType::Isr, 8, None)
        .ok();

    sink.tick_ns = 10 * ms + 400 * (ms / 1000);
    sink.record_span_end("gyro_isr").ok();

    sink.tick_ns = 11 * ms;
    sink.record_span_start("control_task", SourceType::Task, 4, Some(7.0))
        .ok();

    sink.tick_ns = 12 * ms;
    sink.record_marker("ukf_predict", None).ok();

    sink.tick_ns = 13 * ms + 500 * (ms / 1000);
    sink.record_marker("ukf_update", Some(2)).ok();

    sink.tick_ns = 18 * ms + 500 * (ms / 1000);
    sink.record_span_end("control_task").ok();

    // --- Write encoded frames to trace.bin ---
    let bytes = sink.into_bytes();
    std::fs::write("trace.bin", &bytes)?;
    println!(
        "Wrote {} bytes ({} frames) to trace.bin",
        bytes.len(),
        count_frames(&bytes)
    );

    // --- Decode and print each event (round-trip verification) ---
    println!("\nDecoded events:");
    println!(
        "{:<6} {:<14} {:<12} {:<10} {:<8} {}",
        "seq", "name", "type", "source", "ts_ms", "extras"
    );
    println!("{}", "-".repeat(72));
    let mut pos = 0;
    while pos < bytes.len() {
        match decode_trace_frame(&bytes[pos..]) {
            Ok((event, consumed)) => {
                match &event {
                    TraceEvent::SpanStart {
                        sequence,
                        name,
                        source_type,
                        timestamp_ns,
                        relative_deadline_ms,
                        ..
                    } => {
                        let ts_ms = *timestamp_ns as f64 / 1_000_000.0;
                        let source = match source_type {
                            SourceType::Isr => "ISR",
                            SourceType::Task => "Task",
                        };
                        let mut extras = String::new();
                        if let Some(dl) = relative_deadline_ms {
                            extras.push_str(&format!("rel_deadline={dl:.1}ms "));
                        }
                        println!(
                            "{:<6} {:<14} {:<12} {:<10} {:<8.3} {}",
                            sequence,
                            name.as_str(),
                            "SpanStart",
                            source,
                            ts_ms,
                            extras,
                        );
                    }
                    TraceEvent::SpanEnd {
                        sequence,
                        name,
                        timestamp_ns,
                    } => {
                        let ts_ms = *timestamp_ns as f64 / 1_000_000.0;
                        println!(
                            "{:<6} {:<14} {:<12} {:<10} {:<8.3}",
                            sequence,
                            name.as_str(),
                            "SpanEnd",
                            "-",
                            ts_ms,
                        );
                    }
                    TraceEvent::Marker {
                        sequence,
                        name,
                        timestamp_ns,
                        marker_value,
                    } => {
                        let ts_ms = *timestamp_ns as f64 / 1_000_000.0;
                        let mut extras = String::new();
                        if let Some(v) = marker_value {
                            extras.push_str(&format!("value={v}"));
                        }
                        println!(
                            "{:<6} {:<14} {:<12} {:<10} {:<8.3} {}",
                            sequence,
                            name.as_str(),
                            "Marker",
                            "-",
                            ts_ms,
                            extras,
                        );
                    }
                }
                pos += consumed;
            }
            Err(e) => {
                eprintln!("decode error at byte {pos}: {e:?}");
                break;
            }
        }
    }

    Ok(())
}

fn count_frames(buf: &[u8]) -> usize {
    let mut pos = 0;
    let mut count = 0;
    while pos < buf.len() {
        match decode_trace_frame(&buf[pos..]) {
            Ok((_, consumed)) => {
                pos += consumed;
                count += 1;
            }
            Err(_) => break,
        }
    }
    count
}
