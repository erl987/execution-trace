use crate::{TraceEvent, TraceEventSourceType, TraceEventType};
use heapless::String;

/// Errors that a [`TraceSink`] can return.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TracingError {
    /// The underlying channel or buffer was full; the event was discarded.
    MessageDropped,
    /// The receiving end of the channel has been dropped.
    Closed,
    /// The transport-specific send operation failed.
    SendFailed,
}

/// Receives [`TraceEvent`]s produced by instrumented code.
///
/// # Design
///
/// `TraceSink` operates in two layers:
///
/// 1. **Event layer** — `record_span_start`, `record_span_end`, and `record_marker` construct
///    [`TraceEvent`]s and hand them to `try_send`. Sequence numbers are left at zero here;
///    they are injected by the encoder.
/// 2. **Encoder/transport layer** — code that owns the wire (e.g. [`SequenceEncoder`]) calls
///    [`encode_trace_frame`] with a monotonically increasing sequence counter before writing
///    bytes to RTT, UART, etc. The sequence allows the host decoder to detect dropped frames.
///
/// For tests, use [`NoopSink`], which discards all events at zero cost.
///
/// [`SequenceEncoder`]: crate::SequenceEncoder
/// [`encode_trace_frame`]: crate::encode::encode_trace_frame
pub trait TraceSink {
    /// The primitive implementors must provide: forward `message` to the transport.
    ///
    /// All `record_*` methods build a [`TraceEvent`] and call this. Return
    /// [`TracingError::MessageDropped`] when the channel is full, or another variant on a
    /// hard failure.
    ///
    /// # Errors
    /// Propagates whatever [`TracingError`] variant is appropriate for the transport.
    fn try_send(&mut self, message: TraceEvent) -> Result<(), TracingError>;

    /// Returns the current monotonic time in nanoseconds.
    ///
    /// The default implementation returns `0`. Override this with a hardware timer read so that
    /// recorded timestamps are meaningful.
    fn get_elapsed_nanoseconds(&self) -> u64 {
        0
    }

    /// Records the start of a named execution span.
    ///
    /// - `source_name`: task or ISR identifier, max 32 bytes. Longer names return
    ///   [`TracingError::MessageDropped`] immediately.
    /// - `source_type`: whether the caller is an [`Isr`] or a [`Task`].
    /// - `priority`: scheduler priority (e.g. RTIC task priority 1–9).
    /// - `deadline_ms`: optional absolute deadline; enables missed-deadline highlighting in the
    ///   diagram.
    ///
    /// [`Isr`]: TraceEventSourceType::Isr
    /// [`Task`]: TraceEventSourceType::Task
    ///
    /// # Errors
    /// Returns [`TracingError::MessageDropped`] if the name exceeds 32 bytes.
    /// Otherwise propagates whatever `try_send` returns.
    fn record_span_start(
        &mut self,
        source_name: &'static str,
        source_type: TraceEventSourceType,
        priority: u8,
        deadline_ms: Option<f32>,
    ) -> Result<(), TracingError> {
        let mut name: String<32> = String::new();
        name.push_str(source_name)
            .map_err(|_| TracingError::MessageDropped)?;
        let mut msg = TraceEvent {
            timestamp_ns: self.get_elapsed_nanoseconds(),
            name,
            source_type,
            event_type: TraceEventType::SpanStart,
            sequence: 0,
            priority: u32::from(priority),
            ..Default::default()
        };
        if let Some(dl) = deadline_ms {
            msg.set_deadline_ms(dl);
        }
        self.try_send(msg)
    }

    /// Records the end of a named execution span previously started with [`record_span_start`].
    ///
    /// `source_name` must match the corresponding [`record_span_start`] call so the host decoder
    /// can pair them correctly. Source type and priority are inferred from the start event.
    ///
    /// [`record_span_start`]: TraceSink::record_span_start
    ///
    /// # Errors
    /// Returns [`TracingError::MessageDropped`] if the name exceeds 32 bytes.
    /// Otherwise propagates whatever `try_send` returns.
    fn record_span_end(&mut self, source_name: &'static str) -> Result<(), TracingError> {
        let mut name: String<32> = String::new();
        name.push_str(source_name)
            .map_err(|_| TracingError::MessageDropped)?;
        self.try_send(TraceEvent {
            timestamp_ns: self.get_elapsed_nanoseconds(),
            name,
            event_type: TraceEventType::SpanEnd,
            sequence: 0,
            ..Default::default()
        })
    }

