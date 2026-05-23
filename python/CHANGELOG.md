# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
