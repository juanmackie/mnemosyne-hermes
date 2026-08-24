# Autoresearch: Hermes personal-agent retrieval quality

## Objective
Improve Mnemosyne's retrieval path for a local Hermes-style personal agent. The
agent must recall durable preferences, project decisions, procedures, and safety
constraints accurately from a private local memory store. Prefer simple,
general retrieval improvements over benchmark-specific heuristics.

The fixed corpus in `.auto/corpus.jsonl` is intentionally query-independent.
`.auto/eval_dev.jsonl` is the development set. `.auto/eval_heldout_a.jsonl`
and `.auto/eval_heldout_b.jsonl` cover disjoint topics and phrasing styles and
are guardrails against overfitting; do not tune directly to them.

## Metrics
- **Primary**: `dev_mrr` (unitless, higher is better) — mean reciprocal rank
  averaged across flat and hierarchical retrieval on the development set.
- **Secondary quality**: `dev_hit1`, `dev_hit5`, per-mode MRR, `heldout_mrr`,
  `heldout_a_mrr`, and `heldout_b_mrr`.
- **Secondary systems**: `dev_latency_p50_ms` and `dev_latency_p95_ms`.
  A quality win that causes a large latency or memory regression is not useful
  for a personal agent.

## How to Run
`./.auto/measure.sh` builds the candidate, evaluates all three fixed datasets
through the public `mnemosyne recall` CLI, and emits `METRIC name=value` lines.
Run `./.auto/checks.sh` after a candidate's benchmark passes and before keeping
it. Rebuild the fixed corpus only with `python3 .auto/setup_data.py` when the
storage format or ingestion path intentionally changes.

## Files in Scope
- `src/cli/recall.rs` — public CLI composition of keyword, vector, graph, and
  hierarchical ranking signals.
- `src/storage/libsql.rs` — FTS/vector/graph retrieval and score computation.
- `src/config.rs` — retrieval defaults and safety-preserving configuration.
- `src/hierarchy.rs` — generic hierarchical reranking and convergence behavior.
- `src/intent.rs` — offline query intent planning when it demonstrably affects
  retrieval quality or avoids unnecessary retrieval.
- `benchmark/retrieval/locomo_eval.py` — generic benchmark tooling only; never
  add dataset-specific rules.

The `.auto` harness may be improved for measurement correctness, but the fixed
corpus and labels are not tuning knobs. Keep production changes narrowly
related to retrieval/ranking unless an experiment demonstrates a necessary
boundary change.

## Off Limits
- Do not add query strings, target summaries, dataset names, or benchmark
  labels to production source code.
- Do not special-case evaluation queries, memory IDs, tags, or exact expected
  answers.
- Do not alter or delete the fixed datasets to make a result look better.
- Do not disable correctness checks, suppress retrieval failures, or use a
  different code path in measurement than a real Hermes/MCP client uses.
- Do not add network calls, external answer models, or cloud data to the score.
- Do not commit `.auto/data/memory.db` or embedding caches.

## Constraints
- Preserve the keyless/local-first behavior documented in
  `docs/HERMES_INTEGRATION.md`: core memory operations work without a cloud API
  key and fail visibly when a required ranking signal cannot be produced.
- Keep MCP stdout clean and preserve the public CLI JSON contract.
- `./.auto/checks.sh` must pass before a candidate can be kept.
- Keep only a real improvement: target at least +0.005 absolute `dev_mrr`,
  require held-out average MRR not to fall by more than 0.02, and reject large
  latency regressions. Equal-quality simpler code is preferred.
- Measure from a clean committed candidate. Discard failed or regressed
  candidates with `git restore`/`git revert` as appropriate.
- Record every run in `.auto/log.jsonl`, including discarded and crashed runs,
  with the idea, metrics, decision, and what was learned.

## Honest evaluation protocol
1. Establish and record a baseline before changing production code.
2. Change one coherent idea at a time; do not tune on held-out labels.
3. Run `./.auto/measure.sh`; inspect all quality and latency metrics.
4. If dev improves, run `./.auto/checks.sh` and compare both held-out sets.
5. Keep only when the guardrails pass. Otherwise revert the candidate and log
   why. Re-run a noisy speed result before treating it as meaningful.
6. Periodically compare the best candidate against the baseline and report
   generalization, not just the best dev score.

## What's Been Tried
- Harness run 0: concurrent evaluation crashed with SQLite locks because
  recall updates access metadata. The harness now serializes public CLI calls.
- Harness runs 1-2: ordinary apostrophes and trailing punctuation exposed FTS5
  syntax errors. The production query escaper now quotes every token literally,
  with a focused unit test; checks pass.
- Run 3 / ranking baseline at `5fc93d0`: dev MRR 0.4763, held-out average MRR
  0.4357, dev p50/p95 latency 1550.9/1757.8 ms. These are baseline numbers
  after correctness hardening, not an optimization claim.
- Runs 4-5: tried routing the CLI through one configured storage hybrid scorer;
  it compiled after an import fix but regressed dev MRR to 0.3752 and held-out
  average MRR to 0.1500. Reverted. The ad hoc CLI merge is currently the
  stronger baseline; future changes need measured evidence, not abstraction
  consistency alone.
- Do not repeat an idea already recorded in `.auto/log.jsonl` unless the new
  run changes an explicit assumption.
