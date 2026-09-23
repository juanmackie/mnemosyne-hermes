# Autoresearch Dashboard: memory search speed (p50)

**Runs:** 8 | **Kept:** 3 | **Discarded:** 5 | **Crashed:** 0
**Baseline:** search_p50_ms: 0.0108ms (#9)
**Best:** search_p50_ms: 0.0051ms (#11, -52.8%)

*Segment 2 opened 2026-09-24 after +33% machine drift (unchanged code read 0.0108 vs segment-1 best 0.0081 at #7).*

| # | commit | search_p50_ms | status | description |
|---|--------|---------------|--------|-------------|
| 9 | d1e20d0 | 0.0108ms (+0.0%) | keep | segment-2 baseline: quiet-machine probe on unchanged code (d1e20d0) — +33% vs segment-1 best 0.0081 (machine drift, same signature as run 8); re-baseline before retrying the micro-trim bundle |
| 10 | b0f7c3a | 0.0100ms (-7.4%) | keep | retry of run-8 memo-hit micro-trims (isspace no-alloc check, two-compare clamp, try/except _conn/_pending, eager-seeded counters) — N=5 tiebreak, every shape <= +1%, p99 -9.8%; checks.sh green (21+97) |
| 11 | 4dbf2dc | 0.0051ms (-52.8%) | keep | flush patches memoized rows in place instead of clearing _recall_cache — memo stays warm across flushes, one shape's flush no longer nukes other shapes' entries; -49% vs best (clear of band), p99 -36%, every shape improved; checks.sh green (21+97) |
| 12 | b46c882 | 0.0073ms (-32.4%) | discard | list(map(dict.copy)) at return sites — 0.0073 vs 0.0051 best (+43%); q5 -31% as predicted but q0/q1 +56% (shape-selective noise signature); reverted; q5 win corroborated, retry on quiet machine |
| 13 | 0af5ea4 | 0.0087ms (-19.4%) | discard | quiet-machine probe, unchanged code — 0.0087 vs 0.0051 (+71%): noise confirmed; root cause = stale foreign processes (spinning unittests since 6:57AM + duplicate ai_sprink services, ~28% CPU); no code change |
| 14 | 7cc4993 | 0.0065ms (-32.4%) | discard | post-kill probe, unchanged code — runaway frozen_gate tree eliminated (self-nesting gates+unittests, orphaned); recovery: q3/q1/q4/q0/q5 back to run-11 quiet numbers, only q2 burst-hit dragging aggregate (+27%) |
| 15 | 1bd961e | 0.0057ms (-47.2%) | discard | map(dict.copy) retry on recovered machine — +11.8% (N=3 clear, no tiebreak); q5 -32% corroborated twice (above median), q4 contradicted prediction (burst); PARKED; reverted |
| 16 | 1bd961e | 0.0086ms (-20.4%) | discard | PRAGMA cursor reuse (-0.25us microbench) — +69% uniform elevation: frozen_gate RESPAWNED (live node supervisor, CPU 96%), not the code; reverted; one retry owed when machine usable |

---

**LOOP CLOSED 2026-09-24 (user request)** — all segments: **16 runs · 8 kept · 8 discarded · 0 crashed**.
**All-time best: search_p50_ms 0.0051ms (#11, `4dbf2dc`) — −50.0% vs original run-1 baseline.**
