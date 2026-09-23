# Autoresearch Dashboard: memory search speed (p50)

**Runs:** 2 | **Kept:** 1 | **Discarded:** 1 | **Crashed:** 0
**Baseline:** search_p50_ms: 0.0102ms (#1)
**Best:** search_p50_ms: 0.0102ms (#1, (+0.0%))

| # | commit | search_p50_ms | status | description |
|---|--------|---------------|--------|-------------|
| 1 | 986e062 | 0.0102ms (+0.0%) | keep | baseline: recall result memo + candidate cache already in tree; asserts pass |
| 2 | 45fadba | 0.0119ms (+16.7%) | discard | Counter.update(ids) for pending access counts (batched, C-speed) — slower than baseline (0.0119 vs 0.0102); p99 also doubled, suspect machine noise; reverted |

