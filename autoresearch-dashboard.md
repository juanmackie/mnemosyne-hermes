# Autoresearch Dashboard: memory search speed (p50)

**Runs:** 3 | **Kept:** 3 | **Discarded:** 0 | **Crashed:** 0
**Baseline:** search_p50_ms: 0.0114ms (#1)
**Best:** search_p50_ms: 0.0086ms (#6, (-24.6%))


>  **DATA INCONSISTENCY DETECTED** — worklog documents 5 experiments, JSONL segment 1 contains 3 runs (diff 2). Check backups before continuing.

| # | commit | search_p50_ms | status | description |
|---|--------|---------------|--------|-------------|
| 4 | 8c8da91 | 0.0114ms (+0.0%) | keep | segment-1 baseline: median-of-3 runner (AR_RUNS=3), unchanged code |
| 5 | 4bf3204 | 0.0108ms (-5.3%) | keep | batch pending access counts via Counter.update(ids) (C-speed replaces per-row dict loop) — -5.3% p50, -34% p99 vs segment-1 baseline |
| 6 | 3535fcc | 0.0086ms (-24.6%) | keep | r.copy() instead of dict(r) at both recall return sites — -24.6% p50, -45% p99 vs segment-1 baseline |

