# Changelog

All notable changes to the `execution-trace` crate are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

The Python decoder ships from this repository as
[`embedded-etrace`](python/CHANGELOG.md) and is released from the same tag, so the two
version numbers move together.

## [0.2.0] - 2026-09-19

**A breaking release.** The wire format is v2 and is not compatible with the v1 format
0.1.x produced: a firmware built on 0.2.0 needs `embedded-etrace` 0.2.0 on the host, and
a recording made with either version must be decoded by its own. A v2 stream is
recognised by the `TRACE_START` frame at its head.

### Wire format

- Every frame class now travels in one flat `TraceFrame` message discriminated by
  `event_type`, rather than a `oneof` envelope. proto3 omits unset fields, so an event
  pays nothing for the dictionary fields it does not use, where an envelope taxes each
  one with a tag and a length. Field numbers stay at or below 15 so every tag is a
  single byte.
- Names are interned. A `NAME_REGISTERED` frame carries the name once and assigns a
  dictionary id, along with everything else the name fixes — source type, priority and
  relative deadline — and per-occurrence events carry only the id.
- Timestamps are deltas against the previous sequenced frame. `TRACE_START` and
  `NAME_REGISTERED` carry an absolute timestamp and re-establish the origin.
- `timestamp_ticks` is `sint64`, not unsigned. An event is stamped when recorded rather
  than when queued, so an ISR preempting a task between those points legitimately
  produces a negative delta; clamping it to zero drifted the host's reconstructed clock
  permanently ahead of the device's, at milliseconds per second on a real trace. Zigzag
  costs nothing at these magnitudes.
- Sequence numbers wrap at 16 384 (`SEQUENCE_MODULUS`). The counter exists only to make
  gaps visible, and a gap is read modulo the wrap, so a full 32-bit counter would spend
  up to five varint bytes to buy nothing. Because a wrapped restart only looks backwards
  from the low half of the range, `TRACE_START` — not the sequence — is now the reliable
  reset signal.
- A new `TRACE_START` header declares the timebase (nanoseconds, or core cycles with
  `core_frequency_hz`) and the source-group mask the build was compiled with, so a host
  can tell a masked group from one whose frames were lost without hard-coding the mask.

Measured steady state is 11–12 bytes per event over the delta range the reference
firmware produces, against 27 for v1.

### Added

- `TraceEncoder`, holding the three pieces of per-stream state the format needs: the
  name dictionary, the delta base, and the sequence counter. Built for its clock with
  `TraceEncoder::new()` (nanoseconds) or `TraceEncoder::with_cycle_counter(hz)`.
- `TraceEncoder::encode_dictionary_entry`, for walking the dictionary and re-emitting it
  periodically. Without this, a host attaching mid-run receives no dictionary at all and
  can resolve nothing — names are registered in the first control cycles after boot, and
  the device cannot observe an attach over a one-way transport.
- `TraceEncoder::encode_trace_start`, `registered_names`, and the `Encoded` result, whose
  `name_registry_full` flag reports that an event went out under `UNKNOWN_NAME_ID`
  because the dictionary was full.
- `RawTraceFrame`, `FrameKind` and `TimeBase`, the decoded form of a single frame.
- `next_sequence()`, the process-global producer-side counter.
- `MAX_TRACE_BURST_SIZE`, `NAME_REGISTRY_CAPACITY`, `SEQUENCE_MODULUS`, `UNKNOWN_NAME_ID`.
- `TraceSink::ticks_per_us`, `tick_mask` and `ticks_since`, so that code measuring an
  interval works in whichever timebase the sink reports. All three default to the
  nanosecond case, so existing implementations need no change.

### Changed

- **`TraceSink::get_elapsed_nanoseconds` is now `now_ticks`.** The unit is whatever the
  encoder for the stream declares, so the old name would be a lie on a cycle-counter
  sink. A sink may return a free-running 32-bit counter widened to `u64`: the encoder
  extends it, so reading the clock can be a single volatile load with no critical
  section.
- **`decode_trace_frame` returns `RawTraceFrame`, not `TraceEvent`.** In v2 an event is
  not recoverable from one frame — its name is a dictionary id and its timestamp is a
  delta — so name resolution and delta accumulation belong to the host decoder, which
  holds the per-stream state.
- **Frames are numbered at the producer**, by the recording layer immediately before it
  hands the event to the transport, and by the encoder for the header and dictionary
  frames it originates itself. Loss at the producer queue used to happen before any
  number existed, so the host saw a contiguous stream with events simply absent; it now
  leaves a hole. Nothing on the wire changed — only who fills the field.
  - The cost is that arrival order is no longer numeric order: a dictionary frame is
    written ahead of the event that triggered it but numbered after it, inverting a pair
    on every first sight of a name. Hosts must tolerate a reorder window.
- `NAME_REGISTRY_CAPACITY` is 40, down from 64. This array is the largest thing the
  format puts in RAM, and under flip-link every byte of `.bss` is a byte the stack does
  not get. 40 is half again as many entries as the reference firmware registers, and
  overshooting degrades the trace rather than breaking it.
- Interning is keyed on name content rather than on the `&'static str` pointer. By the
  time an event reaches the encoder the literal's pointer is gone — `TraceEvent` carries
  an inline copy. The scan is linear over at most `NAME_REGISTRY_CAPACITY` entries, with
  a length check before any byte comparison, in the task that owns the wire.

### Removed

- `SequenceEncoder`, replaced by `TraceEncoder`. Sequencing moved to the producer and the
  encoder now owns the dictionary and the delta base as well, so the old wrapper no
  longer describes anything.

### Fixed

- A name first seen on a `SPAN_END` — its `SPAN_START` dropped upstream under load — is
  backfilled when a `SPAN_START` for it arrives, so a dictionary refresh cannot re-send a
  bare entry and lose that name's priority for the rest of the run.
- A marker value of `0` is written to the wire instead of being dropped as falsy.

## [0.1.6] - 2026-05-28

### Added

- The `enabled` feature flag. With it off, the recording API compiles to no-ops with
  default implementations, needing neither a clock nor a transport, and the protobuf
  codegen is skipped entirely.

### Changed

- README: dropped a Cargo.toml example that showed a less clean way to disable tracing.

## [0.1.5] - 2026-05-25

### Changed

- Improved the crate and Python READMEs.

## [0.1.4] - 2026-05-24

Releases up to and including this one predate this file; see the git history for
0.1.0–0.1.4.
