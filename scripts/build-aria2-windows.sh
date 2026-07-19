#!/usr/bin/env bash
# Build Cove's pinned, patched 64-bit Windows aria2 backend.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT/build/aria2-win}"
IMAGE="cove-aria2-windows"

command -v docker >/dev/null 2>&1 || {
    echo "docker is required to build the patched Windows aria2 backend" >&2
    exit 1
}

mkdir -p "$OUTPUT_DIR"
docker build \
    --file "$ROOT/packaging/aria2/Dockerfile.mingw" \
    --tag "$IMAGE" \
    "$ROOT/packaging/aria2"

container="$(docker create "$IMAGE")"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
docker cp "$container:/aria2/src/aria2c.exe" "$OUTPUT_DIR/aria2c.exe"
docker cp "$container:/aria2-1.37.0-cove.1-source.tar.gz" \
    "$OUTPUT_DIR/aria2-1.37.0-cove.1-source.tar.gz"
sha256sum "$OUTPUT_DIR/aria2c.exe" \
    "$OUTPUT_DIR/aria2-1.37.0-cove.1-source.tar.gz"
