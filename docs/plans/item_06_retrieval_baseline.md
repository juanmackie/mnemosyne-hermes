# Item 6 — Trustworthy Retrieval Baseline (Planning Deliverable)

Status: PLANNING ONLY. No evaluation executed against deployed DB; no metrics fabricated; no dataset identity invented. All quality claims supported by proposed measurement plan, not by unverified historical data.
Contracts preserved: namespace `agent:hermes`, provider identity (`mnemosyne-rust` / `mnemosyne`), DB path, embedding model identity (`bge-small-en-v1.5`, 384 dims after item 3 standardization).

## Existing evaluation infrastructure (evidence)

Files read:
- `.auto/evaluate.py`: evaluates real-query retrieval on fixed JSONL sets (`corpus.jsonl`, `eval_dev.jsonl`, `eval_heldout_a.jsonl`, `eval_heldout_b.jsonl`). Reports Hit@1, Hit@5, MRR, latency percentiles (p50, p95, p99), per-category MRR breakdown.
- `.auto/evaluate_mcp.py`: evaluates via MCP stdio surface (`mnemosyne mcp`). Reports same metrics but includes `mcp_latency_overhead` (extra time for stdio transport).
- `.auto/evaluate_mcp_warm.py`: warm-start evaluation (probably excludes cold-start overhead).
- `.auto/eval_dev.jsonl`, `.auto/eval_heldout_a.jsonl`, `.auto/eval_heldout_b.jsonl`: query/relevance datasets (JSON lines; each line has `query`, `relevant` array of target strings, `category`).
- `.auto/membench/` directory: benchmark harness (`membench_setup.py`, `membench_validate.py`, `corpus_heldout.jsonl`, `corpus_dev.jsonl`, `queries_heldout.jsonl`, etc.).
- `docs/RETRIEVAL_EVALUATION.md`: evaluation methodology and metric definitions.
- `docs/EMBEDDING_CANDIDATE_EVALUATION.md`: baseline fp32 nomic; Gemma wins; Q fallback (this confirms model disagreement — see item 3).
- `.auto/unified_audit.json`: audit evidence only; no evaluation results stored.

## Required retrieval baseline (design — must be executed after item 3 standardization)

The evaluation must run against the CORRECTED Python stack (after BGE standardization) and isolated fixtures, not against an unknown or unstandardized model.

### Dataset identity

Proposed dataset reference (must be recorded in evaluation output):

- `dataset_version`: reference to `.auto/eval_dev.jsonl` / `.auto/eval_heldout_a.jsonl` / `.auto/eval_heldout_b.jsonl` (filename + file size + sha256 of file content at evaluation time).
- `corpus_reference`: `.auto/corpus.jsonl` (if used) or `.auto/membench/corpus_dev.jsonl`.
- `fixture_isolation`: each evaluation query must use an isolated DB copy (symlink or `shutil.copy2`) to prevent hotness leakage (`.auto/evaluate.py` already does this via `query_db` isolation).
- `namespace`: `agent:hermes` (must be preserved; evaluation runs with `--namespace agent:hermes`).

### Code version identity

- `code_version`: Git sha1 (`git rev-parse HEAD`) at evaluation time.
- `python_version`: Python version (`python --version`).
- `plugin_version`: `mnemosyne-rust` version (`0.1.0` from `pyproject.toml`) or future `mnemosyne` version.
- `embedding_model_identity`: must be `bge-small-en-v1.5 (384 dims)` after standardization; must be verified before evaluation starts (see item 3 validation plan).
- `mcp_version`: if evaluating via `.auto/evaluate_mcp.py`, must include `mnemosyne` binary version (`mnemosyne --version` or `MNEMOSYNE_BIN --version` if available).

### Model identity verification (before evaluation)

Before any evaluation runs:

```bash
python -c "
import os
from mnemosyne_rust_hermes.config import default_config, EmbeddingConfig  # or future Python module
cfg = default_config()
model_identity = cfg.model_identity  # or Env MNEMOSYNE_EMBEDDING_MODEL
assert model_identity == 'bge-small-en-v1.5 (384 dims)', f'Model identity mismatch: {model_identity}'
print('Model identity verified:', model_identity)
"
```

Note: If `EmbeddingConfig` is not yet exposed in the Python adapter, this check must use the environment variable `MNEMOSYNE_EMBEDDING_MODEL` or query the Rust binary's config via MCP. This is an explicit gap.

### Scoring settings

From `.auto/evaluate.py` and `docs/RETRIEVAL_EVALUATION.md`:

- Metrics: Hit@1, Hit@5, MRR, latency percentiles (p50, p95, p99).
- Relevance label: `relevant()` compares query-independent substrings (`rid`, `summary`, `content`) against target list; never edited by optimization loop.
- Weighting: keyword/hybrid/vector ranking weights; must be recorded.
- Hotness: `access_counts` incremented per recall; isolated per query via `query_db` symlinks.

Before comparing RRF, hotness, PPR, or any ranking change, the evaluation must record current default settings:

