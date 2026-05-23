use crate::TraceEvent;
use micropb::{MessageDecode, MessageEncode, PbDecoder, PbEncoder};

pub const MAX_TRACE_FRAME_SIZE: usize = 128;

/// Errors returned by [`encode_trace_frame`] and [`SequenceEncoder::encode`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TracingEncodeError {
    /// The output buffer is too small or the encoded message exceeds [`MAX_TRACE_FRAME_SIZE`].
    BufferFull,
}

/// Encodes a [`TraceEvent`] as a length-delimited protobuf frame into `out`.
///
/// Frame format: `[varint: byte length][protobuf-encoded TraceEvent]`.
///
/// The `sequence` parameter **overwrites** the `sequence` field already present in
/// `msg`. [`TraceSink`] always produces events with `sequence = 0`; use
/// [`SequenceEncoder`] or manage the counter manually here to enable drop detection
/// on the host.
///
/// Returns the number of bytes written on success.
///
/// # Errors
/// Returns [`TracingEncodeError::BufferFull`] if `out` is too small or the message
/// exceeds [`MAX_TRACE_FRAME_SIZE`].
///
/// [`TraceSink`]: crate::TraceSink
#[allow(clippy::indexing_slicing)] // bounds-checked: n <= vec.len() <= MAX_TRACE_FRAME_SIZE <= out.len()
pub fn encode_trace_frame(
    msg: &TraceEvent,
    sequence: u32,
    out: &mut [u8],
) -> Result<usize, TracingEncodeError> {
    let proto_msg = build_proto_message(msg, sequence);

    let mut vec: heapless::Vec<u8, MAX_TRACE_FRAME_SIZE> = heapless::Vec::new();
    let mut encoder = PbEncoder::new(vec);
    proto_msg
        .encode_len_delimited(&mut encoder)
        .map_err(|_| TracingEncodeError::BufferFull)?;
    vec = encoder.into_writer();

    let n = vec.len();
    if n > out.len() {
        return Err(TracingEncodeError::BufferFull);
    }
    out[..n].copy_from_slice(&vec);
    Ok(n)
}

/// Errors returned by [`decode_trace_frame`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TracingDecodeError {
    /// The slice ends before the declared payload length.
    Truncated,
    /// The leading varint length prefix is malformed or incomplete.
    MalformedVarint,
    /// `micropb` rejected the protobuf payload bytes.
    DecodeError,
}

/// Decodes one length-delimited protobuf frame produced by [`encode_trace_frame`].
///
/// `frame` must begin with a varint-encoded byte count followed by that many bytes
/// of protobuf-encoded [`TraceEvent`]. Returns the decoded event and the total number
/// of bytes consumed (varint header + payload). Useful for host-side tooling.
///
/// # Errors
/// Returns [`TracingDecodeError`] if the frame is truncated, the varint is malformed,
/// or the protobuf payload cannot be decoded.
pub fn decode_trace_frame(frame: &[u8]) -> Result<(TraceEvent, usize), TracingDecodeError> {
    let (payload_len, header_len) =
        decode_varint(frame).ok_or(TracingDecodeError::MalformedVarint)?;
    let total = header_len + payload_len;
    if frame.len() < total {
        return Err(TracingDecodeError::Truncated);
    }
    let payload = &frame[header_len..total];
    let mut decoder = PbDecoder::new(payload);
    let mut event = TraceEvent::default();
    event
        .decode(&mut decoder, payload_len)
        .map_err(|_| TracingDecodeError::DecodeError)?;
    Ok((event, total))
}

/// Encodes [`TraceEvent`]s into a byte buffer while tracking the sequence counter.
///
/// [`TraceSink`] does not manage sequence numbers — that is the responsibility of the
/// transport layer (the code that owns the wire). Use `SequenceEncoder` when encoding
/// events manually so that the host-side decoder can detect dropped frames.
///
/// [`TraceSink`]: crate::TraceSink
pub struct SequenceEncoder {
    sequence: u32,
}

impl SequenceEncoder {
    /// Creates a new encoder with the sequence counter initialised to zero.
    pub const fn new() -> Self {
        Self { sequence: 0 }
    }

