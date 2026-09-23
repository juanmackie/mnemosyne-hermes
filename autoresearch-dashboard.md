# Autoresearch Dashboard: memory search speed (p50)

**Runs:** 2 | **Kept:** 2 | **Discarded:** 0 | **Crashed:** 0
**Baseline:** search_p50_ms: 0.0108ms (#9)
**Best:** search_p50_ms: 0.0100ms (#10, -7.4%)

*Segment 2 opened 2026-09-24 after +33% machine drift (unchanged code read 0.0108 vs segment-1 best 0.0081 at #7).*

| # | commit | search_p50_ms | status | description |
|---|--------|---------------|--------|-------------|
| 9 | d1e20d0 | 0.0108ms (+0.0%) | keep | segment-2 baseline: quiet-machine probe on unchanged code (d1e20d0) — +33% vs segment-1 best 0.0081 (machine drift, same signature as run 8); re-baseline before retrying the micro-trim bundle |
| 10 | b0f7c3a | 0.0100ms (-7.4%) | keep | retry of run-8 memo-hit micro-trims (isspace no-alloc check, two-compare clamp, try/except _conn/_pending, eager-seeded counters) — N=5 tiebreak, every shape <= +1%, p99 -9.8%; checks.sh green (21+97) |
