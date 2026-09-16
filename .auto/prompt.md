# Autoresearch: memory search speed

## Objective
Reduce `recall()` (memory search) latency in `src/lib/storage.py`.
Workload: ~3000-memory deterministic corpus, mixed query selectivity
(common term, rare term, exact phrase, no-match, namespace-filtered,
importance-filtered). Primary metric is search p99 (ms), lower is better.

## Metrics
- **Primary**: `search_p99_ms` (ms, lower is better) — p99 over all timed recall calls
- **Secondary**: `search_p50_ms`, per-query p50/p99, `assert_ok` (1 = corpus assertions held)

## How to Run
`./.auto/measure.sh` — outputs `METRIC name=value` lines. Exits nonzero if
recall correctness assertions fail (planted-term counts changed).

## Files in Scope
- `src/lib/storage.py` — PythonMemoryStorage.recall (the search path);
  read-only: remember/list/consolidate unless a change provably helps recall.
- `src/lib/mnemosyne_client.py` — thin async wrapper (only if it adds overhead).

## Off Limits
- No new dependencies (stdlib only)
- No Rust source changes
- No `migrations/` changes; in-code schema additions must auto-apply to
  existing DBs and keep `remember`/`list` behavior identical
- Do not change recall result semantics: same rows, same order, same dicts
  (measure.sh asserts planted counts; checks.sh runs the test suites)

## Constraints
- `.auto/checks.sh` must pass before any keep
- No new external packages; pure Python / stdlib only

## What's Been Tried (prior latency session, segment 0)
- **synchronous=FULL → NORMAL** (c53ed41): remember p99 2.1× down. Search unchanged (~0.07ms on 100 rows).
- **mmap_size=256MB**: ~5% faster reads.
- **WAL checkpoint every 10th write** (c1c590a): remember p99 12% better.
- Old workload (100 near-identical rows) made search noise-dominated; this
  session uses a 3000-row deterministic corpus for headroom.
- **Drop idx_memories_importance** (a811d5b, run 2): bound LIKE hides the
  leading wildcard from the planner, which walked the importance index in
  random row order + temp b-tree sort. Sequential scan + sort is ~2x faster
  (A/B: p50 1.7→0.75ms, p99 3.7→1.4-1.7ms). Self-applies to existing DBs via
  DROPPED_INDEXES. MCP hot path (namespaced, limit 10) unaffected at ~0.05ms.
- **Discarded**: FTS5 token-prefilter (unsound — misses infix LIKE matches,
  e.g. query "emory" vs content "Memory"); FTS5-trigram LIKE (measured
  slower, no acceleration materializes); two-step narrow-sort fetch (p99
  -10% but p50 worse + complexity); temp_store=MEMORY + 64MB cache
  (neutral-to-worse over 3 runs).

## Ideas Backlog
- FTS5 virtual table for recall (replace LIKE '%query%' with MATCH) —
  needs care: LIKE is substring/phrase semantics, FTS5 is token semantics
- Covering index already exists (idx_memories_recall); verify it is used
  (EXPLAIN QUERY PLAN) before adding more indexes
- SELECT only needed columns instead of SELECT *
- PRAGMA cache_size / temp_store=MEMORY tuning for read-heavy workload
- `instr(content,?)>0` instead of LIKE to dodge ESCAPE/casefold overhead
- Cache compiled query plan via persistent connection (measure per-call conn cost)
