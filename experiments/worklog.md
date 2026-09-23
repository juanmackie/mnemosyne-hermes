# Autoresearch Worklog — memory search speed (p50)

- **Session**: 2026-09-23 · branch `autoresearch/search-speed-2026-09-23`
- **Primary metric**: search_p50_ms (ms, lower better)
- **Secondaries**: search_p99_ms, q0_p50_ms … q5_p99_ms, assert_ok
- **Command**: `./autoresearch.sh` (wraps `.auto/measure.sh`)
- **State**: `../autoresearch.jsonl` (config header = segment 0) ·
  dashboard: `../autoresearch-dashboard.md` · ideas: `../autoresearch.ideas.md`
- **Data summary**: fresh segment 0. Prior art lives in `.auto/log.jsonl`
  (segments 0–7, last keep run 112 @ search_p50_ms=0.5387) — its numbers
  are historical context only; run 1 re-baselines here.

## Baseline
- **Run 1** (commit `986e062`): search_p50_ms=**0.0102ms** ·
  search_p99_ms=0.3406ms · assert_ok=1 · per-shape p50:
  q0 0.0130 / q1 0.0059 / q2 0.0047 / q3 0.0061 / q4 0.0105 / q5 0.0132

### Run 1: baseline — search_p50_ms=0.0102 (keep)
- Timestamp: 2026-09-23 (session start)
- What changed: no code change; setup commit `986e062` measured as-is.
- Result: p50 0.0102ms, p99 0.3406ms; assert_ok=1; all six shapes ≤0.0132ms p50.
- Insight: p50 is already memo-dominated (~5–13µs) — the `_recall_cache`
  result memo (d0b00bc) absorbs most of the 60 timed reps; p99 0.34ms is
  the periodic flush→clear-memo→re-query tail. q5 (50-row results) hits
  the 256-distinct-id flush cap every ~5 calls so its p50 sits highest;
  q2 (2-row results) never flushes in-run → cheapest pure-memo shape.
- Next: shave the memo-hit path itself (batched pending updates, cheaper
  per-row fresh copies) — run 2.

### Run 2: Counter.update(ids) for pending access counts — search_p50_ms=0.0119 (discard)
- Timestamp: 2026-09-23 18:05
- What changed: `_pending` now a `Counter`; memo + fresh paths batch
  `pending.update(ids)` (C-speed) instead of a per-row Python get/set loop.
- Result: p50 0.0119ms (+16.7% vs baseline 0.0102), p99 0.7114 (2.1x
  worse); every shape's p50 rose. assert_ok=1.
- Insight: Counter.update is strictly less work than the Python loop, so
  the regression is almost certainly machine noise (p99 doubling across
  ALL shapes supports this) — but protocol is king: worse → discard.
  Noise floor at this scale is now a first-order concern.
- Next: revert + immediate baseline re-run (run 3) to measure the noise
  floor before trusting sub-20% deltas.

### Run 3: baseline noise probe (unchanged code) — search_p50_ms=0.0110 (discard)
- Timestamp: 2026-09-23 18:08
- What changed: nothing (tree identical to run 1's `986e062` sources).
- Result: p50 0.0110ms vs 0.0102 best (+7.8%), p99 0.6782 vs 0.3406.
  Per-shape: q2/q3 got faster, q1/q5 slower — jitter, not structure.
- Insight: single-run noise floor ≈ ±8% on p50 (and far worse on p99).
  Run 2's Counter discard was therefore inconclusive — the loop cannot
  resolve deltas below the noise floor with N=1 samples.
- Next: methodology fix (harness change → new segment): `autoresearch.sh`
  now runs `measure.sh` AR_RUNS=3× and reports per-metric medians.
  Re-baseline as segment 1 run 4; discard runs 2-3 stay segment 0 history.
  Retry the Counter idea once under the stabilized harness.

### Run 4: segment-1 baseline (median-of-3) — search_p50_ms=0.0114 (keep)
- Timestamp: 2026-09-23 18:14
- What changed: nothing in src; harness now reports per-metric medians of
  3 `measure.sh` invocations (commit `8c8da91` starts segment 1).
- Result: p50 0.0114ms, p99 0.8487ms; assert_ok=1. q5 remains the worst
  shape (p50 0.0239, p99 0.925 — flushes every ~5 calls at 50-row results).
- Insight: the machine sits ~12% slower than run 1's fresh state; the
  segment restart keeps comparisons honest. Segment-1 reference = 0.0114.
- Next: retry the Counter pending batching under N=3 aggregation.

### Run 5: Counter.update(ids) pending batching — search_p50_ms=0.0108 (keep)
- Timestamp: 2026-09-23 18:19
- What changed: `_pending` is a `Counter`; memo + fresh recall paths batch
  `pending.update(ids)` instead of a per-row Python get/set loop; flush
  resets to a fresh Counter. (Commit `4bf3204`.)
- Result: p50 0.0108ms (-5.3% vs 0.0114), p99 0.5592 (-34%). Shapes:
  q0 -12%, q1 -13%, q2 -3%, q3 +4%, q4 -5%, q5 -20% (0.0239→0.0191).
- Insight: the same change that "lost" run 2 wins under aggregation —
  run 2 was noise. Biggest beneficiary is q5 (50-row results): its
  frequent flushes made it the most pending-update-heavy shape.
- Next: shave the remaining memo-hit overhead — `getattr(..., "pending_hits",
  0)` + attribute round-trips per call; or attack q5's flush-amplified
  re-queries (p99 still 0.74ms).

## Key Insights
- The loop optimizes a two-regime workload: memo hits (p50) vs
  flush-invalidated re-queries (p99). Both are fair game for p50, since
  shapes that flush mid-run (q0, q4, q5) drag the overall median.

## Next Ideas
See `../autoresearch.ideas.md`.