    /// Encodes `event` into `out`, injecting the current sequence number and
    /// advancing the counter. Returns the number of bytes written.
    ///
    /// # Errors
    /// Returns [`TracingEncodeError::BufferFull`] if `out` is too small.
    pub fn encode(
        &mut self,
        event: &TraceEvent,
        out: &mut [u8],
    ) -> Result<usize, TracingEncodeError> {
        let n = encode_trace_frame(event, self.sequence, out)?;
        self.sequence = self.sequence.wrapping_add(1);
        Ok(n)
    }

    /// Returns the current sequence counter value (the number of the *next* frame).
    pub fn sequence(&self) -> u32 {
        self.sequence
    }
}

impl Default for SequenceEncoder {
    fn default() -> Self {
        Self::new()
    }
}

/// Returns `(value, bytes_consumed)` for a varint at the start of `buf`,
/// or `None` if the varint is malformed or incomplete.
fn decode_varint(buf: &[u8]) -> Option<(usize, usize)> {
    let mut value: u64 = 0;
    let mut shift = 0u32;
    for (i, &byte) in buf.iter().enumerate() {
        value |= u64::from(byte & 0x7F) << shift;
        if byte & 0x80 == 0 {
            return usize::try_from(value).ok().map(|v| (v, i + 1));
        }
        shift += 7;
        if shift >= 64 {
            return None;
        }
    }
    None
}

fn build_proto_message(msg: &TraceEvent, sequence: u32) -> TraceEvent {
    let mut out = TraceEvent {
        timestamp_ns: msg.timestamp_ns,
        name: msg.name.clone(),
        source_type: msg.source_type,
        event_type: msg.event_type,
        sequence,
        priority: msg.priority,
        ..Default::default()
    };
    if let Some(&dl) = msg.r#relative_deadline_ms() {
        out.set_relative_deadline_ms(dl);
    }
    if let Some(&v) = msg.marker_value() {
        out.set_marker_value(v);
    }
    out
}

#[cfg(all(test, feature = "std"))]
mod tests {
    use super::*;
    use crate::{TraceEventSourceType, TraceEventType};
    use heapless::String;
    use insta::assert_debug_snapshot;

    fn make_span_start() -> TraceEvent {
        let mut name: String<32> = String::new();
        name.push_str("led_task").unwrap();
        TraceEvent {
            timestamp_ns: 1_000_000,
            name,
            source_type: TraceEventSourceType::Task,
            event_type: TraceEventType::SpanStart,
            sequence: 0,
            priority: 2,
            ..Default::default()
        }
    }

    fn make_span_start_with_deadline() -> TraceEvent {
        let mut msg = make_span_start();
        msg.set_relative_deadline_ms(0.5);
        msg
    }

    fn make_span_end() -> TraceEvent {
        let mut name: String<32> = String::new();
        name.push_str("gyro_isr").unwrap();
        TraceEvent {
            timestamp_ns: 2_000_000,
            name,
            source_type: TraceEventSourceType::Isr,
            event_type: TraceEventType::SpanEnd,
            sequence: 0,
            priority: 8,
            ..Default::default()
        }
    }

    fn make_marker() -> TraceEvent {
        let mut name: String<32> = String::new();
        name.push_str("ukf_predict").unwrap();
        TraceEvent {
            timestamp_ns: 1_500_000,
            name,
            event_type: TraceEventType::Marker,
            ..Default::default()
        }
    }

    fn make_marker_with_value() -> TraceEvent {
        let mut msg = make_marker();
        msg.set_marker_value(42);
        msg
    }

    fn encode(msg: &TraceEvent, seq: u32) -> ([u8; MAX_TRACE_FRAME_SIZE], usize) {
        let mut buf = [0u8; MAX_TRACE_FRAME_SIZE];
        let n = encode_trace_frame(msg, seq, &mut buf).unwrap();
        (buf, n)
    }

    fn check_frame_length(frame: &[u8]) {
        let mut value: u64 = 0;
        let mut shift = 0u32;
        for (i, &byte) in frame.iter().enumerate() {
            value |= ((byte & 0x7F) as u64) << shift;
            if byte & 0x80 == 0 {
                let header_bytes = i + 1;
                assert_eq!(
                    header_bytes + value as usize,
                    frame.len(),
                    "varint length prefix does not match frame length"
                );
                return;
            }
            shift += 7;
        }
        panic!("incomplete varint in frame");
    }

    fn contains_bytes(frame: &[u8], needle: &[u8]) -> bool {
        frame.windows(needle.len()).any(|w| w == needle)
    }

