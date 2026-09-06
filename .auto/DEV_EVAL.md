# Development / Regression Evaluation Policy

This document defines how the `.auto` retrieval evaluation sets may be used,
per the "(D)" bench item. TL;DR: the existing sets are **development and
regression** evidence — not a frozen independent test oracle. Any further
tuning must freeze a **fresh, never-seen** test set first.

## Current sets are tuned-on

The files below have been used to drive, and have therefore been **fitted
against**, the ranking changes logged in `log.jsonl`:

- `.auto/eval_dev.jsonl`
- `.auto/eval_heldout_a.jsonl`
- `.auto/eval_heldout_b.jsonl`

`log.jsonl` records an iterative optimization loop (e.g. runs 2–16+: candidate
pool expansion, coverage reranking, fusion rebalancing, alias groups, …). Each
change was accepted or discarded by reading metrics off these three sets. That
makes them **development/regression evidence**: they are still the right place
to catch regressions and to keep iterating on the dev loop, but **good numbers
here no longer prove generalization**.

## Freeze an independent test set before tuning

To claim a result on *unseen* data, or before any further ranking tuning:

1. **Freeze** a fresh independent test set (new queries + a corpus revision the
   optimizer has never read) whose items are not present in any of the current
   sets.
2. Record its git revision and file path alongside the result (see
   `benchmark/retrieval/README.md` → "Reporting results"). If you mutate the
   corpus or queries, those are *also* a test-set revision and must be frozen
   before tuning.
3. Run **one** evaluation on the frozen set as a final gate — do not iterate on
   it. If you change ranking code or the eval set, the previous "test" result is
   void and a new frozen set is required.
4. Keep the frozen set out of `log.jsonl`'s tuning loop; log it separately as a
   one-shot holdout.

## What the current sets remain good for

- **Regression detection**: run them on every change; a dip here signals a
  regression even if it is now "tuned-in."
- **Dev iteration**: cheap local iteration before any independent claim.
- **Cross-provider comparison** (`measure.sh` profiles): the *relative* gap
  between keyless-default / model-backed / python-provider is meaningful even
  on tuned sets, because the sets were not tuned to favor one provider.

**Rule of thumb:** a number from `eval_dev`/`eval_heldout_*` is a
*development* number. Label it as such. Only a number from a frozen,
post-tuning, never-iterated set is an *independent* test number.
