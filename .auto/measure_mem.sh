#!/usr/bin/env bash
# membench scoreboard: primary metric + frozen non-regression guards.
#
#   primary  membench_heldout_score    = mean of 5 category scores, held-out persona
#   dev      membench_dev_*            = same on the tuning persona (tune here)
#   guards   realquery_*                = frozen 177-record corpus (warm MCP + CLI)
#            recall_latency_warm_p95_ms = warm per-call mnemosyne_recall p95
#
# Live-stack profile only: `--release --features local-embeddings` with the
# pinned bge-small-en-v1.5 encoder — exactly the stack Hermes runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"
export MNEMOSYNE_EMBEDDING_MODEL="${MNEMOSYNE_EMBEDDING_MODEL:-bge-small-en-v1.5}"

BIN="target/release/mnemosyne-membench"
cargo build --release --locked --features local-embeddings --bin mnemosyne >/dev/null
cp -f target/release/mnemosyne "$BIN"

# ---- membench scoreboard -----------------------------------------------------
for split in dev heldout; do
  MNEMOSYNE_EVAL_BIN="$BIN" python3 .auto/membench_setup.py \
    --corpus ".auto/membench/corpus_${split}.jsonl" \
    --db ".auto/data/membench-${split}.db" \
    --namespace "project:membench-${split}" --label "membench-${split}" >/dev/null
  python3 .auto/evaluate_membench.py --binary "$BIN" --db ".auto/data/membench-${split}.db" \
    --dataset ".auto/membench/queries_${split}.jsonl" \
    --namespace "project:membench-${split}" --prefix "membench_${split}"
done

# ---- frozen-corpus guards (must not regress) ---------------------------------
FROZEN_DB=".auto/data/template-model-backed.db"
MNEMOSYNE_EVAL_BIN="$BIN" MNEMOSYNE_EVAL_DB="$FROZEN_DB" MNEMOSYNE_EVAL_LABEL="model-backed" \
  python3 .auto/setup_data.py >/dev/null

for split in a b; do
  python3 .auto/evaluate.py --binary "$BIN" --db "$FROZEN_DB" \
    --dataset ".auto/eval_heldout_${split}.jsonl" --workers 6 \
    >".auto/data/frozen-cli-${split}.json"
  python3 .auto/evaluate_mcp_warm.py --binary "$BIN" --db "$FROZEN_DB" \
    --dataset ".auto/eval_heldout_${split}.jsonl" >".auto/data/frozen-warm-${split}.json"
done

python3 - <<'PY'
import json, pathlib, statistics

warm = [json.loads(pathlib.Path(f".auto/data/frozen-warm-{s}.json").read_text()) for s in "ab"]
cli = [json.loads(pathlib.Path(f".auto/data/frozen-cli-{s}.json").read_text()) for s in "ab"]
mean = statistics.mean
print(f"METRIC realquery_warm_mrr={mean(r['mrr'] for r in warm):.6f}")
print(f"METRIC realquery_warm_hit1={mean(r['hit1'] for r in warm):.6f}")
print(f"METRIC realquery_heldout_mrr={mean(r['mrr'] for r in cli):.6f}")
print(f"METRIC realquery_heldout_hit1={mean(r['hit1'] for r in cli):.6f}")
print(f"METRIC realquery_heldout_hit5={mean(r['hit5'] for r in cli):.6f}")
PY

# Warm-server latency: one MCP process, serial held-out calls, first 3 dropped.
p95=$(python3 .auto/warm_mcp.py --binary "$BIN" --db "$FROZEN_DB" \
  --dataset .auto/eval_heldout_a.jsonl --dataset .auto/eval_heldout_b.jsonl \
  --metric warm_mcp_p95_ms 2>/dev/null | head -1)
p95="${p95#*=}"
echo "METRIC recall_latency_warm_p95_ms=${p95}"
echo "METRIC realquery_warm_mcp_p95_ms=${p95}"
rm -f .auto/data/frozen-cli-*.json .auto/data/frozen-warm-*.json