    /// Records a point-in-time annotation. No matching `record_span_end` is needed.
    ///
    /// Markers appear as vertical tick lines in the timing diagram, overlaid on the
    /// lane of the highest-priority span active at that timestamp.
    ///
    /// - `label`: annotation name, max 32 bytes.
    /// - `value`: optional u32 payload shown in the diagram tooltip.
    ///
    /// # Errors
    /// Returns [`TracingError::MessageDropped`] if the label exceeds 32 bytes.
    /// Otherwise propagates whatever `try_send` returns.
    fn record_marker(
        &mut self,
        label: &'static str,
        value: Option<u32>,
    ) -> Result<(), TracingError> {
        let mut name: String<32> = String::new();
        name.push_str(label)
            .map_err(|_| TracingError::MessageDropped)?;
        let mut msg = TraceEvent {
            timestamp_ns: self.get_elapsed_nanoseconds(),
            name,
            event_type: TraceEventType::Marker,
            ..Default::default()
        };
        if let Some(v) = value {
            msg.set_marker_value(v);
        }
        self.try_send(msg)
    }
}

/// A [`TraceSink`] that discards all events. Zero-cost in release builds.
///
/// Useful as a placeholder in unit tests where tracing output is irrelevant.
pub struct NoopSink;

impl TraceSink for NoopSink {
    fn try_send(&mut self, _: TraceEvent) -> Result<(), TracingError> {
        Ok(())
    }
}

#[cfg(all(test, feature = "std"))]
mod tests {
    use super::*;

    struct CaptureSink {
        messages: Vec<TraceEvent>,
        timestamp_ns: u64,
    }

    impl CaptureSink {
        fn new() -> Self {
            Self {
                messages: vec![],
                timestamp_ns: 0,
            }
        }

        fn with_timestamp(ns: u64) -> Self {
            Self {
                messages: vec![],
                timestamp_ns: ns,
            }
        }
    }

    impl TraceSink for CaptureSink {
        fn try_send(&mut self, message: TraceEvent) -> Result<(), TracingError> {
            self.messages.push(message);
            Ok(())
        }

        fn get_elapsed_nanoseconds(&self) -> u64 {
            self.timestamp_ns
        }
    }

    struct ErrorSink;

    impl TraceSink for ErrorSink {
        fn try_send(&mut self, _: TraceEvent) -> Result<(), TracingError> {
            Err(TracingError::SendFailed)
        }
    }

    #[test]
    fn record_span_start_sets_span_start_event_type() {
        let mut sink = CaptureSink::new();
        sink.record_span_start("main_task", TraceEventSourceType::Task, 4, None)
            .unwrap();
        assert_eq!(sink.messages[0].event_type, TraceEventType::SpanStart);
    }

    #[test]
    fn record_span_end_sets_span_end_event_type() {
        let mut sink = CaptureSink::new();
        sink.record_span_end("main_task").unwrap();
        assert_eq!(sink.messages[0].event_type, TraceEventType::SpanEnd);
    }

    #[test]
    fn record_span_start_with_deadline_sets_deadline() {
        let mut sink = CaptureSink::new();
        sink.record_span_start("main_task", TraceEventSourceType::Task, 4, Some(0.5))
            .unwrap();
        assert_eq!(sink.messages[0].deadline_ms(), Some(&0.5_f32));
    }

    #[test]
    fn record_span_start_without_deadline_has_no_deadline() {
        let mut sink = CaptureSink::new();
        sink.record_span_start("gyro_isr", TraceEventSourceType::Isr, 8, None)
            .unwrap();
        assert_eq!(sink.messages[0].deadline_ms(), None);
    }

    #[test]
    fn record_span_end_has_no_deadline() {
        let mut sink = CaptureSink::new();
        sink.record_span_end("main_task").unwrap();
        assert_eq!(sink.messages[0].deadline_ms(), None);
    }

