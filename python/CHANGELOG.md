# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed

- `write_tracing_csv` wrote a marker value of `0` as an empty CSV cell, and likewise a span
  `deadline_us` or `value` of `0`. The column is meant to be empty only when the field is
  absent, but the writer tested the value for truth, and zero is a legitimate payload — a
  reason code, a count of nothing, a deadline at the activation instant. Downstream the empty
  cell reads as "no value": `diagram.py` renders it as `—`, hiding exactly the case the marker
  was emitted to report. The decoder was always correct; only the writer dropped it.

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
