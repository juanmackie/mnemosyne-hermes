#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTFLAGS="${RUSTFLAGS:-}"

# Build the candidate before measuring so stale binaries cannot pass.
cargo build --release --locked --bin mnemosyne >/dev/null

# Keep the adoption contract as a hard part of the recall workload: retrieval
# improvements must not regress the public local-first provider surface.
adoption_log="$(mktemp)"
eval_log=""
trap 'rm -f "$adoption_log" "${eval_log:-}"' EXIT
if ! ./scripts/test-hermes-adoption.sh >"$adoption_log" 2>&1; then
  tail -80 "$adoption_log"
  exit 1
fi
grep '^METRIC ' "$adoption_log"

# Rebuild the fixed corpus from the public CLI. This never edits the source
# evaluation datasets or their relevance labels.
python3 .auto/setup_data.py >/dev/null

eval_log="$(mktemp)"
for dataset in eval_dev.jsonl eval_heldout_a.jsonl eval_heldout_b.jsonl; do
  python3 .auto/evaluate.py \
    --binary target/release/mnemosyne \
    --db .auto/data/template.db \
    --dataset ".auto/$dataset" \
    --mode both \
    --workers 4 | tee -a "$eval_log" >/dev/null
done

python3 - "$eval_log" <<'PY'
import json
import pathlib
import statistics
import sys

rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if line.strip()]
by_name = {pathlib.Path(row["dataset"]).name: row for row in rows}
flat = {name: row["modes"]["flat"] for name, row in by_name.items()}
hier = {name: row["modes"]["hierarchical"] for name, row in by_name.items()}
heldout = [flat["eval_heldout_a.jsonl"], flat["eval_heldout_b.jsonl"]]
heldout_hier = [hier["eval_heldout_a.jsonl"], hier["eval_heldout_b.jsonl"]]
mean = lambda key, items: statistics.mean(item[key] for item in items)
print(f"METRIC recall_heldout_mrr={mean('mrr', heldout):.6f}")
print(f"METRIC recall_dev_mrr={flat['eval_dev.jsonl']['mrr']:.6f}")
print(f"METRIC recall_heldout_hit5={mean('hit5', heldout):.6f}")
print(f"METRIC recall_heldout_hierarchical_mrr={mean('mrr', heldout_hier):.6f}")
print(f"METRIC recall_latency_p95_ms={max(row['latency_p95_ms'] for row in flat.values()):.3f}")
PY

python3 - target/release/mnemosyne <<'PY'
import pathlib
import sys
binary = pathlib.Path(sys.argv[1])
print(f"METRIC binary_size_mib={binary.stat().st_size / (1024 * 1024):.4f}")
PY
