//! Wire encoding for the v2 trace format (EXEC-TRACE-002 §6).
//!
//! Every frame is `[varint: byte length][protobuf-encoded TraceFrame]`, and every
//! frame class — the three per-occurrence events, the dictionary entry and the
//! stream header — shares one flat message discriminated by `event_type` (§18.1).
//!
//! [`TraceEncoder`] owns the three pieces of per-stream state the format needs:
//! the name dictionary (§5.5), the timestamp base the deltas are taken against
//! (§5.6) and the sequence counter (§5.7). It runs in the task that owns the
//! transport, off the control path.

use crate::types::{SourceType, TraceEvent};
use micropb::{MessageDecode, MessageEncode, PbDecoder, PbEncoder};

/// Upper bound on one encoded frame, length prefix included.
pub const MAX_TRACE_FRAME_SIZE: usize = 128;

/// Upper bound on one [`TraceEncoder::encode`] call, which emits a dictionary
/// frame ahead of the event when a name is seen for the first time.
pub const MAX_TRACE_BURST_SIZE: usize = 2 * MAX_TRACE_FRAME_SIZE;

/// Distinct names the dictionary holds, against roughly 30 in the firmware today.
pub const NAME_REGISTRY_CAPACITY: usize = 64;

/// The reserved "unknown" name id, emitted when the registry is full (REQ-T11).
pub const UNKNOWN_NAME_ID: u32 = 0;

/// Sequence numbers wrap here rather than at 2³².
///
/// The counter exists only to detect gaps, a gap is read modulo the wrap, and the
/// largest burst ever observed was 103 frames. A full 32-bit counter costs five
/// varint bytes once past 2²⁸; this costs at most two (§18.1).
pub const SEQUENCE_MODULUS: u32 = 16_384;

/// The unit of the `timestamp_ticks` field, declared once on the stream header.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum TimeBase {
    /// Ticks are nanoseconds; `core_frequency_hz` is unused.
    #[default]
    Nanoseconds,
    /// Ticks are core clock cycles, converted by `core_frequency_hz` (§5.8).
    Cycles,
}

/// Errors returned by the encoding entry points.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TracingEncodeError {
    /// The output buffer is too small or the encoded message exceeds [`MAX_TRACE_FRAME_SIZE`].
    BufferFull,
}

/// Result of one [`TraceEncoder::encode`] call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Encoded {
    /// Bytes written to the output buffer.
    pub len: usize,
    /// The name was unknown and the dictionary was full, so the event went out
    /// with [`UNKNOWN_NAME_ID`]. The caller should count this as a fault: the
    /// stream stays decodable, but the name is lost.
    pub name_registry_full: bool,
}

/// The frame classes the wire carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameKind {
    SpanStart,
    SpanEnd,
    Marker,
    /// A dictionary entry: `name_id` now resolves to `name` (§6.2).
    NameRegistered,
    /// The stream header, carrying the timebase and the source mask (§6.3).
    TraceStart,
}

/// One decoded frame, mirroring the wire message field for field.
///
/// Decoding deliberately stops here rather than returning a [`TraceEvent`]: in v2
/// an event is not recoverable from a single frame, because its name is a
/// dictionary id and its timestamp is a delta. Resolving both needs per-stream
/// state, which belongs in the host decoder (§18.3).
#[derive(Debug, Clone, PartialEq)]
pub struct RawTraceFrame {
    /// A delta against the previous sequenced frame, except on
    /// [`FrameKind::NameRegistered`] and [`FrameKind::TraceStart`], where it is
    /// absolute and re-establishes the time origin.
    pub timestamp_ticks: u64,
    pub name_id: u32,
    pub kind: FrameKind,
    pub sequence: u32,
    /// [`FrameKind::Marker`] only.
    pub marker_value: Option<u32>,
    /// [`FrameKind::NameRegistered`] only.
    pub name: heapless::String<32>,
    /// [`FrameKind::NameRegistered`] only.
    pub source_type: Option<SourceType>,
    /// [`FrameKind::NameRegistered`] only.
    pub priority: u32,
    /// [`FrameKind::NameRegistered`] only.
    pub relative_deadline_ms: Option<f32>,
    /// [`FrameKind::TraceStart`] only.
    pub timebase: TimeBase,
    /// [`FrameKind::TraceStart`] only; zero when the timebase is nanoseconds.
    pub core_frequency_hz: u32,
    /// [`FrameKind::TraceStart`] only.
    pub source_mask: u32,
}

impl RawTraceFrame {
    fn new(kind: FrameKind, timestamp_ticks: u64, sequence: u32, name_id: u32) -> Self {
        Self {
            timestamp_ticks,
            name_id,
            kind,
            sequence,
            marker_value: None,
            name: heapless::String::new(),
            source_type: None,
            priority: 0,
            relative_deadline_ms: None,
            timebase: TimeBase::Nanoseconds,
            core_frequency_hz: 0,
            source_mask: 0,
        }
    }
}

/// Encodes [`TraceEvent`]s into the v2 wire format, holding the per-stream state
/// the format needs.
///
/// # Name interning
///
/// The dictionary is keyed on the name's **content**, not on the `&'static str`
/// pointer §5.5 proposed. By the time an event reaches this layer the literal's
/// pointer is gone — [`TraceEvent`] carries an inline copy that has been moved
/// through a channel — so pointer keying is only available to a registry in the
/// recording layer, which would mean a process-global with atomics and a changed
/// [`TraceSink`] API (§18.2). The scan is linear over at most
/// [`NAME_REGISTRY_CAPACITY`] entries with a length check before any byte
/// comparison, and it runs off the control path.
///
/// [`TraceSink`]: crate::TraceSink
pub struct TraceEncoder {
    names: heapless::Vec<heapless::String<32>, NAME_REGISTRY_CAPACITY>,
    last_ticks: u64,
    sequence: u32,
}

