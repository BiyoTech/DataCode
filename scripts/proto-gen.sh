#!/usr/bin/env bash
# Proto code generation script for Go and Python services.
# Requires: buf (https://buf.build/docs/installation)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PROTO_DIR="$ROOT_DIR/proto"
GEN_DIR="$ROOT_DIR/gen"

echo "==> Cleaning generated code..."
rm -rf "$GEN_DIR"
mkdir -p "$GEN_DIR/go" "$GEN_DIR/python"

echo "==> Generating proto code with buf..."
cd "$PROTO_DIR"
buf generate

echo "==> Adding __init__.py for Python packages..."
find "$GEN_DIR/python" -type d -exec touch {}/__init__.py \;

echo "==> Proto code generation complete."
echo "    Go output:     $GEN_DIR/go/"
echo "    Python output:  $GEN_DIR/python/"
