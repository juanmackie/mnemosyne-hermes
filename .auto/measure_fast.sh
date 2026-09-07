#!/bin/bash
# Autoresearch measurement: build + run the Hermes recall latency bench.
# Emits `METRIC name=value` lines (primary: hybrid_ppr_p95_ms).
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"
cd "$(dirname "$0")/.."

RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

# Fast pre-check: a syntax/type error should fail in seconds, not after ingest.
if ! cargo build --release --bench hermes_recall_bench >"$RAW" 2>&1; then
  echo "BUILD FAILED"
  grep -E "^(error|warning: unused)" -A6 "$RAW" | head -60
  exit 1
fi

BIN=$(ls -t target/release/deps/hermes_recall_bench-* 2>/dev/null | grep -v '\.d$' | head -1)
if [ -z "$BIN" ]; then
  echo "BUILD FAILED: bench binary not found"
  tail -20 "$RAW"
  exit 1
fi

if ! BENCH_MEMORIES="${BENCH_MEMORIES:-2000}" \
   BENCH_DEGREE="${BENCH_DEGREE:-6}" \
   BENCH_REPEATS="${BENCH_REPEATS:-2}" \
   BENCH_CONC=1 \
   "$BIN" >"$RAW" 2>&1; then
  echo "BENCH FAILED"
  tail -30 "$RAW"
  exit 1
fi

grep -E "^(METRIC|note)" "$RAW"