    #[test]
    fn record_span_start_sets_source_type_and_priority() {
        let mut sink = CaptureSink::new();
        sink.record_span_start("gyro_isr", TraceEventSourceType::Isr, 8, None)
            .unwrap();
        let msg = &sink.messages[0];
        assert_eq!(msg.source_type, TraceEventSourceType::Isr);
        assert_eq!(msg.priority, 8);
    }

    #[test]
    fn record_span_start_uses_elapsed_nanoseconds_for_timestamp() {
        let mut sink = CaptureSink::with_timestamp(12_345_678);
        sink.record_span_start("main_task", TraceEventSourceType::Task, 4, None)
            .unwrap();
        assert_eq!(sink.messages[0].timestamp_ns, 12_345_678);
    }

    #[test]
    fn record_span_end_uses_elapsed_nanoseconds_for_timestamp() {
        let mut sink = CaptureSink::with_timestamp(99_000_000);
        sink.record_span_end("led_task").unwrap();
        assert_eq!(sink.messages[0].timestamp_ns, 99_000_000);
    }

    #[test]
    fn record_span_start_name_too_long_returns_message_dropped() {
        let mut sink = CaptureSink::new();
        let result =
            sink.record_span_start("this_name_is_way_too_long_for_limit", TraceEventSourceType::Task, 2, None);
        assert_eq!(result, Err(TracingError::MessageDropped));
        assert!(sink.messages.is_empty());
    }

    #[test]
    fn record_span_end_name_too_long_returns_message_dropped() {
        let mut sink = CaptureSink::new();
        let result = sink.record_span_end("this_name_is_way_too_long_for_limit");
        assert_eq!(result, Err(TracingError::MessageDropped));
        assert!(sink.messages.is_empty());
    }

    #[test]
    fn send_error_propagated_from_record_span_start() {
        let result = ErrorSink.record_span_start("main_task", TraceEventSourceType::Task, 4, None);
        assert_eq!(result, Err(TracingError::SendFailed));
    }

    #[test]
    fn send_error_propagated_from_record_span_end() {
        let result = ErrorSink.record_span_end("main_task");
        assert_eq!(result, Err(TracingError::SendFailed));
    }

    #[test]
    fn record_marker_sets_marker_event_type() {
        let mut sink = CaptureSink::new();
        sink.record_marker("ukf_predict", None).unwrap();
        assert_eq!(sink.messages[0].event_type, TraceEventType::Marker);
    }

    #[test]
    fn record_marker_with_value_sets_marker_value() {
        let mut sink = CaptureSink::new();
        sink.record_marker("drain_done", Some(42)).unwrap();
        assert_eq!(sink.messages[0].marker_value(), Some(&42_u32));
    }

    #[test]
    fn record_marker_without_value_has_no_marker_value() {
        let mut sink = CaptureSink::new();
        sink.record_marker("ukf_predict", None).unwrap();
        assert_eq!(sink.messages[0].marker_value(), None);
    }

    #[test]
    fn record_marker_uses_elapsed_nanoseconds_for_timestamp() {
        let mut sink = CaptureSink::with_timestamp(5_000_000);
        sink.record_marker("checkpoint", None).unwrap();
        assert_eq!(sink.messages[0].timestamp_ns, 5_000_000);
    }

    #[test]
    fn record_marker_name_too_long_returns_message_dropped() {
        let mut sink = CaptureSink::new();
        let result = sink.record_marker("this_name_is_way_too_long_for_limit", None);
        assert_eq!(result, Err(TracingError::MessageDropped));
        assert!(sink.messages.is_empty());
    }

    #[test]
    fn send_error_propagated_from_record_marker() {
        let result = ErrorSink.record_marker("ukf_predict", None);
        assert_eq!(result, Err(TracingError::SendFailed));
    }

    #[test]
    fn record_marker_has_no_deadline() {
        let mut sink = CaptureSink::new();
        sink.record_marker("checkpoint", Some(1)).unwrap();
        assert_eq!(sink.messages[0].deadline_ms(), None);
    }

    #[test]
    fn record_marker_source_type_is_unspecified() {
        let mut sink = CaptureSink::new();
        sink.record_marker("checkpoint", None).unwrap();
        assert_eq!(
            sink.messages[0].source_type,
            TraceEventSourceType::Unspecified
        );
    }
}
