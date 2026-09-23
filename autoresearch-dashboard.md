# Autoresearch Dashboard: memory search speed (p50)

**Runs:** 4 | **Kept:** 4 | **Discarded:** 0 | **Crashed:** 0
**Baseline:** search_p50_ms: 0.0114ms (#1)
**Best:** search_p50_ms: 0.0081ms (#7, (-28.9%))


>  **DATA INCONSISTENCY DETECTED** — worklog documents 7 experiments, JSONL segment 1 contains 4 runs (diff 3). Check backups before continuing.

| # | commit | search_p50_ms | status | description |
|---|--------|---------------|--------|-------------|
| 4 | 8c8da91 | 0.0114ms (+0.0%) | keep | segment-1 baseline: median-of-3 runner (AR_RUNS=3), unchanged code |
| 5 | 4bf3204 | 0.0108ms (-5.3%) | keep | batch pending access counts via Counter.update(ids) (C-speed replaces per-row dict loop) — -5.3% p50, -34% p99 vs segment-1 baseline |
| 6 | 3535fcc | 0.0086ms (-24.6%) | keep | r.copy() instead of dict(r) at both recall return sites — -24.6% p50, -45% p99 vs segment-1 baseline |
| 7 | af2d969 | 0.0081ms (-28.9%) | keep | pending = list of id-tuples (append per recall), per-id expansion only at flush; ACCESS_FLUSH_DISTINCT now bounds batches — -5.8% p50 (N=5 tiebreak), q5 -44%; checks.sh green |

