# Autoresearch: Real-query retrieval pipeline quality (candidate pool + coverage reranking)

## Objective

Improve episodic-recall retrieval quality for the live Hermes agent stack by
optimizing the **retrieval pipeline**, not the encoder. Evidence from the live
corpus: four alternative encoders scored identically and Gemma was materially
worse, while the biggest historical wins came from BM25 handling, candidate
management, and ranking logic. The encoder stays fixed at
**BAAI/bge-small-en-v1.5 (384-dim)** — pinned via `MNEMOSYNE_EMBEDDING_MODEL`
in measure.sh.

Primary lever hypotheses (user-directed plan):
1. Candidate pools are too small: FTS keyword search hard-caps at `LIMIT 20`,
   vector search fetches `limit * 2` (= 10 for top_k=5). Relevant memories die
   before reranking. Test pool sizes 20 -> 50 -> 100.
2. Fusion/reranking rewards single-token OR matches instead of coverage.
   Reward candidates covering multiple query terms/entities, exact model
   numbers, versions, URLs; penalize one-token OR matches.
3. Supersession-awareness: `superseded_by` is stored but ignored by search;
   corrected facts compete with their replacements. Penalize superseded rows.
4. Structured queries (identity/config/ownership/current-state) should prefer
   canonical/current records.

## Metrics

- **Primary**: `realquery_heldout_mrr` (higher is better) — mean MRR@5 over
  held-out splits A+B measured through BOTH the public CLI and the MCP stdio
  surface (`mnemosyne_recall`), flat mode, top_k=5, local bge embeddings.
- Secondary: `realquery_dev_mrr` (iteration guidance only), 
  `realquery_heldout_hit5`, `realquery_heldout_hit1`,
  `recall_latency_p95_ms` (regression monitor).
- Per-category CLI+MCP held-out MRR is printed as `INFO category_mrr[...]`
  lines: correction / already_told / structured_current / entity_exact /
  multi_constraint. Use these to localize wins/regressions.

## Benchmark data (fixed — do not edit during the loop)

`.auto/corpus.jsonl` — 177 realistic personal-agent memory records in
namespace `project:personal-agent-eval`: identity/config/hardware/accounts,
10 correction pairs wired with explicit `supersedes` edges, duplicate
"I already told you" facts, ~40 noisy episodic blobs that mention many
entities tangentially, and per-record `age_days` backdating so recency is
realistic.

Eval sets (27 queries each): `.auto/eval_dev.jsonl`,
`.auto/eval_heldout_a.jsonl`, `.auto/eval_heldout_b.jsonl`. Categories:
correction, already_told, structured_current, entity_exact,
multi_constraint. Relevance labels are substrings of intended record content.

Anti-overfit rules:
- NEVER change corpus.jsonl, eval sets, or labels during the loop.
- Treat a change as real only if held-out A and B both improve (or are flat)
  alongside the primary mean; dev-only wins are suspect.
- Do not special-case query strings from the eval sets in code. Ranking logic
  must be generic.

The benchmark is synthetic-but-realistic: it mirrors the user's described live
failure modes because the actual live query log is not available here. When
the user later exports real queries, they can replace the JSONL files without
code changes (format documented in evaluate.py).

## How to Run

`./.auto/measure.sh` — builds release binary with `--features local-embeddings`,
rebuilds the corpus DB only when its fingerprint changes, evaluates CLI
(dev+a+b) and MCP (a+b), prints `METRIC` lines plus per-category INFO lines.
Typical iteration cost: incremental build + ~90 CLI + 54 MCP invocations.

If recall crashes with schema/corruption errors after storage-layer changes,
delete the cached DB to force a clean rebuild: `rm -rf .auto/data`.

## Files in Scope

- `src/storage/libsql.rs` — FTS `LIMIT 20` caps in `keyword_search`;
  `hybrid_search` fusion weights/pool sizes; supersession handling in SQL.
- `src/storage/mod.rs` — StorageBackend trait signatures if pool sizing needs
  parameterization.
- `src/cli/recall.rs` — CLI recall pipeline: hybrid+vector merge (0.4/0.3),
  candidate counts (`limit * 2`), reranking hooks.
- `src/mcp/tools.rs` — MCP recall pipeline (same shape as CLI; this is what
  the live Hermes agent calls).
- `src/config.rs` — SearchConfig fields (add e.g. `fts_candidate_limit`,
  coverage-rerank toggles). Keep defaults backward-compatible unless the
  benchmark says otherwise.
- New helper module for coverage scoring if needed (e.g. `src/utils/retrieval.rs`).

## Off Limits

- Embedding model/dimension changes; no new embedding deps. bge-small-en-v1.5
  @ 384 dims is the frozen live stack.
- Benchmark data files and relevance labels (see above).
- Public MCP tool names/schemas; Hermes compatibility aliases must keep working.
- No remote API calls during evaluation (offline local-only path).
- Do not game latency by weakening correctness checks.

## Constraints

- `.auto/checks.sh` must pass: rustfmt, storage lib tests, MCP server tests,
  combined full+distributed feature compile.
- Existing unit tests around keyword/BM25 ranking behavior may need updating
  ONLY if their intent is preserved; do not delete assertions to make changes pass.
- Keep fail-closed retrieval semantics intact.

## What's Been Tried (live-corpus history, pre-session)

- Encoder sweep on live corpus: EmbeddingGemma/nomic/bge variants identical;
  Gemma materially worse + rotary-cache errors on long episodic memories +
  OOM pressure. REJECTED — do not revisit.
- Already ported from Rust fork upstream work: BM25 relevance ordering in FTS5
  (bm25() rank kept + normalized per query), stopword/modality filtering of FTS
  queries, deterministic-fallback vectors excluded from ranking when model-backed
  embeddings unavailable, importance demoted below keyword/vector signals.
- Curated dev eval reached MRR ≈ 0.93 — too small to optimize against safely;
  this session's larger real-query-style sets exist to expose the residual
  failures (see per-category baselines once recorded).

### Session findings (update as experiments accumulate)

- (baseline pending — record per-category numbers and pool-size sweep results here)
