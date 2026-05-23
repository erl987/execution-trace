#!/usr/bin/env bash
# Regenerate Python protobuf bindings from proto/tracing.proto.
#
# Run from anywhere inside the repo.
#
# Usage:
#   bash scripts/generate_protos.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${REPO_ROOT}/python/src/execution_trace/_proto"

protoc \
    --python_out="${OUT}" \
    --pyi_out="${OUT}" \
    --proto_path="${REPO_ROOT}/proto" \
    "${REPO_ROOT}/proto/tracing.proto"

echo "Generated → ${OUT}/tracing_pb2.{py,pyi}"
