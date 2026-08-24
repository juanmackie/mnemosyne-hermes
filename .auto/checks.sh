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

# The measure step already built the release binary. Avoid rebuilding the
# same target in a second cargo check; the focused MCP test still compiles the
# library/test harness, while the full suite remains release validation.
cargo test --release --lib mcp::server::tests -- --test-threads=1 \
  >/tmp/mnemosyne-autoresearch-mcp.log 2>&1 || {
  tail -80 /tmp/mnemosyne-autoresearch-mcp.log
  exit 1
}
