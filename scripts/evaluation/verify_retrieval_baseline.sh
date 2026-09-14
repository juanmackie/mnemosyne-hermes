#!/bin/bash
# scripts/evaluation/verify_retrieval_baseline.sh — proposed cached evaluation (design only)
# Planning deliverable item 6. Not executed (no deployed DB evaluation performed).
# Contracts preserved: namespace=agent:hermes; dataset identity preserved.
set -euo pipefail

echo "=== Proposed retrieval baseline verification (design) ==="
echo "Status: Design only — not executed against deployed DB."
echo "Requires: corrected Python stack (BGE standardization — item 3) applied first."
echo "Requires: dataset identity recorded (`.auto/eval_result_YYYY-MM-DD.json` format)."
echo "Requires: `MNEMOSYNE_EMBEDDING_MODEL` = bge-small-en-v1.5 verified before evaluation."
echo "Proposed dataset identity checks:"
echo "  sha256sum .auto/eval_dev.jsonl .auto/eval_heldout_a.jsonl .auto/eval_heldout_b.jsonl"
echo "  sha256sum .auto/corpus.jsonl"
echo "Proposed code identity checks:"
echo "  git rev-parse HEAD"
echo "  python --version"
echo "  python -c \"from mnemosyne_rust_hermes.config import default_config; cfg=default_config(); print(cfg.effective_identity())\""
echo "Proposed evaluation commands:"
echo "  .auto/evaluate.py --namespace agent:hermes --db-path <DB> --corpus .auto/corpus.jsonl --eval .auto/eval_dev.jsonl"
echo "  .auto/evaluate_mcp.py --namespace agent:hermes --mcp-args 'mcp'"
echo "Note: `.auto/evaluate.py` does not currently verify embedding model identity."
echo "Note: `.auto/evaluate_mcp.py` reports `mcp_latency_overhead` but does not verify model identity."
echo "Status: Design complete."