impl TraceEncoder {
    /// Creates an encoder with an empty dictionary and the counters at zero.
    pub const fn new() -> Self {
        Self {
            names: heapless::Vec::new(),
            last_ticks: 0,
            sequence: 0,
        }
    }

    /// Returns the sequence number of the *next* frame.
    pub fn sequence(&self) -> u32 {
        self.sequence
    }

    /// Returns the number of names currently held in the dictionary.
    pub fn registered_names(&self) -> usize {
        self.names.len()
    }

    /// Encodes the stream header (§6.3) into `out`, returning the bytes written.
    ///
    /// The header gives the host the tick unit and the active source mask without
    /// either being hard-coded in the decoder, and it is what marks the stream as
    /// v2 (§6.4). Emit it once at init, before any event.
    ///
    /// Its timestamp is absolute and becomes the base the following deltas are
    /// taken against.
    ///
    /// # Errors
    /// Returns [`TracingEncodeError::BufferFull`] if `out` is too small.
    pub fn encode_trace_start(
        &mut self,
        timestamp_ticks: u64,
        timebase: TimeBase,
        core_frequency_hz: u32,
        source_mask: u32,
        out: &mut [u8],
    ) -> Result<usize, TracingEncodeError> {
        let mut frame = RawTraceFrame::new(
            FrameKind::TraceStart,
            timestamp_ticks,
            self.next_sequence(),
            UNKNOWN_NAME_ID,
        );
        frame.timebase = timebase;
        frame.core_frequency_hz = core_frequency_hz;
        frame.source_mask = source_mask;
        self.last_ticks = timestamp_ticks;
        encode_trace_frame(&frame, out)
    }

    /// Encodes `event` into `out`, interning its name, delta-encoding its
    /// timestamp and assigning its sequence number.
    ///
    /// When the name is seen for the first time this writes **two** frames: the
    /// dictionary entry, carrying the name and its fixed attributes, followed by
    /// the event itself. `out` must therefore be at least
    /// [`MAX_TRACE_BURST_SIZE`] bytes.
    ///
    /// # Errors
    /// Returns [`TracingEncodeError::BufferFull`] if `out` is too small.
    pub fn encode(
        &mut self,
        event: &TraceEvent,
        out: &mut [u8],
    ) -> Result<Encoded, TracingEncodeError> {
        let (name, timestamp_ns) = match event {
            TraceEvent::SpanStart {
                name, timestamp_ns, ..
            }
            | TraceEvent::SpanEnd {
                name, timestamp_ns, ..
            }
            | TraceEvent::Marker {
                name, timestamp_ns, ..
            } => (name, *timestamp_ns),
        };

        let mut written = 0usize;
        let mut name_registry_full = false;

        let name_id = match self.lookup(name) {
            Some(id) => id,
            None => match self.register(event, timestamp_ns, out) {
                Ok((id, n)) => {
                    written += n;
                    id
                }
                Err(RegisterError::Full) => {
                    name_registry_full = true;
                    UNKNOWN_NAME_ID
                }
                Err(RegisterError::Encode(e)) => return Err(e),
            },
        };

        let delta = timestamp_ns.saturating_sub(self.last_ticks);
        self.last_ticks = timestamp_ns;
        let sequence = self.next_sequence();

        let mut frame = match event {
            TraceEvent::SpanStart { .. } => {
                RawTraceFrame::new(FrameKind::SpanStart, delta, sequence, name_id)
            }
            TraceEvent::SpanEnd { .. } => {
                RawTraceFrame::new(FrameKind::SpanEnd, delta, sequence, name_id)
            }
            TraceEvent::Marker { marker_value, .. } => {
                let mut f = RawTraceFrame::new(FrameKind::Marker, delta, sequence, name_id);
                f.marker_value = *marker_value;
                f
            }
        };
        // proto3 omits a zero-valued scalar, so a delta of zero costs nothing.
        frame.timestamp_ticks = delta;

        let out_tail = out
            .get_mut(written..)
            .ok_or(TracingEncodeError::BufferFull)?;
        written += encode_trace_frame(&frame, out_tail)?;

        Ok(Encoded {
            len: written,
            name_registry_full,
        })
    }

    /// Linear scan keyed on content, length checked before any byte comparison.
    fn lookup(&self, name: &heapless::String<32>) -> Option<u32> {
        let needle = name.as_bytes();
        self.names
            .iter()
            .position(|candidate| candidate.len() == needle.len() && candidate.as_bytes() == needle)
            // Ids are one-based: zero is reserved for "unknown".
            .and_then(|index| u32::try_from(index + 1).ok())
    }

