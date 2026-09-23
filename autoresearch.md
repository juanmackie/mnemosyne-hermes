# Autoresearch: memory search speed (p50)

## Objective
Minimize `PythonMemoryStorage.recall()` latency (p50) on the deterministic
3000-row corpus built by `.auto/measure.sh`, without changing recall
semantics. The workload covers six query shapes: common+namespace, rare
term, multi-word phrase, no-match miss, common+importance floor, and wide
unfiltered (limit=50).

## Metrics
- **Primary**: search_p50_ms (ms, lower is better) — median across all
  timed samples of all six query shapes (60 reps each after warmup).
- **Secondary**: search_p99_ms; q0_p50_ms … q5_p99_ms (per-shape median
  and tail); assert_ok (must stay 1). Once tracked, included in every
  result line.

## How to Run
`./autoresearch.sh` — fast `py_compile` pre-check (<1s), then
`.auto/measure.sh`; prints `METRIC name=number` lines. Under plain
PowerShell invoke it as `bash ./autoresearch.sh`.

Sampling: each run is the **median of AR_RUNS (default 3) invocations**
(single-run noise measured ±8% — never trust N=1 at this scale). When
`AR_BASELINE=<current best primary>` is set and the median lands within
8% of it, two extra samples are taken (median-of-5) so noise cannot flip
the keep/discard decision. Experiments pass `AR_BASELINE` at invocation;
re-baselines do not.

## Files in Scope
- `src/lib/storage.py` — the whole recall path: the `instr(content_lower)`
  SQL, covering indexes (`idx_memories_recall`, `idx_memories_rank`),
  `_search_candidates` (Python snapshot + per-query candidate cache),
  `_recall_cache` result memo, buffered access-count flush
  (`_flush_accesses`), connection/PRAGMA setup and index init.

## Off Limits
- `.auto/` harness (`measure.sh`, `checks.sh`, eval sets, `log.jsonl`) —
  the instrument, not the target. Never edit it to improve numbers.
- `migrations/` schema semantics; DB-path / namespace / tool-name contracts.
- `integrations/hermes-provider/` and the MCP surfaces.
- Quality eval sets (`.auto/eval_*.jsonl`, membench): this loop makes no
  ranking-quality claims (policy in `.auto/DEV_EVAL.md`).

## Constraints
- `.auto/checks.sh` must pass (provider unit tests + `pytest tests -m
  'not integration'`).
- No new dependencies.
- The correctness assertions inside `measure.sh` must hold (rare-term
  count=7, phrase count=2, no-match=0, common saturates the 100 cap).
- A keep must not change WHICH rows recall returns for any query shape —
  only how fast they come back. If results differ, it is a discard even
  when faster.
- Branch `autoresearch/search-speed-2026-09-23` only; never commit to main.
- Status protocol: benchmark non-zero exit / failed `assert_ok` → `crash`;
  benchmark valid but `checks.sh` red or results changed → `discard`
  (description records why); primary improved vs best kept → `keep`.

## What's Been Tried
(Historical numbers come from `.auto/log.jsonl` segment 7 on this machine —
re-baseline here rather than trusting them.)

Keeps (segment 7, search_p50_ms):
- baseline 0.716 → covering rank index for unfiltered recall 0.5918
  (`idx_memories_rank`: ORDER BY streams from the index, LIMIT early exit).
- instr() over ASCII-lowered `content_lower` instead of LIKE 0.5387
  (LIKE's case-folding matcher cost ~1.3–1.6x a plain substring scan on
  the scan-bound shapes).

Landed after the log (present in tree; folded into this session's baseline):
- Python-side snapshot + per-query candidate cache (`_search_candidates`,
  cap 128 candidates → SQL `id IN (...)` narrowing; >128 falls back to the
  full ordered scan).
- Result memo `_recall_cache` validated by `_search_version`
  (`data_version` + `total_changes − ignored_changes`).
- Deferred access-count flush (historically p50 0.310 → 0.119ms) with
  `ignored_changes` so flushes never invalidate the content snapshot;
  `_recall_cache` cleared on flush because memoized rows carry stale
  `access_count`.
- Migration-time `content_lower` backfill + partial NULL index (O(1)
  "needs backfill?" probe on open).
- Connection cached per thread; WAL pragmas applied once per connection;
  `wal_autocheckpoint=0` with checkpoint only on write paths.

Dead ends / do not repeat:
- NULL-safe OR-fallback predicate in recall (mixed-version writer defense)
  — forces a table fetch, 1.17–1.25x slower on scan-bound shapes. The
  migration-time backfill replaced it.
- `idx_memories_importance` for unfiltered recall — lured the planner into
  a random-order walk for bound LIKE, ~4x slower than seq scan + temp sort.
  Index is dropped on open; do not reintroduce.
- LIKE + ESCAPE for `%`/`_` — replaced by literal `instr()`; don't go back.
- Per-row `content_lower IS NULL` guard in the recall predicate — measured
  1.17–1.25x slower; the backfill guarantees non-NULL.

Open ideas: `autoresearch.ideas.md` (seeded from `.auto/ideas.md`).