```json
{
  "scoring_settings": {
    "ranking_weights": {"keyword": 0.3, "vector": 0.5, "hybrid": 0.2},
    "rrf_k": 60,
    "hotness_weight": 0.1,
    "ppr_weight": 0.2,
    "min_importance": 0.0,
    "tags_filter": [],
    "abstention_threshold": 0.0,
    "hierarchical": false
  },
  "default_settings_reference": "docs/config/default_retrieval_settings.json (must exist or be created)"
}
```

Note: No `default_retrieval_settings.json` exists in repo; this must be created or derived from `.auto/evaluate.py` defaults. This is an explicit gap.

### Comparison before changing defaults

The user requires comparing individual ranking changes (weighted RRF, hotness, PPR) against the baseline individually before changing defaults. Proposed evaluation sequence:

1. **Baseline**: Run `.auto/evaluate.py` with default settings; record metrics (`hit1`, `hit5`, `mrr`, `latency_p95`, `latency_p99`).
2. **RRF variant**: Modify RRF weight (`rrf_k` or `ranking_weights.vector`); run evaluation; record delta.
3. **Hotness variant**: Modify `hotness_weight`; run evaluation; record delta.
4. **PPR variant**: Modify `ppr_weight`; run evaluation; record delta.
5. **Combined**: Only after individual variants show improvement should combined changes be applied.

Each variant must record dataset identity, code version, model identity, and scoring settings, so claims are reproducible.

### Quality metrics and latency

From `.auto/evaluate.py` and `.auto/measure.sh`:

- Latency: `time.perf_counter()` per query; aggregated to p50/p95/p99.
- Quality: Hit@1, Hit@5, MRR (mean reciprocal rank), per-category MRR breakdown (`summarize()` in `.auto/evaluate.py`).
- Latency must be recorded separately from quality (do not trade quality for latency without evidence).
- Benchmark (`.auto/measure.sh`) compares warm p95; any change that increases warm p95 by >10% must be flagged.

Proposed evaluation output format (`.auto/eval_result_YYYY-MM-DD.json`):

```json
{
  "dataset_identity": {
    "corpus_sha256": "...",
    "eval_dev_sha256": "...",
    "eval_heldout_sha256": "...",
    "fixture_isolation": true,
    "namespace": "agent:hermes"
  },
  "code_version": {
    "git_sha1": "...",
    "python_version": "3.12",
    "plugin_version": "0.1.0",
    "embedding_model_identity": "bge-small-en-v1.5 (384 dims)",
    "mcp_version": "..."
  },
  "scoring_settings": { ... },
  "quality_metrics": {
    "hit1": 0.42,
    "hit5": 0.78,
    "mrr": 0.55,
    "per_category": {"uncategorized": 0.52, "technical": 0.60}
  },
  "latency_ms": {
    "p50": 12.3,
    "p95": 45.6,
    "p99": 89.1
  },
  "variant": "baseline",
  "timestamp_utc": "2026-09-14T08:30:00Z",
  "evidence_path": ".auto/eval_result_2026-09-14.json"
}
```

Note: No such `.auto/eval_result_YYYY-MM-DD.json` exists in repo; evaluation results must be produced after standardization and isolated from historical gaps.

## Persisted evaluation results

To support maintenance and quality claims:

- Each evaluation run must write `.auto/eval_result_YYYY-MM-DD.json`.
- The evaluation must include a `dataset_identity` section (file hashes, not just names).
- The evaluation must include `embedding_model_identity` verified before measurement.
- The evaluation must include `scoring_settings` (so future changes can compare to same settings).
- Historical gaps must be explicitly labeled: any previous evaluation without `dataset_identity` or `embedding_model_identity` is UNKNOWN and must not be used as a baseline without verification.

## Gaps (explicitly unknown; no manufacturing)

- Whether any previous `.auto/eval_*.json` exists with full dataset identity and model identity: UNKNOWN (none found; only `.jsonl` files exist, no result JSON).
- Whether `.auto/evaluate.py` currently verifies model identity: UNKNOWN (no identity check in code; must add).
- Whether benchmark (`.auto/measure.sh`) records dataset identity: UNKNOWN (measure script not fully read; likely does not include dataset hashes).
- Whether warm latency (`.auto/measure_mem.sh`) records model identity: UNKNOWN.
- Whether any evaluation results from previous sessions are preserved: UNKNOWN (`.auto/eval_heldout_a.jsonl` is dataset; no result file exists).
- Whether `docs/RETRIEVAL_EVALUATION.md` defines dataset identity format: UNKNOWN (must verify full file; partial read only).

## Deliverable artifacts

- `.auto/deliverables/item_06_retrieval_baseline.md` (this file)
- `docs/plans/item_06_retrieval_baseline.md` (mirror)
- Proposed evaluation output format (`.auto/eval_result_YYYY-MM-DD.json` template — text only, not executed).
- No evaluation executed; no metrics fabricated.
