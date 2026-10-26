# Retrieval results

Date: 2026-10-22. Engine: `mnemosyne-memory` 3.15.1. Harness:
`bench/retrieval/run_eval.py` (see [README.md](README.md)).

## The change

`core/beam.py`, `_minimum_recall_relevance`:

| Query terms | Before | After |
| --- | --- | --- |
| 1-2 | 0.15 | 0.15 |
| 3 | 0.50 | 0.15 |
| 4 or more | 0.30 | 0.15 |

The function is the lexical admission floor for working-memory recall. A row
enters the result set only when its lexical relevance reaches this floor. The
stepped values rejected rows that answer a multi-term question with one precise
term. For example, `"laptop specs - what machine do I have?"` matches
`"My laptop is a ThinkPad X1 Carbon Gen 12 with 64GB RAM running Ubuntu 24.04."`
on one of three terms, so the row scored 0.33, fell under 0.50, and the query
returned nothing.

Queries with no meaningful token overlap still abstain. `_lexical_relevance`
returns 0.0 for them, which stays below the floor.
`tests/test_engine_recall_scope.py::test_zero_overlap_query_still_returns_nothing`
guards that case.

## Measured result

Each split has 27 queries. Higher is better.

| Split | Baseline MRR | Candidate MRR | Delta | Baseline empty | Candidate empty |
| --- | --- | --- | --- | --- | --- |
| dev | 0.6605 | 0.7438 | +0.0833 | 4 | 0 |
| heldout a | 0.7654 | 0.7840 | +0.0185 | 2 | 1 |
| heldout b | 0.7315 | 0.8549 | +0.1235 | 4 | 0 |
| **mean** | **0.7191** | **0.7942** | **+0.0751** | | |

Baseline and candidate use the same harness on the same fixtures. Baseline is
the previous audited patch state (`patched_sha256` `4ef04dc5...`). Candidate is
the new patch state (`patched_sha256` `3e3fe10f...`, `patch_sha256`
`d37de37e...`). The candidate numbers also reproduce from a fresh copy of the
3.15.1 wheel after `scripts/apply_engine_patches.py`, so the result belongs to
the shipped patch artifact and not to an ad-hoc edit.

The baseline is stable across the two engine builds that were tested. The
partially patched engine installed in the local Hermes venv (`a0f31415...`)
produced the same 0.6605 / 0.7654 / 0.7315.

## Category detail

Only categories that changed are listed.

| Category | Split | Baseline MRR | Candidate MRR | Delta |
| --- | --- | --- | --- | --- |
| already_told | dev | 0.5000 | 0.7000 | +0.2000 |
| correction | dev | 0.6250 | 0.6667 | +0.0417 |
| entity_exact | dev | 0.4500 | 0.6500 | +0.2000 |
| entity_exact | heldout a | 0.8500 | 0.7500 | -0.1000 |
| multi_constraint | heldout a | 0.8333 | 1.0000 | +0.1667 |
| correction | heldout b | 0.5833 | 0.8056 | +0.2222 |
| entity_exact | heldout b | 0.6000 | 1.0000 | +0.4000 |

The single regression is `entity_exact` on heldout a: one query moved from
rank 1 to rank 2. It has five queries, so one rank flip is -0.1000 MRR. Both
other splits improve in that category, and the heldout mean still rises.

## Relation to the archived Rust tuning

This is **not** a literal port of the archived constants. The record is
[docs/archive/RUST_RETRIEVAL_TUNING.md](../../docs/archive/RUST_RETRIEVAL_TUNING.md).
The Rust knobs have no counterpart in the engine:

- `SUPERSEDED_PENALTY` (0.35 to 0.85): the engine removes superseded rows in
  SQL (`superseded_by IS NULL`). The penalty has nothing to act on.
- `recency_weight` (0.02 to 0.00): the engine uses a time decay
  (`RECENCY_HALFLIFE_HOURS`), not the Rust weighted sum.
- `COVERAGE_CEILING` (1.4 to 1.6): the engine has no coverage multiplier.
- Lexical term in fusion and the intent boost: the engine already scores
  lexical overlap, and the intent boost was the lowest-value knob.

The part that does carry over is the principle: query-term coverage should lower
a score, not silently remove a result. The engine expressed that principle as a
hard step, and the step caused the empty results above. The measurement chose
the replacement value.

## Reproduction

```bash
# Candidate: pristine 3.15.1 plus the current audited patches.
cp -r <wheel>/mnemosyne /tmp/candidate/mnemosyne
python scripts/apply_engine_patches.py --engine-root /tmp/candidate/mnemosyne
PYTHONPATH=/tmp/candidate python bench/retrieval/run_eval.py --all

# Baseline: the same procedure with the previous audited patch state.
git worktree add /tmp/mn-baseline HEAD^
cp -r <wheel>/mnemosyne /tmp/baseline/mnemosyne
python /tmp/mn-baseline/scripts/apply_engine_patches.py --engine-root /tmp/baseline/mnemosyne
PYTHONPATH=/tmp/baseline python bench/retrieval/run_eval.py --all
```

MRR is deterministic for a fixed engine build and fixture set. Recall counters
cannot leak between queries because each query runs on a fresh copy of the
seeded database. Latency fields in the JSON records come from the recording
machine and are not part of the ranking claim.

## Embedding A/B on the retrieval corpus (Kaggle, 2026-10-26)

`run_eval.py` now honours `MNEMOSYNE_EVAL_EMBEDDINGS=on` to exercise the dense
path (default remains off, preserving the lexical baseline and existing tests).
A GPU run on Kaggle encoded the 177-row corpus with `BAAI/bge-small-en-v1.5`
and `BAAI/bge-base-en-v1.5` and replayed all three splits with embeddings on.

| Model | dev MRR | heldout_a MRR | heldout_b MRR |
| --- | --- | --- | --- |
| bge-small-en-v1.5 (embeddings on) | 0.6605 | 0.7654 | 0.7315 |
| bge-base-en-v1.5 (embeddings on) | 0.6605 | 0.7654 | 0.7315 |
| lexical baseline (embeddings off) | 0.6605 | 0.7654 | 0.7315 |

No measurable MRR difference on this synthetic corpus: the lexical admission
floor dominates and dense recall adds nothing, consistent with the
`MNEMOSYNE_EMBEDDINGS_OFF` guidance. This corpus cannot discriminate
embedding models; the reindex decision stays on the BEIR scifact bake-off
(bge-base winner) pending a staging A/B on a real memory corpus. Raw artifact:
`bench/retrieval/retrieval_embeddings_ab_results.json`.
