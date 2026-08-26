#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"

# Live stack contract: local bge-small-en-v1.5 (384-dim). Do not change the
# model for ranking experiments; encoder swaps are out of scope this session.
export MNEMOSYNE_EMBEDDING_MODEL="${MNEMOSYNE_EMBEDDING_MODEL:-bge-small-en-v1.5}"

# Build the candidate before measuring so stale binaries cannot pass.
cargo build --release --locked --features local-embeddings --bin mnemosyne >/dev/null

# Rebuild the fixed corpus only when its fingerprint changes (corpus content,
# embedding model, or setup version). Ranking-only iterations reuse the cache.
python3 .auto/setup_data.py >/dev/null

cli_log="$(mktemp)"
mcp_log="$(mktemp)"
trap 'rm -f "$cli_log" "$mcp_log"' EXIT

for dataset in eval_dev eval_heldout_a eval_heldout_b; do
  python3 .auto/evaluate.py \
    --binary target/release/mnemosyne \
    --db .auto/data/template.db \
    --dataset ".auto/${dataset}.jsonl" \
    --workers 6 | tee -a "$cli_log" >/dev/null
done

# The live Hermes agent talks through MCP; measure held-out splits there too.
for dataset in eval_heldout_a eval_heldout_b; do
  python3 .auto/evaluate_mcp.py \
    --binary target/release/mnemosyne \
    --db .auto/data/template.db \
    --dataset ".auto/${dataset}.jsonl" \
    --workers 6 | tee -a "$mcp_log" >/dev/null
done

python3 - "$cli_log" "$mcp_log" <<'PY'
import json
import pathlib
import statistics
import sys


def read(path):
    rows = [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line.strip()]
    return {pathlib.Path(row["dataset"]).name: row for row in rows}


cli = read(sys.argv[1])
mcp = read(sys.argv[2])
heldout_names = ("eval_heldout_a.jsonl", "eval_heldout_b.jsonl")
cli_heldout = [cli[name] for name in heldout_names]
mcp_heldout = [mcp[name] for name in heldout_names]
mean = lambda key, items: statistics.mean(item[key] for item in items)

print(f"METRIC realquery_heldout_mrr={statistics.mean([mean('mrr', cli_heldout), mean('mrr', mcp_heldout)]):.6f}")
print(f"METRIC realquery_cli_heldout_mrr={mean('mrr', cli_heldout):.6f}")
print(f"METRIC realquery_mcp_heldout_mrr={mean('mrr', mcp_heldout):.6f}")
print(f"METRIC realquery_dev_mrr={cli['eval_dev.jsonl']['mrr']:.6f}")
print(f"METRIC realquery_heldout_hit5={statistics.mean([mean('hit5', cli_heldout), mean('hit5', mcp_heldout)]):.6f}")
print(f"METRIC realquery_heldout_hit1={statistics.mean([mean('hit1', cli_heldout), mean('hit1', mcp_heldout)]):.6f}")
print(f"METRIC recall_latency_p95_ms={max([row['latency_p95_ms'] for row in cli.values()] + [row['latency_p95_ms'] for row in mcp.values()]):.3f}")

# Per-category diagnostics across CLI+MCP held-out splits: where the next
# yield is hiding. Emitted as INFO lines (not METRIC) for ASI annotation.
cats = {}
for source in cli_heldout + mcp_heldout:
    for cat, stats_row in source["by_category"].items():
        cats.setdefault(cat, []).append(stats_row["mrr"])
for cat in sorted(cats):
    print(f"INFO category_mrr[{cat}]={statistics.mean(cats[cat]):.4f}")
PY
