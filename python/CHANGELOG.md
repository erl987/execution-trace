# Changelog

All notable changes to `embedded-etrace` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

The Rust crate this package decodes for ships from the same repository as
[`execution-trace`](../CHANGELOG.md) and is released from the same tag, so the two
version numbers move together.

## [0.2.0] - 2026-09-19

**A breaking release.** This decodes the v2 wire format and **cannot decode a v1 stream**,
which is what `execution-trace` 0.1.x firmware emits; 0.1.x of this package cannot decode
a v2 one. A v2 stream is recognised by the `TRACE_START` frame at its head. Recordings
made with either version must be decoded by their own.

### Added

- `TraceStreamState` — the per-connection decoder state v2 requires. A frame carries a
  dictionary id instead of a name and a delta instead of a timestamp, so it is not
  self-contained: this holds the name dictionary, the running clock, the timebase the
  `TRACE_START` frame declares, and the sequence tracker. One per connection; the device
  restarts its dictionary and clock when it restarts.
  - `source_mask` exposes the source-group mask the firmware was built with, so a group
    that is silent by design can be told from one whose frames were lost.
  - `unresolved_events` counts events dropped because their name id never arrived.
- `NameEntry` — one dictionary entry: the name plus everything it fixes (source type,
  priority, relative deadline), sent once rather than on every occurrence.
- `GapRecord` and `TraceEventBuffer.gaps` — a stretch the sequence numbers say was lost,
  emitted as a row of its own so that missing data looks missing. Written as a `gap` row
  by `write_tracing_csv` and drawn as a hatched band across every lane, labelled with the
  frame count.
- `TraceEvent.interrupted` and an `interrupted` CSV column — the span was cut short by a
  gap rather than closed by its own `SPAN_END`, so its real end is unknown and at least
  `end_us`. Such a span is exempt from the deadline check, may be zero-length, and is
  given a minimum render width so it stays visible.
- `SequenceTracker` takes `modulus` and `reorder_window`.
- `SEQUENCE_MODULUS` (16 384), the value the v2 counter wraps at.

### Changed

- **`decode_tracing_stream(buf, state, event_buffer)` takes a `TraceStreamState` where it
  took a `SequenceTracker`.** The tracker is now held inside the state, along with the
  rest of what a stateful decode needs.
- **`SequenceTracker` tolerates reordering** when given a `reorder_window` (the trace
  stream uses 64, covering the firmware's 48-slot channel). A frame is numbered by its
  producer rather than at the wire, so a dictionary frame is written ahead of the event
  that triggered it but numbered after it — inverting a pair on every first sight of a
  name. A hole is now declared only once something lands more than the window past it;
  stragglers close their own hole, and one arriving after its hole was reported is
  ignored rather than read as a backwards jump. Single-producer streams keep the window
  at zero and get the report immediately.
- A hole is placed in time from the frames either side of it, not from whichever frame
  triggered the report — tens of milliseconds apart on a real trace.
- `etrace-decode` writes gap rows. Without them the CSV shows an unexplained hole and
  interrupted spans with nothing beside them to say why.
- An unresolvable name id drops the event rather than renaming it to a placeholder.
  Attributing distinct unknown ids to one `<unknown>` lane interleaved unrelated spans,
  so a `SPAN_END` could close a `SPAN_START` belonging to another name and produce a
  malformed pair.
- The "never registered" warning fires once per id. At 1 300+ events/s the per-event
  warning produced thousands of identical lines a second.

### Fixed

- `write_tracing_csv` wrote a marker value of `0` as an empty CSV cell, and likewise a
  span `deadline_us` or `value` of `0`. The column is meant to be empty only when the
  field is absent, but the writer tested the value for truth, and zero is a legitimate
  payload — a reason code, a count of nothing, a deadline at the activation instant.
  Downstream the empty cell reads as "no value": `diagram.py` renders it as `—`, hiding
  exactly the case the marker was emitted to report. The decoder was always correct;
  only the writer dropped it.
- A lost frame no longer leaves its span open until the next unrelated `SPAN_END` for
  that name, which drew one enormous bar across the gap and past it. Spans open *at the
  hole* are closed and marked interrupted, and the first `SPAN_END` arriving for such a
  name afterwards is discarded rather than paired with whatever opens next, which would
  invent a span that never ran.
- `decode_tracing_stream` no longer clears the byte buffer on a device reset. The stream
  is length-prefixed with no sync marker, so discarding bytes mid-frame misaligned
  everything after it unrecoverably — the source of the decode-failure bursts that
  arrived at the same millisecond as each false reset. The frames after a reset are
  intact; only the decoder state is stale.
- The frame that reveals a reset is no longer discarded with it. That frame is the first
  of the new run, usually the reboot's own header carrying the timebase and mask.
- A reboot is separated from ordering jitter by a margin rather than a strict backwards
  comparison. A reboot drops the clock by the whole uptime and jitter is microseconds;
  without the margin, one inverted microsecond blinded the trace for a full refresh
  interval.
- A repeated `TRACE_START` on a running stream is a dictionary refresh, not a reset.
  Treating it as a reboot cleared the dictionary on every refresh, undoing the thing the
  refresh exists to do. A reboot is identified by the device clock going backwards, which
  a refresh never does.

## [0.1.6] - 2026-05-28

No changes to this package; released from the same tag as the crate.

## [0.1.5] - 2026-05-25

### Changed

- Improved the package README.

## [0.1.4] - 2026-05-24

### Added

- Initial extraction from `analysis/src/tools/` into a standalone package.
- `SequenceTracker`, `iter_frames`, `decode_varint`, `encode_varint` in `stream.py`.
- `TraceEvent`, `MarkerRecord`, `TraceEventBuffer`, `decode_tracing_stream`,
  `write_tracing_csv` in `decode.py`.
- Zoomable Bokeh timing diagram in `diagram.py` (optional `[diagram]` extra).
- `etrace-decode` CLI — decode a raw binary trace file to CSV.
- `etrace-diagram` CLI — generate an HTML timing diagram from a CSV.
- PEP 561 `py.typed` marker for static analysis.
- Full Google-style docstrings on all public API surface.
- Test suite with coverage for stream primitives, decoder, and diagram.