    /// Assigns the next id and writes the dictionary frame, whose timestamp is
    /// absolute so a host attaching mid-run can re-establish the time origin.
    fn register(
        &mut self,
        event: &TraceEvent,
        timestamp_ns: u64,
        out: &mut [u8],
    ) -> Result<(u32, usize), RegisterError> {
        let (name, source_type, priority, relative_deadline_ms) = match event {
            TraceEvent::SpanStart {
                name,
                source_type,
                priority,
                relative_deadline_ms,
                ..
            } => (name, Some(*source_type), *priority, *relative_deadline_ms),
            // A SpanEnd or Marker reaching registration before its SpanStart
            // carries no attributes to register; the name alone is the entry.
            TraceEvent::SpanEnd { name, .. } | TraceEvent::Marker { name, .. } => {
                (name, None, 0, None)
            }
        };

        self.names
            .push(name.clone())
            .map_err(|_| RegisterError::Full)?;
        let id = u32::try_from(self.names.len()).map_err(|_| RegisterError::Full)?;

        let sequence = self.next_sequence();
        let mut frame = RawTraceFrame::new(FrameKind::NameRegistered, timestamp_ns, sequence, id);
        frame.name = name.clone();
        frame.source_type = source_type;
        frame.priority = priority;
        frame.relative_deadline_ms = relative_deadline_ms;
        self.last_ticks = timestamp_ns;

        let n = encode_trace_frame(&frame, out).map_err(RegisterError::Encode)?;
        Ok((id, n))
    }

    fn next_sequence(&mut self) -> u32 {
        let current = self.sequence;
        self.sequence = (self.sequence + 1) % SEQUENCE_MODULUS;
        current
    }
}

impl Default for TraceEncoder {
    fn default() -> Self {
        Self::new()
    }
}

enum RegisterError {
    Full,
    Encode(TracingEncodeError),
}

/// Encodes one [`RawTraceFrame`] as a length-delimited protobuf frame into `out`.
///
/// Frame format: `[varint: byte length][protobuf-encoded TraceFrame]`. Returns the
/// number of bytes written.
///
/// This is the stateless half: it writes exactly the fields the frame carries and
/// applies no interning, delta or sequencing. Use [`TraceEncoder`] to produce
/// frames from [`TraceEvent`]s.
///
/// # Errors
/// Returns [`TracingEncodeError::BufferFull`] if `out` is too small or the message
/// exceeds [`MAX_TRACE_FRAME_SIZE`].
#[allow(clippy::indexing_slicing)] // bounds-checked: n <= vec.len() <= MAX_TRACE_FRAME_SIZE <= out.len()
pub fn encode_trace_frame(
    frame: &RawTraceFrame,
    out: &mut [u8],
) -> Result<usize, TracingEncodeError> {
    let proto_msg = to_proto(frame);

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
    /// `micropb` rejected the protobuf payload bytes, or the event type is unknown.
    DecodeError,
}

