# Autoresearch Dashboard: memory search speed (p50)

**Runs:** 5 | **Kept:** 4 | **Discarded:** 1 | **Crashed:** 0
**Baseline:** search_p50_ms: 0.0114ms (#4)
**Best:** search_p50_ms: 0.0081ms (#7, -28.9%)

| # | commit | search_p50_ms | status | description |
|---|--------|---------------|--------|-------------|
| 4 | 8c8da91 | 0.0114ms (+0.0%) | keep | segment-1 baseline: median-of-3 runner (AR_RUNS=3), unchanged code |
| 5 | 4bf3204 | 0.0108ms (-5.3%) | keep | batch pending access counts via Counter.update(ids) (C-speed replaces per-row dict loop) — -5.3% p50, -34% p99 vs segment-1 baseline |
| 6 | 3535fcc | 0.0086ms (-24.6%) | keep | r.copy() instead of dict(r) at both recall return sites — -24.6% p50, -45% p99 vs segment-1 baseline |
| 7 | af2d969 | 0.0081ms (-28.9%) | keep | pending = list of id-tuples (append per recall), per-id expansion only at flush; ACCESS_FLUSH_DISTINCT now bounds batches — -5.8% p50 (N=5 tiebreak), q5 -44%; checks.sh green |
| 8 | 63a8366 | 0.0096ms (-15.8%) | discard | memo-hit micro-trims (isspace clamp, try/except _conn/_pending, eager ignored_changes/pending_hits) — 0.0096 vs 0.0081 best; q5 swung 2.4x suggesting machine-state noise; reverted, to be retried in a future session |

