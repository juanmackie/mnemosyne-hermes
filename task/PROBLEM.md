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

The unmodified seed measures **≈700 ms** for 5000 rows on this machine: 595, 611,
627, 708 and 733 ms over five scorer runs, with a peak WAL of 17.0–17.1 MiB and
`search_p50_ms` 0.0055. **That is the number to beat.**

**Absolute numbers drift with machine state.** A control batch measured later the
same day returned 710–930 ms for the same unchanged seed. Treat the baseline as
≈700 ms with a **±25% noise floor**. Do not believe a win under ~15% unless a
mechanism explains it, and re-measure before you trust any comparison.

History: the objective has been worked already, and both mechanism wins are in
the seed. Do not spend a branch rediscovering them:

| change | effect on 5000-row ingest |
|---|---|
| WAL checkpoint bound 4 → 16 MiB, interval 10 → 100 writes | 6717 → ~4900 ms |
| 1 KiB page geometry + one redundant index retired | → ~700 ms |

Checkpoint policy is no longer the bottleneck. `wal_autocheckpoint` is off, and
`_maybe_checkpoint` still checkpoints manually when the WAL crosses
`WAL_CHECKPOINT_BYTES` (16 MiB), checked every `WAL_CHECKPOINT_INTERVAL` (100)
writes.

What is left, measured on the current seed:

| component | cost | share |
|---|---|---|
| per-row `conn.commit()` (durability contract) | ~506 ms | **~63%** |
| `execute()` — INSERT, FTS5 and index writes | ~204 ms | ~25% |
| `_maybe_checkpoint` | ~100 ms | ~12% |
| everything else (hashing, bookkeeping, connection lookup) | <5 ms | <1% |

**The insert path is not the problem — it is already within ~1% of raw sqlite.**
The remaining cost is the per-row commit, and the per-row commit is the
durability contract. Relaxing it is the rejected cheat below, not a mechanism
change.

**This objective is close to exhausted.** Any honest win left is small, so a new
cycle needs a reason to exist beyond "make writes faster".

## Correctness is non-negotiable — three gates

A candidate gets `fail_class != "ok"` and **no score** if any gate fails:

1. **`assert_failed`** — the harness's own recall assertions: 7 hits for
   `xylophone`, 2 for the phrase, 0 for `zzzqqqnomatch`, and `memory` saturating
   the 100-result cap.
2. **`contract_broken`** — `journal_mode=wal` and `synchronous=NORMAL` on the
   connection the store actually opens.
3. **`wal_unbounded`** — peak WAL across the run exceeds 28 MiB. The honest seed
   peaks at 17.1 MiB and disabling checkpointing reaches ~41 MiB. The limit was
   recalibrated after the 1 KiB page geometry shrank the unbounded WAL: the old
   64 MiB limit let that cheat through, and it scored 535 ms against a 744 ms seed.

Gates 2 and 3 exist because write throughput is trivially cheatable, and the
cheats were measured before this task was configured:

| cheat | measured | verdict |
|---|---|---|
| `synchronous=OFF` | ~12% faster | rejected — writes no longer survive a power loss |
| disable checkpointing | 535–570 ms, 40.9 MiB peak WAL | rejected — the WAL grows without bound |

Both are regressions dressed as wins. Do not attempt them; the scorer throws the
candidate away. For reference, the honest direction still passes: a tighter
4 MiB checkpoint bound scores 698 ms at a 4.2 MiB peak WAL and is accepted.

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