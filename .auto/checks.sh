#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"

bash -n install.sh scripts/test-hermes-adoption.sh 2>/dev/null || bash -n install.sh

if rustup component list --installed 2>/dev/null | grep -q '^rustfmt'; then
  cargo fmt --check >/tmp/mnemosyne-autoresearch-fmt.log 2>&1 || {
    tail -80 /tmp/mnemosyne-autoresearch-fmt.log
    exit 1
  }
else
  echo "rustfmt component unavailable; skipping format check" >&2
fi

# Storage-layer retrieval tests (keyword/hybrid/vector behavior lives there).
cargo test --release --lib storage:: -- --test-threads=2 \
  >/tmp/mnemosyne-autoresearch-storage.log 2>&1 || {
  grep -E "^(test result|failures:|---- )" /tmp/mnemosyne-autoresearch-storage.log | head -40
  tail -40 /tmp/mnemosyne-autoresearch-storage.log
  exit 1
}

# MCP surface tests (public recall contract).
cargo test --release --lib mcp::server::tests -- --test-threads=1 \
  >/tmp/mnemosyne-autoresearch-mcp.log 2>&1 || {
  grep -E "^(test result|failures:|---- )" /tmp/mnemosyne-autoresearch-mcp.log | head -40
  tail -40 /tmp/mnemosyne-autoresearch-mcp.log
  exit 1
}

# Optional feature profiles must keep compiling.
cargo check --release --locked --features full,distributed --bin mnemosyne \
  >/tmp/mnemosyne-autoresearch-combined.log 2>&1 || {
  tail -60 /tmp/mnemosyne-autoresearch-combined.log
  exit 1
}
