# Task: write throughput of `PythonMemoryStorage`

`src/mnemosyne_lite/storage.py` implements the memory store. Objective: **reduce the
cost of bulk writes** (`remember()`) without weakening recall, durability, or the
WAL bound.

## What the scorer does

`task/score.py` copies the repo's own harness (`bench/measure.sh`) pristine into a
scratch dir, points it at your workspace, and reports **`write_5000_p50_ms`** —
median time to insert 5000 rows into a fresh database. **Lower is better.** You do
not need to run it; the runtime scores your workspace after every attempt.

## Baseline

The unmodified seed measures **≈6700 ms** for 5000 rows (measured here 6371 / 6717 /
6782 ms — treat ~6700 ms as the number to beat). Profiling the seed shows:

| component | cost | share |
|---|---|---|
| `PRAGMA wal_checkpoint(TRUNCATE)` in `_maybe_checkpoint` | ~5500 ms | **~82%** |
| per-row `conn.commit()` (durability contract) | ~1250 ms | ~18% |
| FTS5 index maintenance + Python | ~15 ms | <1% |

**The insert path itself is not the problem — it is already within ~1% of raw
sqlite.** The bottleneck is the checkpoint policy. `wal_autocheckpoint` is off, so
`_maybe_checkpoint` checkpoints manually when the WAL crosses
`WAL_CHECKPOINT_BYTES` (4 MiB), checked every `WAL_CHECKPOINT_INTERVAL` (10)
writes. Checkpoint cost scales with WAL size, so total checkpoint cost grows with
ingest volume, and it is charged to whichever `remember()` call happens to land
on the interval.

A known-good change already measured in this repo: raising the two knobs to
16 MiB / every 100 writes scores **4297 ms (1.56×) with a 20.3 MiB peak WAL** and
passes every gate. Treat that as the floor to beat, not the ceiling.

## Correctness is non-negotiable — three gates

A candidate gets `fail_class != "ok"` and **no score** if any gate fails:

1. **`assert_failed`** — the harness's own recall assertions: 7 hits for
   `xylophone`, 2 for the phrase, 0 for `zzzqqqnomatch`, and `memory` saturating
   the 100-result cap.
2. **`contract_broken`** — `journal_mode=wal` and `synchronous=NORMAL` on the
   connection the store actually opens.
3. **`wal_unbounded`** — peak WAL across the run exceeds 64 MiB.

Gates 2 and 3 exist because write throughput is trivially cheatable, and the
cheats were measured before this task was configured:

| cheat | score | verdict |
|---|---|---|
| `synchronous=OFF` | 1297 ms | rejected — writes no longer survive a power loss |
| remove checkpointing | 1370 ms | rejected — 146.6 MiB peak WAL |

Both are ~5x "wins" that are regressions. Do not attempt them; the scorer will
throw the candidate away.

## In scope / off limits

- **In scope:** `src/mnemosyne_lite/storage.py`.
- **Off limits:** `bench/measure.sh` and `task/score.py` (the instrument),
  `integrations/`, the DB path, namespace semantics, tool names, and
  `mnemosyne_lite/{cli,tools,mcp}.py` unless your change genuinely requires it.
  Checkpoint *policy* is in scope; checkpoint *removal* is not.

## Generalization and honest trade-offs

The 5000-row workload is a sample of a real bulk ingest, not a special case to
target. Do not special-case its shape, and do not defer work past the measurement
window to make the number look better.

**The trade-off the scorer cannot see:** checkpoint less often and writes get
faster, but a larger WAL sits on disk longer and a concurrent reader can stall
behind it. If your change trades WAL size, reader latency, or crash-window
semantics for throughput, say so explicitly in `proposal.md` — that is the part
a reviewer needs and the benchmark cannot report.