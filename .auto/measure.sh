#!/usr/bin/env bash
# Measure retrieval quality/latency across the THREE embedding/feature
# profiles separately. A single-profile run no longer hides which backend a
# result came from.
#
# NOTE: numbers from eval_dev/eval_heldout_* are DEVELOPMENT/regression
# evidence, not an independent test oracle. See .auto/DEV_EVAL.md.
#
#   keyless-default : `--release` (default features) -> deterministic hash
#                     fallback embeddings. No model runtime or download.
#   model-backed    : `--release --features local-embeddings` -> fastembed
#                     ONNX, model from MNEMOSYNE_EMBEDDING_MODEL.
#   python-provider : `--release --features python` -> DSPy/Python-backed
#                     modules enabled.
#
# Override with AUTO_MEASURE_PROFILES="label|features|model" (space-separated)
# to run only a subset, e.g. for CI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"

# Live stack contract: local bge-small-en-v1.5 (384-dim). Keep the model for
# ranking experiments; encoder swaps are out of scope this session.
export MNEMOSYNE_EMBEDDING_MODEL="${MNEMOSYNE_EMBEDDING_MODEL:-bge-small-en-v1.5}"

PROFILES="${AUTO_MEASURE_PROFILES:-
keyless-default||bge-small-en-v1.5
model-backed|local-embeddings|bge-small-en-v1.5
python-provider|python|bge-small-en-v1.5
}"

declare -a LOGS=()
trap 'rm -f "${LOGS[@]}"' EXIT

for entry in $PROFILES; do
  IFS='|' read -r label features model <<<"$entry"
  echo "== profile: $label (features='${features:-<default>}', model=$model) =="

  # Build only the profile's own feature set; copy aside so each profile has a
  # distinct binary and later passes cannot reuse a stale one.
  if [ -n "$features" ]; then
    cargo build --release --locked --features "$features" --bin mnemosyne >/dev/null
  else
    cargo build --release --locked --bin mnemosyne >/dev/null
  fi
  bin="target/release/mnemosyne-${label}"
  cp -f target/release/mnemosyne "$bin"

  export MNEMOSYNE_EMBEDDING_MODEL="$model"
  export MNEMOSYNE_EVAL_BIN="$bin"
  export MNEMOSYNE_EVAL_DB=".auto/data/template-${label}.db"
  export MNEMOSYNE_EVAL_LABEL="$label"

  # Rebuild the fixed corpus only when its fingerprint changes; the profile
  # label is part of the fingerprint so profiles never share a stale DB.
  python3 .auto/setup_data.py >/dev/null

  db=".auto/data/template-${label}.db"
  cli_log="$(mktemp)"
  mcp_log="$(mktemp)"
  rss_log="$(mktemp)"
  LOGS+=("$cli_log" "$mcp_log" "$rss_log")

  # Peak RSS, opportunistic: only where GNU time (-v) is available.
  measure_rss() { # $1 binary; appends peak RSS (KB) to $rss_log
    local bin="$1"
    if command -v /usr/bin/time >/dev/null 2>&1; then
      /usr/bin/time -v -- "$bin" --help >/dev/null 2>"$rss_log.t" || true
      grep -Eo 'Maximum resident set size.*[0-9]+' "$rss_log.t" \
        | grep -Eo '[0-9]+' >>"$rss_log" || true
    else
      echo 0 >>"$rss_log"
    fi
  }
  measure_rss "$bin"

  for dataset in eval_dev eval_heldout_a eval_heldout_b; do
    python3 .auto/evaluate.py \
      --binary "$bin" --db "$db" \
      --dataset ".auto/${dataset}.jsonl" \
      --workers 6 | tee -a "$cli_log" >/dev/null
  done

  # The live Hermes agent talks through MCP; measure held-out splits there too.
  for dataset in eval_heldout_a eval_heldout_b; do
    python3 .auto/evaluate_mcp.py \
      --binary "$bin" --db "$db" \
      --dataset ".auto/${dataset}.jsonl" \
      --workers 6 | tee -a "$mcp_log" >/dev/null
  done
  # Warm-server latency (the Hermes contract): ONE MCP process, serial
  # held-out calls, first 3 dropped. Only the live-stack profile.
  if [ "$label" = "model-backed" ]; then
    python3 .auto/warm_mcp.py \
      --binary "$bin" --db "$db" \
      --dataset .auto/eval_heldout_a.jsonl --dataset .auto/eval_heldout_b.jsonl \
      --metric realquery_warm_mcp_p95_ms
  fi
  unset MNEMOSYNE_EVAL_BIN MNEMOSYNE_EVAL_DB MNEMOSYNE_EVAL_LABEL
done

python3 - "${PROFILES}" "${LOGS[@]}" <<'PY'
import json
import pathlib
import statistics
import sys

# argv: labels string (entry list), then per profile: cli_log, mcp_log, rss_log.
lines = sys.argv[1].split()
per = 3
pairs = [tuple(sys.argv[2 + per * i: 2 + per * (i + 1)]) for i in range(len(lines))]


def read(path):
    rows = [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line.strip()]
    return {pathlib.Path(row["dataset"]).name: row for row in rows}


def rss_kb(path):
    vals = [int(v) for v in pathlib.Path(path).read_text().split() if v.isdigit()]
    return max(vals) if vals else 0


def size_bytes(bin_path):
    try:
        return pathlib.Path(bin_path).stat().st_size
    except OSError:
        return 0


heldout_names = ("eval_heldout_a.jsonl", "eval_heldout_b.jsonl")

for label, (cli_path, mcp_path, rss_path) in zip(lines, pairs):
    cli = read(cli_path)
    mcp = read(mcp_path)
    cli_heldout = [cli[name] for name in heldout_names]
    mcp_heldout = [mcp[name] for name in heldout_names]
    cli_dev = cli.get("eval_dev.jsonl", {})
    mean = statistics.mean
    cli_mrr = mean(row["mrr"] for row in cli_heldout)
    mcp_mrr = mean(row["mrr"] for row in mcp_heldout)
    latency = max([row["latency_p95_ms"] for row in cli.values()]
                  + [row["latency_p95_ms"] for row in mcp.values()])
    empty = sum(row["empty"] for row in list(cli.values()) + list(mcp.values()))
    print(f"METRIC {label}_cli_heldout_mrr={cli_mrr:.6f}")
    print(f"METRIC {label}_mcp_heldout_mrr={mcp_mrr:.6f}")
    print(f"METRIC {label}_dev_mrr={cli_dev.get('mrr', 0.0):.6f}")
    print(f"METRIC {label}_heldout_hit5={mean([mean(row.get('hit5', 0) for row in cli_heldout), mean(row.get('hit5', 0) for row in mcp_heldout)]):.6f}")
    print(f"METRIC {label}_heldout_hit1={mean([mean(row.get('hit1', 0) for row in cli_heldout), mean(row.get('hit1', 0) for row in mcp_heldout)]):.6f}")
    print(f"METRIC {label}_recall_latency_p95_ms={latency:.3f}")
    # Abstention proxy: recall returning zero results is arguably the model
    # refusing/being unable to retrieve anything for that query.
    print(f"METRIC {label}_empty_results={empty}")
    # Resource proxy: on-disk binary size (portable) and, where GNU time was
    # available, peak RSS (KB) measured at startup.
    print(f"METRIC {label}_binary_bytes={size_bytes('target/release/mnemosyne-%s' % label)}")
    print(f"METRIC {label}_peak_rss_kb={rss_kb(rss_path)}")
PY
