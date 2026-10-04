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

The unmodified seed measures **4870 ms** for 5000 rows on this machine (peak WAL
20.3 MiB, `search_p50_ms` 0.0075). **That is the number to beat.**

History: an earlier seed with a 4 MiB / every-10-writes checkpoint policy measured
≈6700 ms, and raising the knobs to 16 MiB / every 100 writes was the known-good
change that reached 4870 ms. **That change is already in the seed** — do not re-derive
it, and do not spend a branch rediscovering it. Re-tuning those two constants will
not move the score much further; look for a different mechanism.

Profiling the original 4 MiB seed showed:

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

The remaining cost is therefore checkpoint work on a ~16 MiB WAL plus the
per-row `conn.commit()` durability contract. The insert path itself is already
within ~1% of raw sqlite, so there is no win hiding in the INSERT itself.

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

## Time budget — read this first

You have **30 minutes of wall clock, and the attempt is killed at the limit.** An
attempt that runs out is recorded as a failure and its work is thrown away, even
if the code was correct. Budget accordingly:

* **The runtime scores your workspace after you finish.** You never need to run
  the metric yourself. Do not re-implement the harness — that is what burned
  earlier attempts.
* If you do want local evidence, **cap yourself at ~4 timed runs total** and keep
  each under 3 minutes. Prefer static reasoning (frame counts, page geometry,
  pragma semantics) plus **one** confirmation run.
* **Write a stub `proposal.md` in your first few minutes**, then refine it. A
  finished proposal with thin evidence beats a perfect one that never lands.
* Leave the workspace in its final intended state with ~5 minutes to spare.

A correct change that lands is worth more than a better change that times out.

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