    #[test]
    fn span_start_encodes_without_error() {
        let (buf, n) = encode(&make_span_start(), 0);
        assert!(n > 0 && n <= MAX_TRACE_FRAME_SIZE);
        check_frame_length(&buf[..n]);
    }

    #[test]
    fn span_start_with_deadline_encodes_without_error() {
        let (buf, n) = encode(&make_span_start_with_deadline(), 0);
        assert!(n > 0 && n <= MAX_TRACE_FRAME_SIZE);
        check_frame_length(&buf[..n]);
    }

    #[test]
    fn span_end_encodes_without_error() {
        let (buf, n) = encode(&make_span_end(), 0);
        assert!(n > 0 && n <= MAX_TRACE_FRAME_SIZE);
        check_frame_length(&buf[..n]);
    }

    #[test]
    fn marker_encodes_without_error() {
        let (buf, n) = encode(&make_marker(), 0);
        assert!(n > 0 && n <= MAX_TRACE_FRAME_SIZE);
        check_frame_length(&buf[..n]);
    }

    #[test]
    fn marker_with_value_encodes_without_error() {
        let (buf, n) = encode(&make_marker_with_value(), 0);
        assert!(n > 0 && n <= MAX_TRACE_FRAME_SIZE);
        check_frame_length(&buf[..n]);
    }

    #[test]
    fn span_start_fits_in_max_buffer() {
        let (_, n) = encode(&make_span_start_with_deadline(), u32::MAX);
        assert!(n <= MAX_TRACE_FRAME_SIZE);
    }

    #[test]
    fn span_end_fits_in_max_buffer() {
        let (_, n) = encode(&make_span_end(), u32::MAX);
        assert!(n <= MAX_TRACE_FRAME_SIZE);
    }

    #[test]
    fn marker_fits_in_max_buffer() {
        let (_, n) = encode(&make_marker_with_value(), u32::MAX);
        assert!(n <= MAX_TRACE_FRAME_SIZE);
    }

    #[test]
    fn name_present_in_output() {
        let (buf, n) = encode(&make_span_start(), 0);
        assert!(contains_bytes(&buf[..n], b"led_task"));
    }

    #[test]
    fn marker_label_present_in_output() {
        let (buf, n) = encode(&make_marker(), 0);
        assert!(contains_bytes(&buf[..n], b"ukf_predict"));
    }

    #[test]
    fn deadline_present_in_span_start_output() {
        let (buf, n) = encode(&make_span_start_with_deadline(), 0);
        assert!(contains_bytes(&buf[..n], &0.5_f32.to_le_bytes()));
    }

    #[test]
    fn span_start_with_deadline_larger_than_span_end() {
        let (_, n_start) = encode(&make_span_start_with_deadline(), 0);
        let (_, n_end) = encode(&make_span_end(), 0);
        assert!(n_start > n_end);
    }

    #[test]
    fn sequence_included_in_output() {
        let msg = make_span_start();
        let (buf0, n0) = encode(&msg, 0);
        let (buf1, n1) = encode(&msg, 99);
        assert_ne!(&buf0[..n0], &buf1[..n1]);
    }

    #[test]
    fn sequence_wraps_at_max() {
        let (buf, n) = encode(&make_span_start(), u32::MAX);
        check_frame_length(&buf[..n]);
    }

    #[test]
    fn timestamp_ns_present_in_output() {
        // varint(1_000_000) = [0xC0, 0x84, 0x3D]
        let (buf, n) = encode(&make_span_start(), 0);
        assert!(contains_bytes(&buf[..n], &[0xC0u8, 0x84, 0x3D]));
    }

    #[test]
    fn buffer_too_small_returns_error() {
        let mut buf = [0u8; 4];
        assert_eq!(
            encode_trace_frame(&make_span_start(), 0, &mut buf),
            Err(TracingEncodeError::BufferFull)
        );
    }

    #[test]
    fn marker_value_preserved_after_encode_decode() {
        let (buf, n) = encode(&make_marker_with_value(), 0);
        assert!(n > 0);
        // The value 42 as varint is just 0x2A; verify it appears in the frame
        assert!(contains_bytes(&buf[..n], &[0x2A]));
    }

