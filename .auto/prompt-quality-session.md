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

### Session findings

- Baseline (run 1): held-out MRR 0.950617; Hit@5 0.9815; Hit@1 0.9259.
  CLI and MCP were identical, confirming the shared storage pipeline dominates.
  Weak categories were correction 0.875 and already_told 0.9167.
- Keep (run 2): FTS candidate limit 20 -> 50 and vector fetch 10 -> 20 at
  top_k=5. Held-out MRR 0.950617 -> 0.966667; Hit@5 became 1.0 and
  correction rose to 0.9583. This is the useful candidate-pool win.
- Discard (run 3): FTS 50 -> 100; no quality change. Discard (run 9):
  vector 20 -> 50; MRR fell to 0.981481 and entity_exact to 0.95, with
  p95 latency 3.47s. Discard (run 10): intermediate hybrid handoff 10 ->
  50; no quality change. Do not widen pools further on this corpus without
  better score normalization.
- Keep (run 4): coverage-aware fusion, conversational FTS stopwords,
  hyphen/slash compound tokenization, and `superseded_by` demotion. Held-out
  MRR 0.966667 -> 0.990741; already_told and correction reached 1.0.
  Coverage must be paired with supersession demotion: otherwise a stale
  record containing more query words can be boosted above its replacement.
- Discard (runs 5-8): coverage-only aliases, FTS aliases, structured type
  priors, and a 0.35/0.35 keyword/vector rebalance did not improve the
  protected suite. FTS aliases did improve a fresh 32-query paraphrase probe
  (MRR 0.750 -> 0.829), showing candidate recall remains a real live-corpus
  concern even when this small protected set is saturated.
- Keep (run 11): generic `host`/`serve` coverage normalization fixed the last
  protected structured-query miss; held-out CLI+MCP MRR and Hit@1/Hit@5 are
  now 1.0. The fresh probe remains only MRR 0.713 / Hit@1 0.594, so this is
  not evidence that the synthetic suite generalizes to live paraphrases.

### Final implementation

- `SearchConfig::fts_candidate_limit` defaults to 50.
- Vector candidates use `limit * 4` (20 for top_k=5) in storage/CLI/MCP.
- Shared retrieval rescoring applies light stemming, compound tokenization,
  coverage factor 0.6-1.4, conversational query stopwords, host/serve
  normalization, and a 0.35 factor for superseded records.
- `benchmark/retrieval` remains untouched; the new `.auto` corpus/evals are
  fixed artifacts and must be replaced or augmented with exported live Hermes
  queries before further tuning.
