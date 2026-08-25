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
cli_eval_log=""
mcp_eval_log=""
trap 'rm -f "$adoption_log" "${cli_eval_log:-}" "${mcp_eval_log:-}"' EXIT
if ! ./scripts/test-hermes-adoption.sh >"$adoption_log" 2>&1; then
  tail -80 "$adoption_log"
  exit 1
fi
grep '^METRIC ' "$adoption_log"

# Rebuild the fixed corpus from the public CLI. This never edits the source
# evaluation datasets or their relevance labels.
python3 .auto/setup_data.py >/dev/null

cli_eval_log="$(mktemp)"
mcp_eval_log="$(mktemp)"
for dataset in eval_dev.jsonl eval_heldout_a.jsonl eval_heldout_b.jsonl; do
  python3 .auto/evaluate.py \
    --binary target/release/mnemosyne \
    --db .auto/data/template.db \
    --dataset ".auto/$dataset" \
    --mode both \
    --workers 4 | tee -a "$cli_eval_log" >/dev/null
  # Exercise the same recall operation through the public MCP stdio surface.
  python3 .auto/evaluate_mcp.py \
    --binary target/release/mnemosyne \
    --db .auto/data/template.db \
    --dataset ".auto/$dataset" \
    --mode flat \
    --workers 4 | tee -a "$mcp_eval_log" >/dev/null
done

python3 - "$cli_eval_log" "$mcp_eval_log" <<'PY'
import json
import pathlib
import statistics
import sys


def read(path):
    rows = [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line.strip()]
    return {pathlib.Path(row["dataset"]).name: row for row in rows}

cli = read(sys.argv[1])
mcp = read(sys.argv[2])
cli_flat = {name: row["modes"]["flat"] for name, row in cli.items()}
cli_hier = {name: row["modes"]["hierarchical"] for name, row in cli.items()}
mcp_flat = {name: row["modes"]["flat"] for name, row in mcp.items()}
heldout_names = ("eval_heldout_a.jsonl", "eval_heldout_b.jsonl")
mean = lambda key, items: statistics.mean(item[key] for item in items)
cli_heldout = [cli_flat[name] for name in heldout_names]
mcp_heldout = [mcp_flat[name] for name in heldout_names]
# The primary measures the public adoption path, not only its CLI wrapper.
print(f"METRIC recall_heldout_mrr={statistics.mean([mean('mrr', cli_heldout), mean('mrr', mcp_heldout)]):.6f}")
print(f"METRIC recall_cli_heldout_mrr={mean('mrr', cli_heldout):.6f}")
print(f"METRIC recall_mcp_heldout_mrr={mean('mrr', mcp_heldout):.6f}")
print(f"METRIC recall_dev_mrr={cli_flat['eval_dev.jsonl']['mrr']:.6f}")
print(f"METRIC recall_heldout_hit5={mean('hit5', cli_heldout):.6f}")
print(f"METRIC recall_mcp_heldout_hit5={mean('hit5', mcp_heldout):.6f}")
print(f"METRIC recall_heldout_hierarchical_mrr={mean('mrr', [cli_hier[name] for name in heldout_names]):.6f}")
print(f"METRIC recall_latency_p95_ms={max([row['latency_p95_ms'] for row in cli_flat.values()] + [row['latency_p95_ms'] for row in mcp_flat.values()]):.3f}")
PY

python3 - target/release/mnemosyne <<'PY'
import pathlib
import sys
binary = pathlib.Path(sys.argv[1])
print(f"METRIC binary_size_mib={binary.stat().st_size / (1024 * 1024):.4f}")
PY
