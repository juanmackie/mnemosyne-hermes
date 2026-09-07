#!/bin/bash
# Full lib suite (854 tests). Run alongside checks.sh whenever src/storage or
# connection/transaction semantics change; checks.sh only runs the 20 storage::
# and 1 mcp::server:: tests that the inherited gate covered.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"
cargo test --release --lib -- --test-threads=4 2>&1 | tail -25 | grep -E "^test result|^failures|panicked" || true
cargo test --release --lib > /tmp/mnemosyne-full-lib.log 2>&1 || {
  grep -E "^test result|^failures:|panicked at" /tmp/mnemosyne-full-lib.log | head -30
  exit 1
}
grep -E "^test result" /tmp/mnemosyne-full-lib.log | tail -1
