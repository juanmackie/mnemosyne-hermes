# Autoresearch: PrecisionMemBench retrieval precision

## Goal

Increase PrecisionMemBench active passes from the measured baseline **39/43**
toward **43/43**, while reducing over-return failures and keeping missing
expected beliefs <= 1. All results are **public-suite tuned**, not independent
holdout evidence (the suite is the only available evaluator for this target).

The originally requested "18 -> 35" framing came from a stale `RESULTS.md`
table (§7). Commit `9eb0320` already added the P48 structured-label narrowing
and raised the live baseline to 39/43; the §7 table was not updated. Verified
by a fresh run on 2026-11-16 with the pinned patched engine and embeddings on.

## Metric

`score = activePasses * 100 - overreturnFailures - 10 * missingExpectedBeliefs`

Primary readout is `activePasses` (out of 43). Ties break on fewer over-return
failures, then fewer missing beliefs. `pass` is true when
`activePasses == 43`, `missingExpectedBeliefs <= 1`, `overreturnFailures <= 5`.

Baseline: `activePasses=39, overreturnFailures=8, missingExpectedBeliefs=1`,
score **3882**.

## Evaluator

`research/evaluate.py` — runs `bench.precisionmembench.run_eval.run()` against
the real provider + pinned patched engine, returns `{"pass": bool, "score": number}`.

Environment: an activated virtual environment outside the repo with
`mnemosyne-memory[embeddings]==3.15.1` + `scripts/apply_engine_patches.py`.
`PYTHONPATH` must include the repo root and `integrations/hermes-provider`.

## Constraints

- Do not change the pinned fixtures, the engine version, or the engine pin.
- Do not edit the vendored snapshot without a `# LOCAL PATCH:` marker, a
  `PATCHES.md` entry, and an updated `VENDORED_FROM.json` hash/bytes/lines.
- Keep the change behind the existing P48 "all rows labeled" guard so legacy
  unlabeled rows and shared-surface rows keep their current behavior.
- Keyless; no LLM, no network at eval time (the embedding model is cached).
- Guardrail: `bench/scorecard/gate.py` must stay green (instrument soundness),
  and the synthetic held-out scorecard must not regress. Any keep requires the
  guardrail check.

## Search space

1. P48 label matching: exact token phrase (current) vs bounded 1-edit fuzzy
   match (Damerau-Levenshtein for transpositions).
2. Candidate scoring: content-token overlap as an additional signal when no
   label matches.
3. Recall `top_k` / `maxBeliefs` budget and multi-scope interleaving.
4. Query-time label expansion (harder: provider does not index labels).

## Failures at baseline (all in "Fuzzy matching and prefix guards")

| case | failure |
| --- | --- |
| messy-typo-blind-spot | extra `b-redis-code`, `b-auth-depends-redis` |
| fuzzy-single-edit-fires-non-pinned | extra `b-functional-pipeline` |
| fuzzy-single-term-only-signal | extra `b-dead-letter-queue` |
| fuzzy-two-edit-transposition-blocked | missing `b-ts-pref` |

## History

| iter | change | score | passes | over | missing | verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 0 | baseline (P48 exact labels) | 3882 | 39 | 8 | 1 | baseline |
| 1 | P49 1-edit fuzzy label evidence | 4185 | 42 | 5 | 1 | kept (score +303) |
| 2 | P50 zero-result label query expansion | 4294 | 43 | 6 | 0 | kept (score +109) |
| 3 | prefix guard in shared one-edit check | 4295 | 43 | 5 | 0 | kept; **target met** |
| 3-A/A | determinism re-run | 4295 | 43 | 5 | 0 | identical |
| guard | scorecard gate + repo checks + 800-probe held-out arm | — | — | — | — | all green |
