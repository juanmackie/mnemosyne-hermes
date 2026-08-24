#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"

bash -n install.sh
bash -n scripts/test-hermes-adoption.sh
if rustup component list --installed 2>/dev/null | grep -q '^rustfmt'; then
  cargo fmt --check >/tmp/mnemosyne-autoresearch-fmt.log 2>&1 || {
    tail -80 /tmp/mnemosyne-autoresearch-fmt.log
    exit 1
  }
else
  echo "rustfmt component unavailable; skipping format check" >&2
fi

# Phase 1 changes are concentrated in the CLI/MCP boundary. Keep the
# backpressure check focused enough to run on every iteration; the full suite
# remains a release-validation job rather than a per-experiment timeout.
cargo check --release --bin mnemosyne >/tmp/mnemosyne-autoresearch-check.log 2>&1 || {
  tail -80 /tmp/mnemosyne-autoresearch-check.log
  exit 1
}
cargo test --release --lib mcp::server::tests -- --test-threads=1 \
  >/tmp/mnemosyne-autoresearch-mcp.log 2>&1 || {
  tail -80 /tmp/mnemosyne-autoresearch-mcp.log
  exit 1
}
