#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTFLAGS="${RUSTFLAGS:-}"

# Build the candidate being measured; no stale binary can produce a score.
cargo build --release --bin mnemosyne >/dev/null

# Rebuild a pristine corpus for every measurement. Recall updates access
# metadata, so reusing a prior database contaminates hierarchical hotness.
python3 .auto/setup_data.py >/dev/null

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
python3 .auto/evaluate.py --binary "$ROOT/target/release/mnemosyne" --db .auto/data/template.db --dataset .auto/eval_dev.jsonl --mode both --workers 1 >"$TMP"
DEV_JSON="$(cat "$TMP")"
python3 .auto/evaluate.py --binary "$ROOT/target/release/mnemosyne" --db .auto/data/template.db --dataset .auto/eval_heldout_a.jsonl --mode both --workers 1 >"$TMP"
HOLDOUT_A_JSON="$(cat "$TMP")"
python3 .auto/evaluate.py --binary "$ROOT/target/release/mnemosyne" --db .auto/data/template.db --dataset .auto/eval_heldout_b.jsonl --mode both --workers 1 >"$TMP"
HOLDOUT_B_JSON="$(cat "$TMP")"

python3 - "$DEV_JSON" "$HOLDOUT_A_JSON" "$HOLDOUT_B_JSON" <<'PY'
import json
import sys

dev, holdout_a, holdout_b = map(json.loads, sys.argv[1:])

def modes(payload):
    return payload["modes"]["flat"], payload["modes"]["hierarchical"]

def mean_metric(payload, key):
    flat, hierarchical = modes(payload)
    return (flat[key] + hierarchical[key]) / 2.0

def print_metric(name, value):
    print(f"METRIC {name}={value:.8f}")

# Primary metric: average MRR across independent flat and hierarchical paths.
print_metric("dev_mrr", mean_metric(dev, "mrr"))
print_metric("dev_hit1", mean_metric(dev, "hit1"))
print_metric("dev_hit5", mean_metric(dev, "hit5"))
print_metric("dev_flat_mrr", modes(dev)[0]["mrr"])
print_metric("dev_hierarchical_mrr", modes(dev)[1]["mrr"])
print_metric("heldout_a_mrr", mean_metric(holdout_a, "mrr"))
print_metric("heldout_b_mrr", mean_metric(holdout_b, "mrr"))
print_metric("heldout_mrr", (mean_metric(holdout_a, "mrr") + mean_metric(holdout_b, "mrr")) / 2.0)
print_metric("dev_latency_p50_ms", mean_metric(dev, "latency_p50_ms"))
print_metric("dev_latency_p95_ms", mean_metric(dev, "latency_p95_ms"))
print_metric("heldout_empty", sum(modes(payload)[0]["empty"] + modes(payload)[1]["empty"] for payload in (holdout_a, holdout_b)))
PY
