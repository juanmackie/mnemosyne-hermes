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
Pending (run 1).

## Key Insights
(experiment by experiment)

## Next Ideas
See `../autoresearch.ideas.md`.
