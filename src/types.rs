/// Whether the event source is an ISR or a task.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SourceType {
    /// Hardware interrupt service routine.
    Isr,
    /// Scheduled task (e.g. RTIC task, FreeRTOS task).
    Task,
}

/// A single execution-trace event recorded by the embedded device.
///
/// The variant encodes the kind of event; fields differ by variant to make
/// invalid states (e.g. `marker_value` on a span) unrepresentable.
#[cfg(feature = "enabled")]
#[derive(Debug, Clone, PartialEq)]
pub enum TraceEvent {
    /// Start of a named execution span.
    SpanStart {
        timestamp_ns: u64,
        name: heapless::String<32>,
        source_type: SourceType,
        sequence: u32,
        priority: u32,
        /// Deadline relative to span activation, in milliseconds.
        relative_deadline_ms: Option<f32>,
    },
    /// End of a named execution span previously opened with [`TraceEvent::SpanStart`].
    ///
    /// The `name` must match the corresponding `SpanStart` so the host decoder can
    /// pair them.
    SpanEnd {
        timestamp_ns: u64,
        name: heapless::String<32>,
        sequence: u32,
    },
    /// Point-in-time annotation. No matching `SpanEnd` is needed.
    Marker {
        timestamp_ns: u64,
        name: heapless::String<32>,
        sequence: u32,
        /// Optional u32 payload shown in the diagram tooltip.
        marker_value: Option<u32>,
    },
}