/// Decodes one length-delimited frame produced by [`encode_trace_frame`].
///
/// Returns the raw frame and the total number of bytes consumed (varint header +
/// payload). The frame's name is a dictionary id and its timestamp is usually a
/// delta; resolving either needs the per-stream state the host decoder holds
/// (§18.3).
///
/// # Errors
/// Returns [`TracingDecodeError`] if the frame is truncated, the varint is
/// malformed, or the protobuf payload cannot be decoded.
pub fn decode_trace_frame(frame: &[u8]) -> Result<(RawTraceFrame, usize), TracingDecodeError> {
    let (payload_len, header_len) =
        decode_varint(frame).ok_or(TracingDecodeError::MalformedVarint)?;
    let total = header_len + payload_len;
    if frame.len() < total {
        return Err(TracingDecodeError::Truncated);
    }
    let payload = frame
        .get(header_len..total)
        .ok_or(TracingDecodeError::Truncated)?;
    let mut decoder = PbDecoder::new(payload);
    let mut proto_frame = crate::proto::tracing_::TraceFrame::default();
    proto_frame
        .decode(&mut decoder, payload_len)
        .map_err(|_| TracingDecodeError::DecodeError)?;
    let decoded = from_proto(proto_frame).ok_or(TracingDecodeError::DecodeError)?;
    Ok((decoded, total))
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

fn to_proto(frame: &RawTraceFrame) -> crate::proto::tracing_::TraceFrame {
    use crate::proto::tracing_ as pb;

    let mut msg = pb::TraceFrame {
        timestamp_ticks: frame.timestamp_ticks,
        name_id: frame.name_id,
        event_type: match frame.kind {
            FrameKind::SpanStart => pb::TraceEventType::SpanStart,
            FrameKind::SpanEnd => pb::TraceEventType::SpanEnd,
            FrameKind::Marker => pb::TraceEventType::Marker,
            FrameKind::NameRegistered => pb::TraceEventType::NameRegistered,
            FrameKind::TraceStart => pb::TraceEventType::TraceStart,
        },
        sequence: frame.sequence,
        name: frame.name.clone(),
        source_type: match frame.source_type {
            Some(SourceType::Isr) => pb::TraceEventSourceType::Isr,
            Some(SourceType::Task) => pb::TraceEventSourceType::Task,
            None => pb::TraceEventSourceType::Unspecified,
        },
        priority: frame.priority,
        timebase: match frame.timebase {
            TimeBase::Nanoseconds => pb::TimeBase::Nanoseconds,
            TimeBase::Cycles => pb::TimeBase::Cycles,
        },
        core_frequency_hz: frame.core_frequency_hz,
        source_mask: frame.source_mask,
        ..Default::default()
    };
    if let Some(v) = frame.marker_value {
        msg.set_marker_value(v);
    }
    if let Some(dl) = frame.relative_deadline_ms {
        msg.set_relative_deadline_ms(dl);
    }
    msg
}

fn from_proto(p: crate::proto::tracing_::TraceFrame) -> Option<RawTraceFrame> {
    use crate::proto::tracing_ as pb;

    let kind = if p.event_type == pb::TraceEventType::SpanStart {
        FrameKind::SpanStart
    } else if p.event_type == pb::TraceEventType::SpanEnd {
        FrameKind::SpanEnd
    } else if p.event_type == pb::TraceEventType::Marker {
        FrameKind::Marker
    } else if p.event_type == pb::TraceEventType::NameRegistered {
        FrameKind::NameRegistered
    } else if p.event_type == pb::TraceEventType::TraceStart {
        FrameKind::TraceStart
    } else {
        return None;
    };

    let source_type = if p.source_type == pb::TraceEventSourceType::Isr {
        Some(SourceType::Isr)
    } else if p.source_type == pb::TraceEventSourceType::Task {
        Some(SourceType::Task)
    } else {
        None
    };

    let timebase = if p.timebase == pb::TimeBase::Cycles {
        TimeBase::Cycles
    } else {
        TimeBase::Nanoseconds
    };

    Some(RawTraceFrame {
        timestamp_ticks: p.timestamp_ticks,
        name_id: p.name_id,
        kind,
        sequence: p.sequence,
        marker_value: p.marker_value().copied(),
        source_type,
        priority: p.priority,
        relative_deadline_ms: p.relative_deadline_ms().copied(),
        timebase,
        core_frequency_hz: p.core_frequency_hz,
        source_mask: p.source_mask,
        name: p.name,
    })
}

#[cfg(all(test, feature = "std", feature = "enabled"))]
mod tests {
    use super::*;
    use heapless::String;
    use insta::assert_debug_snapshot;

    fn name(v: &str) -> String<32> {
        let mut n: String<32> = String::new();
        n.push_str(v).unwrap();
        n
    }

    fn span_start(n: &str, ts: u64) -> TraceEvent {
        TraceEvent::SpanStart {
            timestamp_ns: ts,
            name: name(n),
            source_type: SourceType::Task,
            sequence: 0,
            priority: 2,
            relative_deadline_ms: None,
        }
    }

    fn span_start_with_deadline(n: &str, ts: u64) -> TraceEvent {
        TraceEvent::SpanStart {
            timestamp_ns: ts,
            name: name(n),
            source_type: SourceType::Isr,
            sequence: 0,
            priority: 8,
            relative_deadline_ms: Some(0.5),
        }
    }

    fn span_end(n: &str, ts: u64) -> TraceEvent {
        TraceEvent::SpanEnd {
            timestamp_ns: ts,
            name: name(n),
            sequence: 0,
        }
    }

    fn marker(n: &str, ts: u64, value: Option<u32>) -> TraceEvent {
        TraceEvent::Marker {
            timestamp_ns: ts,
            name: name(n),
            sequence: 0,
            marker_value: value,
        }
    }

    /// Encodes into a fresh buffer and returns the bytes actually written.
    fn encode_one(enc: &mut TraceEncoder, event: &TraceEvent) -> (Vec<u8>, Encoded) {
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        let outcome = enc.encode(event, &mut buf).unwrap();
        (buf[..outcome.len].to_vec(), outcome)
    }

    /// Decodes every frame in `bytes`, asserting the buffer is consumed exactly.
    fn decode_all(bytes: &[u8]) -> Vec<RawTraceFrame> {
        let mut frames = Vec::new();
        let mut offset = 0;
        while offset < bytes.len() {
            let (frame, consumed) = decode_trace_frame(&bytes[offset..]).unwrap();
            frames.push(frame);
            offset += consumed;
        }
        assert_eq!(offset, bytes.len(), "frames did not tile the buffer");
        frames
    }

    /// An encoder whose dictionary already holds `names`, so that following
    /// encodes are steady-state rather than cold.
    fn warmed(names: &[&str]) -> TraceEncoder {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        for n in names {
            enc.encode(&span_start(n, 0), &mut buf).unwrap();
        }
        enc
    }

    // ── Frame structure ───────────────────────────────────────────────────────

    #[test]
    fn first_sight_of_a_name_emits_a_dictionary_frame_then_the_event() {
        let mut enc = TraceEncoder::new();
        let (bytes, outcome) = encode_one(&mut enc, &span_start_with_deadline("gyro_isr", 5_000));
        assert!(!outcome.name_registry_full);

        let frames = decode_all(&bytes);
        assert_eq!(frames.len(), 2, "expected dictionary frame + event");

        assert_eq!(frames[0].kind, FrameKind::NameRegistered);
        assert_eq!(frames[0].name.as_str(), "gyro_isr");
        assert_eq!(frames[0].name_id, 1);
        assert_eq!(frames[0].source_type, Some(SourceType::Isr));
        assert_eq!(frames[0].priority, 8);
        assert_eq!(frames[0].relative_deadline_ms, Some(0.5));

        assert_eq!(frames[1].kind, FrameKind::SpanStart);
        assert_eq!(frames[1].name_id, 1);
        assert!(frames[1].name.is_empty(), "event must not carry the name");
    }

    #[test]
    fn dictionary_frame_timestamp_is_absolute() {
        let mut enc = TraceEncoder::new();
        let (bytes, _) = encode_one(&mut enc, &span_start("main_task", 7_000_000));
        let frames = decode_all(&bytes);
        assert_eq!(frames[0].timestamp_ticks, 7_000_000);
    }

    #[test]
    fn second_sight_of_a_name_emits_the_event_alone() {
        let mut enc = warmed(&["main_task"]);
        let (bytes, _) = encode_one(&mut enc, &span_end("main_task", 1_000));
        let frames = decode_all(&bytes);
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].kind, FrameKind::SpanEnd);
        assert_eq!(frames[0].name_id, 1);
    }

    #[test]
    fn distinct_names_get_distinct_ids() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        for (i, n) in ["a", "b", "c"].iter().enumerate() {
            let outcome = enc.encode(&span_start(n, 0), &mut buf).unwrap();
            let frames = decode_all(&buf[..outcome.len]);
            assert_eq!(frames[0].name_id, u32::try_from(i + 1).unwrap());
            assert_eq!(frames[1].name_id, u32::try_from(i + 1).unwrap());
        }
        assert_eq!(enc.registered_names(), 3);
    }

    #[test]
    fn name_ids_are_one_based_so_zero_stays_reserved() {
        let mut enc = TraceEncoder::new();
        let (bytes, _) = encode_one(&mut enc, &span_start("first", 0));
        assert_eq!(decode_all(&bytes)[0].name_id, 1);
        assert_ne!(decode_all(&bytes)[0].name_id, UNKNOWN_NAME_ID);
    }

    #[test]
    fn interning_is_keyed_on_content_not_on_pointer() {
        // Two separately constructed names with equal content must share an id —
        // this is the property §18.2 chose content keying to get.
        let mut enc = warmed(&["main_task"]);
        let mut owned = std::string::String::from("main_");
        owned.push_str("task");
        let (bytes, _) = encode_one(&mut enc, &span_end(&owned, 10));
        let frames = decode_all(&bytes);
        assert_eq!(frames.len(), 1, "equal content must not re-register");
        assert_eq!(frames[0].name_id, 1);
    }

    #[test]
    fn names_sharing_a_prefix_are_distinct_entries() {
        let mut enc = warmed(&["main", "main_task"]);
        assert_eq!(enc.registered_names(), 2);
        let (bytes, _) = encode_one(&mut enc, &span_end("main", 0));
        assert_eq!(decode_all(&bytes)[0].name_id, 1);
        let (bytes, _) = encode_one(&mut enc, &span_end("main_task", 0));
        assert_eq!(decode_all(&bytes)[0].name_id, 2);
    }

    // ── Delta timestamps (§5.6) ───────────────────────────────────────────────

    #[test]
    fn event_timestamps_are_deltas_against_the_previous_frame() {
        let mut enc = warmed(&["main_task"]);
        let (bytes, _) = encode_one(&mut enc, &span_start("main_task", 1_000));
        assert_eq!(decode_all(&bytes)[0].timestamp_ticks, 1_000);
        let (bytes, _) = encode_one(&mut enc, &span_end("main_task", 1_700));
        assert_eq!(decode_all(&bytes)[0].timestamp_ticks, 700);
    }

    #[test]
    fn the_event_following_its_own_dictionary_frame_has_a_zero_delta() {
        let mut enc = TraceEncoder::new();
        let (bytes, _) = encode_one(&mut enc, &span_start("main_task", 9_999));
        let frames = decode_all(&bytes);
        assert_eq!(
            frames[0].timestamp_ticks, 9_999,
            "dictionary frame absolute"
        );
        assert_eq!(frames[1].timestamp_ticks, 0, "event delta against it");
    }

    #[test]
    fn accumulated_deltas_reconstruct_the_original_timestamps() {
        let mut enc = warmed(&["a"]);
        let stamps = [1_000u64, 1_001, 250_000, 250_003, 9_000_000];
        let mut bytes = Vec::new();
        for ts in stamps {
            let (b, _) = encode_one(&mut enc, &span_end("a", ts));
            bytes.extend_from_slice(&b);
        }
        let mut clock = 0u64;
        let reconstructed: Vec<u64> = decode_all(&bytes)
            .iter()
            .map(|f| {
                clock += f.timestamp_ticks;
                clock
            })
            .collect();
        assert_eq!(reconstructed, stamps);
    }

    #[test]
    fn a_backwards_timestamp_saturates_rather_than_wrapping() {
        // Cannot happen with a monotonic clock, but a delta of ~2^64 would be
        // five varint bytes and would desynchronise the host's clock for good.
        let mut enc = warmed(&["a"]);
        encode_one(&mut enc, &span_end("a", 10_000));
        let (bytes, _) = encode_one(&mut enc, &span_end("a", 9_000));
        assert_eq!(decode_all(&bytes)[0].timestamp_ticks, 0);
    }

    // ── Sequencing (§18.1) ────────────────────────────────────────────────────

    #[test]
    fn sequence_starts_at_zero_and_advances_per_frame() {
        let mut enc = TraceEncoder::new();
        assert_eq!(enc.sequence(), 0);
        let (bytes, _) = encode_one(&mut enc, &span_start("a", 0));
        let frames = decode_all(&bytes);
        assert_eq!(frames[0].sequence, 0, "dictionary frame is sequenced too");
        assert_eq!(frames[1].sequence, 1);
        assert_eq!(enc.sequence(), 2);
    }

    #[test]
    fn sequence_wraps_at_the_modulus_not_at_u32_max() {
        let mut enc = warmed(&["a"]);
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        while enc.sequence() != SEQUENCE_MODULUS - 1 {
            enc.encode(&span_end("a", 0), &mut buf).unwrap();
        }
        let (bytes, _) = encode_one(&mut enc, &span_end("a", 0));
        assert_eq!(decode_all(&bytes)[0].sequence, SEQUENCE_MODULUS - 1);
        assert_eq!(enc.sequence(), 0, "wraps to zero, not to 16384");
    }

    #[test]
    fn sequence_never_exceeds_the_modulus() {
        let mut enc = warmed(&["a"]);
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        for _ in 0..(SEQUENCE_MODULUS * 2) {
            let n = enc.encode(&span_end("a", 0), &mut buf).unwrap();
            let frame = decode_all(&buf[..n.len]).remove(0);
            assert!(frame.sequence < SEQUENCE_MODULUS);
        }
    }

    // ── Registry exhaustion (REQ-T11) ─────────────────────────────────────────

    #[test]
    fn registry_exhaustion_falls_back_to_the_unknown_id_and_reports_it() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        for i in 0..NAME_REGISTRY_CAPACITY {
            let outcome = enc
                .encode(&span_start(&format!("n{i}"), 0), &mut buf)
                .unwrap();
            assert!(!outcome.name_registry_full);
        }
        assert_eq!(enc.registered_names(), NAME_REGISTRY_CAPACITY);

        let (bytes, outcome) = encode_one(&mut enc, &span_start("one_too_many", 0));
        assert!(outcome.name_registry_full);
        let frames = decode_all(&bytes);
        assert_eq!(frames.len(), 1, "no dictionary frame is emitted");
        assert_eq!(frames[0].name_id, UNKNOWN_NAME_ID);
        assert_eq!(frames[0].kind, FrameKind::SpanStart);
    }

    #[test]
    fn a_registered_name_still_resolves_after_the_registry_fills() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        for i in 0..NAME_REGISTRY_CAPACITY {
            enc.encode(&span_start(&format!("n{i}"), 0), &mut buf)
                .unwrap();
        }
        enc.encode(&span_start("overflow", 0), &mut buf).unwrap();
        let (bytes, outcome) = encode_one(&mut enc, &span_end("n0", 0));
        assert!(!outcome.name_registry_full);
        assert_eq!(decode_all(&bytes)[0].name_id, 1);
    }

    // ── Stream header (§6.3) ──────────────────────────────────────────────────

    #[test]
    fn trace_start_carries_the_timebase_frequency_and_mask() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_FRAME_SIZE];
        let n = enc
            .encode_trace_start(1_234, TimeBase::Cycles, 72_000_000, 0b10111, &mut buf)
            .unwrap();
        let frames = decode_all(&buf[..n]);
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].kind, FrameKind::TraceStart);
        assert_eq!(frames[0].timestamp_ticks, 1_234, "absolute, not a delta");
        assert_eq!(frames[0].timebase, TimeBase::Cycles);
        assert_eq!(frames[0].core_frequency_hz, 72_000_000);
        assert_eq!(frames[0].source_mask, 0b10111);
        assert_eq!(frames[0].sequence, 0, "the header is the first frame");
    }

    #[test]
    fn trace_start_sets_the_base_for_the_following_delta() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        enc.encode_trace_start(1_000, TimeBase::Nanoseconds, 0, 0x1F, &mut buf)
            .unwrap();
        let (bytes, _) = encode_one(&mut enc, &span_start("a", 1_500));
        let frames = decode_all(&bytes);
        assert_eq!(frames[0].timestamp_ticks, 1_500, "dictionary absolute");
        assert_eq!(frames[1].timestamp_ticks, 0);
    }

    #[test]
    fn a_nanosecond_timebase_is_the_default_and_costs_no_bytes() {
        // proto3 omits zero-valued scalars, so NANOSECONDS and an unset core
        // frequency are free on the header.
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_FRAME_SIZE];
        let ns = enc
            .encode_trace_start(0, TimeBase::Nanoseconds, 0, 0, &mut buf)
            .unwrap();
        let mut enc = TraceEncoder::new();
        let cycles = enc
            .encode_trace_start(0, TimeBase::Cycles, 72_000_000, 0, &mut buf)
            .unwrap();
        assert!(cycles > ns);
    }

    // ── Marker payloads ───────────────────────────────────────────────────────

    #[test]
    fn marker_value_round_trips() {
        let mut enc = warmed(&["ukf_predict"]);
        let (bytes, _) = encode_one(&mut enc, &marker("ukf_predict", 0, Some(42)));
        assert_eq!(decode_all(&bytes)[0].marker_value, Some(42));
    }

    #[test]
    fn a_bare_marker_carries_no_value() {
        let mut enc = warmed(&["ukf_predict"]);
        let (bytes, _) = encode_one(&mut enc, &marker("ukf_predict", 0, None));
        assert_eq!(decode_all(&bytes)[0].marker_value, None);
    }

    #[test]
    fn a_marker_value_of_zero_survives_as_zero_not_as_absent() {
        // proto3 would drop a plain zero field; `marker_value` is optional so the
        // presence bit distinguishes them. Regression guard for f3ccb25.
        let mut enc = warmed(&["m"]);
        let (bytes, _) = encode_one(&mut enc, &marker("m", 0, Some(0)));
        assert_eq!(decode_all(&bytes)[0].marker_value, Some(0));
    }

    // ── Per-occurrence frames carry no fixed field (REQ-T06) ──────────────────

    #[test]
    fn events_carry_no_name_bytes_once_registered() {
        let mut enc = warmed(&["main_task"]);
        let (bytes, _) = encode_one(&mut enc, &span_start("main_task", 0));
        assert!(
            !bytes.windows(9).any(|w| w == b"main_task"),
            "the name must appear only in the dictionary frame"
        );
    }

    #[test]
    fn events_carry_neither_priority_nor_deadline() {
        let mut enc = warmed(&["gyro_isr"]);
        let (bytes, _) = encode_one(&mut enc, &span_start_with_deadline("gyro_isr", 0));
        let frame = decode_all(&bytes).remove(0);
        assert_eq!(frame.priority, 0);
        assert_eq!(frame.relative_deadline_ms, None);
        assert_eq!(frame.source_type, None);
    }

    // ── Size budget (§18.1, AC 5) ─────────────────────────────────────────────

    /// Steady-state frame size for each event class at a representative delta.
    fn steady_state_sizes(delta: u64) -> [usize; 4] {
        let mut enc = warmed(&["main_task", "ukf_predict"]);
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        let mut t = 1_000_000_000u64;
        let cases = [
            span_start("main_task", 0),
            span_end("main_task", 0),
            marker("ukf_predict", 0, Some(42)),
            marker("ukf_predict", 0, None),
        ];
        // Push the sequence into its two-varint-byte range, the common case.
        for _ in 0..200 {
            t += delta;
            enc.encode(&span_end("main_task", t), &mut buf).unwrap();
        }
        let mut sizes = [0usize; 4];
        for (i, case) in cases.iter().enumerate() {
            t += delta;
            let mut ev = case.clone();
            match &mut ev {
                TraceEvent::SpanStart { timestamp_ns, .. }
                | TraceEvent::SpanEnd { timestamp_ns, .. }
                | TraceEvent::Marker { timestamp_ns, .. } => *timestamp_ns = t,
            }
            sizes[i] = enc.encode(&ev, &mut buf).unwrap().len;
        }
        sizes
    }

    #[test]
    fn steady_state_frames_stay_within_the_size_budget() {
        // §18.1 predicted 13 / 12 / 15 / 12 for SpanStart / SpanEnd / Marker with
        // a value / bare Marker. At 1300-3800 events/s the mean spacing is
        // 260-770 us and bursts are far tighter, so deltas up to 1 ms are the
        // operating range; the implementation is at or under budget across it.
        for delta in [1_000u64, 10_000, 100_000, 1_000_000] {
            let sizes = steady_state_sizes(delta);
            for (size, budget) in sizes.iter().zip([13usize, 12, 15, 12]) {
                assert!(
                    *size <= budget,
                    "delta {delta} ns: frame of {size} B exceeds the {budget} B budget"
                );
            }
        }
    }

    #[test]
    fn a_delta_past_the_operating_range_costs_one_more_byte_and_no_more() {
        // A 10 ms delta — an idle gap, not a traced workload — pushes the varint
        // to three bytes. Recorded rather than hidden: it is where §18.1's
        // per-class figures stop holding, and the tail is bounded at one byte.
        let wide = steady_state_sizes(10_000_000);
        let operating = steady_state_sizes(1_000_000);
        for (w, o) in wide.iter().zip(operating.iter()) {
            assert_eq!(*w, *o + 1);
        }
    }

    #[test]
    fn frame_sizes_across_the_delta_range_snapshot() {
        let table: Vec<(u64, [usize; 4])> = [1_000u64, 10_000, 100_000, 1_000_000, 10_000_000]
            .into_iter()
            .map(|d| (d, steady_state_sizes(d)))
            .collect();
        assert_debug_snapshot!(table);
    }

    #[test]
    fn v2_more_than_halves_the_mean_event_size_against_v1() {
        // v1 measured 33 / 24 / 25 / 27 B for the same four classes (§18.1), a
        // mean of 27.25. The saving is in the mean, which is what the bandwidth
        // budget is spent from: per class it ranges from a 1.8x cut on a bare
        // SpanEnd to a 2.75x cut on a SpanStart, which no longer repeats the
        // name, the priority and the deadline on every occurrence.
        let sizes = steady_state_sizes(100_000);
        let v2_mean = sizes.iter().sum::<usize>() as f64 / 4.0;
        let v1_mean = (33.0 + 24.0 + 25.0 + 27.0) / 4.0;
        assert!(
            v2_mean * 2.0 < v1_mean,
            "v2 mean {v2_mean} B is not less than half of v1 mean {v1_mean} B"
        );
    }

    #[test]
    fn span_start_wire_format_snapshot() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        let n = enc
            .encode(&span_start_with_deadline("gyro_isr", 1_000_000), &mut buf)
            .unwrap()
            .len;
        assert_debug_snapshot!(&buf[..n]);
    }

    // ── Framing ───────────────────────────────────────────────────────────────

    #[test]
    fn each_frame_declares_its_own_length() {
        let mut enc = TraceEncoder::new();
        let (bytes, _) = encode_one(&mut enc, &span_start_with_deadline("gyro_isr", 1_000));
        // decode_all asserts the frames tile the buffer exactly.
        assert_eq!(decode_all(&bytes).len(), 2);
    }

    #[test]
    fn a_buffer_too_small_for_the_event_returns_buffer_full() {
        let mut enc = warmed(&["a"]);
        let mut buf = [0u8; 2];
        assert_eq!(
            enc.encode(&span_end("a", 0), &mut buf),
            Err(TracingEncodeError::BufferFull)
        );
    }

    #[test]
    fn a_buffer_holding_only_the_dictionary_frame_returns_buffer_full() {
        let mut enc = TraceEncoder::new();
        let mut probe = [0u8; MAX_TRACE_BURST_SIZE];
        let dictionary_len = {
            let mut e = TraceEncoder::new();
            let n = e
                .encode(&span_start("main_task", 0), &mut probe)
                .unwrap()
                .len;
            n - 1 // everything but the trailing event frame
        };
        let mut buf = vec![0u8; dictionary_len];
        assert_eq!(
            enc.encode(&span_start("main_task", 0), &mut buf),
            Err(TracingEncodeError::BufferFull)
        );
    }

    #[test]
    fn a_maximum_length_name_still_fits_one_frame() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        let longest = "x".repeat(32);
        let outcome = enc
            .encode(&span_start(&longest, u64::MAX / 2), &mut buf)
            .unwrap();
        let frames = decode_all(&buf[..outcome.len]);
        assert_eq!(frames[0].name.as_str(), longest);
        assert!(outcome.len <= MAX_TRACE_BURST_SIZE);
    }

    // ── Decoding failures ─────────────────────────────────────────────────────

    #[test]
    fn decode_truncated_frame_returns_error() {
        let mut enc = warmed(&["a"]);
        let (bytes, _) = encode_one(&mut enc, &span_end("a", 0));
        assert_eq!(
            decode_trace_frame(&bytes[..bytes.len() - 1]),
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
    fn decode_varint_overflow_returns_malformed_varint() {
        let overlong: Vec<u8> = (0..10).map(|_| 0xFF).collect();
        assert_eq!(decode_varint(&overlong), None);
        let frame: Vec<u8> = overlong.into_iter().chain(std::iter::once(0x00)).collect();
        assert_eq!(
            decode_trace_frame(&frame),
            Err(TracingDecodeError::MalformedVarint)
        );
    }

    #[test]
    fn decode_rejects_an_unknown_event_type() {
        // event_type is field 3, varint: tag 0x18, value 9 — past TRACE_START.
        let payload = [0x18u8, 0x09];
        let frame = [&[payload.len() as u8][..], &payload[..]].concat();
        assert_eq!(
            decode_trace_frame(&frame),
            Err(TracingDecodeError::DecodeError)
        );
    }

    #[test]
    fn decode_returns_the_consumed_byte_count() {
        let mut enc = warmed(&["a"]);
        let (bytes, _) = encode_one(&mut enc, &marker("a", 0, Some(5)));
        let (_, consumed) = decode_trace_frame(&bytes).unwrap();
        assert_eq!(consumed, bytes.len());
    }

    // ── A whole stream ────────────────────────────────────────────────────────

    #[test]
    fn a_full_stream_round_trips_through_a_host_style_decoder() {
        let mut enc = TraceEncoder::new();
        let mut buf = [0u8; MAX_TRACE_BURST_SIZE];
        let mut wire = Vec::new();

        let n = enc
            .encode_trace_start(1_000, TimeBase::Nanoseconds, 0, 0x1F, &mut buf)
            .unwrap();
        wire.extend_from_slice(&buf[..n]);

        let events = [
            span_start_with_deadline("gyro_isr", 1_100),
            marker("ukf_predict", 1_150, Some(7)),
            span_end("gyro_isr", 1_400),
            span_start_with_deadline("gyro_isr", 2_100),
            span_end("gyro_isr", 2_380),
        ];
        for ev in &events {
            let outcome = enc.encode(ev, &mut buf).unwrap();
            wire.extend_from_slice(&buf[..outcome.len]);
        }

        // Host side: resolve ids through the dictionary, accumulate the deltas.
        let mut dictionary: std::collections::HashMap<u32, std::string::String> =
            std::collections::HashMap::new();
        let mut clock = 0u64;
        let mut expect_sequence = 0u32;
        let mut resolved: Vec<(std::string::String, u64, FrameKind)> = Vec::new();

        for frame in decode_all(&wire) {
            assert_eq!(frame.sequence, expect_sequence, "no gaps in a clean stream");
            expect_sequence = (expect_sequence + 1) % SEQUENCE_MODULUS;
            clock = match frame.kind {
                FrameKind::NameRegistered | FrameKind::TraceStart => frame.timestamp_ticks,
                _ => clock + frame.timestamp_ticks,
            };
            match frame.kind {
                FrameKind::NameRegistered => {
                    dictionary.insert(frame.name_id, frame.name.as_str().into());
                }
                FrameKind::TraceStart => assert_eq!(frame.source_mask, 0x1F),
                kind => resolved.push((dictionary[&frame.name_id].clone(), clock, kind)),
            }
        }

        assert_eq!(
            resolved,
            vec![
                ("gyro_isr".into(), 1_100, FrameKind::SpanStart),
                ("ukf_predict".into(), 1_150, FrameKind::Marker),
                ("gyro_isr".into(), 1_400, FrameKind::SpanEnd),
                ("gyro_isr".into(), 2_100, FrameKind::SpanStart),
                ("gyro_isr".into(), 2_380, FrameKind::SpanEnd),
            ]
        );
        assert_eq!(dictionary.len(), 2, "each name registered exactly once");
    }
}