    #[test]
    fn span_start_wire_format_snapshot() {
        let (buf, n) = encode(&make_span_start_with_deadline(), 1);
        assert_debug_snapshot!(&buf[..n]);
    }

    // ── SequenceEncoder tests ─────────────────────────────────────────────────

    #[test]
    fn sequence_encoder_starts_at_zero() {
        assert_eq!(SequenceEncoder::new().sequence(), 0);
    }

    #[test]
    fn sequence_encoder_advances_on_each_encode() {
        let mut enc = SequenceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_FRAME_SIZE];
        enc.encode(&make_span_start(), &mut buf).unwrap();
        assert_eq!(enc.sequence(), 1);
        enc.encode(&make_span_end(), &mut buf).unwrap();
        assert_eq!(enc.sequence(), 2);
    }

    #[test]
    fn sequence_encoder_injects_sequence_into_frame() {
        let mut enc = SequenceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_FRAME_SIZE];
        let n = enc.encode(&make_span_start(), &mut buf).unwrap();
        let (decoded, _) = decode_trace_frame(&buf[..n]).unwrap();
        assert_eq!(decoded.sequence, 0);
        let n = enc.encode(&make_span_start(), &mut buf).unwrap();
        let (decoded, _) = decode_trace_frame(&buf[..n]).unwrap();
        assert_eq!(decoded.sequence, 1);
    }

    #[test]
    fn sequence_encoder_wraps_at_max() {
        let mut enc = SequenceEncoder { sequence: u32::MAX };
        let mut buf = [0u8; MAX_TRACE_FRAME_SIZE];
        enc.encode(&make_span_start(), &mut buf).unwrap();
        assert_eq!(enc.sequence(), 0);
    }

    // ── decode_trace_frame round-trip tests ───────────────────────────────────

    fn round_trip(msg: &TraceEvent, seq: u32) -> TraceEvent {
        let (buf, n) = encode(msg, seq);
        let (decoded, consumed) = decode_trace_frame(&buf[..n]).unwrap();
        assert_eq!(consumed, n);
        decoded
    }

    #[test]
    fn round_trip_span_start_preserves_fields() {
        let original = make_span_start();
        let decoded = round_trip(&original, 7);
        assert_eq!(decoded.name, original.name);
        assert_eq!(decoded.event_type, original.event_type);
        assert_eq!(decoded.source_type, original.source_type);
        assert_eq!(decoded.timestamp_ns, original.timestamp_ns);
        assert_eq!(decoded.priority, original.priority);
        assert_eq!(decoded.sequence, 7);
    }

    #[test]
    fn round_trip_span_start_with_deadline_preserves_deadline() {
        let original = make_span_start_with_deadline();
        let decoded = round_trip(&original, 0);
        assert_eq!(decoded.relative_deadline_ms(), Some(&0.5_f32));
    }

    #[test]
    fn round_trip_span_end_preserves_fields() {
        let original = make_span_end();
        let decoded = round_trip(&original, 3);
        assert_eq!(decoded.name, original.name);
        assert_eq!(decoded.event_type, original.event_type);
        assert_eq!(decoded.source_type, original.source_type);
    }

    #[test]
    fn round_trip_marker_preserves_fields() {
        let original = make_marker();
        let decoded = round_trip(&original, 0);
        assert_eq!(decoded.name, original.name);
        assert_eq!(decoded.event_type, original.event_type);
        assert_eq!(decoded.marker_value(), None);
    }

    #[test]
    fn round_trip_marker_with_value_preserves_value() {
        let original = make_marker_with_value();
        let decoded = round_trip(&original, 0);
        assert_eq!(decoded.marker_value(), Some(&42_u32));
    }

    #[test]
    fn decode_truncated_frame_returns_error() {
        let (buf, n) = encode(&make_span_start(), 0);
        assert_eq!(
            decode_trace_frame(&buf[..n - 1]),
            Err(TracingDecodeError::Truncated)
        );
    }

    #[test]
    fn decode_empty_slice_returns_malformed_varint() {
        assert_eq!(
            decode_trace_frame(&[]),
            Err(TracingDecodeError::MalformedVarint)
        );
    }

    #[test]
    fn decode_returns_correct_consumed_byte_count() {
        let (buf, n) = encode(&make_marker_with_value(), 5);
        let (_, consumed) = decode_trace_frame(&buf[..n]).unwrap();
        assert_eq!(consumed, n);
    }
}
