#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTFLAGS="${RUSTFLAGS:-}"

# Build the candidate before measuring so stale binaries cannot pass.
cargo build --release --bin mnemosyne >/dev/null

./scripts/test-hermes-adoption.sh

python3 - "$ROOT/target/release/mnemosyne" <<'PY'
import os
import pathlib
import sys

binary = pathlib.Path(sys.argv[1])
size_mib = binary.stat().st_size / (1024 * 1024)
print(f"METRIC binary_size_mib={size_mib:.4f}")
PY
