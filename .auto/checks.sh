#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"

bash -n scripts/test-hermes-adoption.sh
if rustup component list --installed 2>/dev/null | grep -q '^rustfmt'; then
  cargo fmt --check >/tmp/mnemosyne-autoresearch-fmt.log 2>&1 || {
    tail -80 /tmp/mnemosyne-autoresearch-fmt.log
    exit 1
  }
else
  echo "rustfmt component unavailable; skipping format check" >&2
fi

for filter in 'storage::' 'hierarchy::' 'intent::' 'mcp::'; do
  log="/tmp/mnemosyne-autoresearch-${filter//:/_}.log"
  cargo test --release --lib "$filter" -- --test-threads=2 >"$log" 2>&1 || {
    tail -80 "$log"
    exit 1
  }
done
