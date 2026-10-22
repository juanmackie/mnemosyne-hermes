# Rust-era retrieval tuning (PR #24 and PR #25)

Status: preserved as a record. Not merged, not shipped.

Main is Python-only since `816ffa0` ("Python-only runtime: restore Python source,
remove Rust references"). The files these changes touch no longer exist:

- `src/config.rs`
- `src/storage/libsql.rs`
- `src/utils/retrieval.rs`

A merge would restore the retired Rust tree, which `AGENTS.md` forbids.

## Where the work is preserved

- Git tag: `archive/rust-retrieval-tuning` -> `f1898a8`
- Git tag: `archive/rust-retrieval-tuning-pr24` -> `2002689`
- PR #25 head: `f1898a8` (branch `feat/retrieval-recency-zero`)
- PR #24 head: `2002689` (branch `feat/retrieval-superseded-penalty`)
- PR #25 contains the changes from PR #24.

The Rust tree itself is preserved at `archive/rust-era` and the reference in
[RUST_ARCHIVE_REF.md](RUST_ARCHIVE_REF.md).

## The changes

| Knob | File | Before | After |
| --- | --- | --- | --- |
| `recency_weight` | `src/config.rs` | `0.02` | `0.00` |
| `SUPERSEDED_PENALTY` | `src/utils/retrieval.rs` | `0.35` | `0.85` |
| `COVERAGE_CEILING` | `src/utils/retrieval.rs` | `1.4` | `1.6` |
| Lexical signal in fusion | `src/storage/libsql.rs` | rank-only RRF | `+ 0.10 * lexical_score` |
| Intent/slot boost | `src/utils/retrieval.rs` | none | `1.05x` per matched intent word |
| Superseded test | `src/utils/retrieval.rs` | `score < 0.9 * 0.5` | `score < 0.9` |

PR #25 also makes `query_content_terms` public.

## Why this is not ported

The pinned engine (`mnemosyne-memory` 3.15.1) has no `SUPERSEDED_PENALTY` or
`COVERAGE_CEILING`. It excludes superseded rows in SQL (`superseded_by IS NULL`)
and applies a time decay (`RECENCY_HALFLIFE_HOURS`). A port is a scoring
redesign, not a constant change, and needs its own measurement.

## Verification status

The scorer and its fixtures are recoverable from git history, not lost:

- `b548943` (`.auto/`): `evaluate.py`, `setup_data.py`, `corpus.jsonl` (36 KB),
  and three labelled sets, `eval_dev.jsonl`, `eval_heldout_a.jsonl` and
  `eval_heldout_b.jsonl`. Each set has 27 queries; the dev split is
  `correction` 6, `already_told` 5, `structured_current` 5, `entity_exact` 5,
  `multi_constraint` 6. These names match the categories in the PR bodies.
- `032c848` removed `.auto/` in the Python-only cleanup.

The assets are not runnable as committed: `setup_data.py` and `evaluate.py`
shell out to the retired Rust binary (`target/release/mnemosyne`). The data and
the metric can be ported to a live surface; the invocation layer cannot.

`eval/score.json` is unrelated. It comes from `task/score.py`, a write-throughput
scorer (`write_5000_p50_ms`), not from retrieval.

The MRR numbers in the PR bodies stay unverified until a runner is ported and
run, but the fixtures that produced them are not lost.

## Flags for any future port

- The intent-word list (`email`, `shift`, `volunteer`, `booking`, ...) is
  task-specific and can overfit to one evaluation set.
- The boost comment says "1.15x per matched intent word (was 1.05x)", but the
  code applies `1.0 + 0.05 * intent_hits`, which is 1.05x per hit. The comment
  is wrong.
- The loosened assertion (`score < 0.9`) no longer checks a meaningful bound;
  the original `0.9 * 0.5` pinned the 0.35 penalty.
