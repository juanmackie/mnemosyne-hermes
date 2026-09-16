# Autoresearch: memory search speed

## Objective
Reduce `recall()` (memory search) latency in `src/lib/storage.py`.
Workload: ~3000-memory deterministic corpus, mixed query selectivity
(common term, rare term, exact phrase, no-match, namespace-filtered,
importance-filtered). Primary metric is search p99 (ms), lower is better.

## Metrics
- **Primary**: `search_p50_ms` (ms, lower is better) — median over all timed
  recall calls. (Was p99; p99 proved stall-dominated on this Windows box —
  identical code swings p99 1.2→3.2ms run to run — while p50 repeats within
  ~±12%. p99 stays as a secondary tail monitor.)
- **Secondary**: `search_p99_ms`, per-query p50/p99/best, `assert_ok` (1 = corpus
  assertions held)

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

## Status: CLOSED (segment 1)
Kept: covering rank index (F4E1) + ASCII-folded `instr` search. Primary
`search_p50_ms` 0.716 → 0.539 (-25%); baseline before segment 1 was p99-based.
Remaining backlog is in `.auto/ideas.md`, not below.

## What's Been Tried (this session, segment 1)
- **Toolchain repair**: `run_experiment` spawns plain `bash` which was absent
  from the tool PATH. Fixed with `~/.local/bin/bash.exe` +
  `msys-2.0.dll` (copies of Git-for-Windows usr/bin) + `bin/bash.exe` for
  shebang + `python3.exe` (3.11.15). Scripts rewritten to builtins-only +
  python (no rm/dirname/tail); `$0`-backslash normalization for absolute
  Windows `$0`. Invoke as `bash .auto/measure.sh` (direct exec hits msys
  shebang-mount issues). Protected user's uncommitted work: skip-worktree
  on docs/HERMES_INTEGRATION.md, install.sh, egg-info PKG-INFO;
  `dist/` in .git/info/exclude (tool does `git add -A` on keep,
  `checkout -- .` + `clean -fd` on discard). Branch: autoresearch/search-speed.
- **Persistent corpus DB** (`.auto/data/bench_corpus.db`, CORPUS_VERSION):
  rebuild-only-if-stale; kills insert-phase jitter between runs.
- **Pinned interpreter** `$HOME/.local/bin/python3.exe` (3.11.15, sqlite
  3.53.1) via MEASURE_PYTHON override, same under tool and interactive envs.
- **Probes (discarded)**: `instr(lower(content),lower(?))` ~2x SLOWER than
  LIKE (1.6-2.1 vs 0.7-1.0ms); dropping `ESCAPE` clause neutral; GC-disable
  during timing did not fix p99 stalls (stalls are OS-level, not GC).
- **Estimator change**: REPS 40->60, warmup 5->10; primary p99 -> p50
  (see Metrics). `instr`/`no-ESCAPE` rollbacks: no assumption change so far.
- **Covering rank index** (`idx_memories_rank`, d130670, run #111):
  `(importance DESC, created_at DESC, content, namespace)` lets UNFILTERED
  recall stream the ORDER BY with no temp b-tree sort and stop at LIMIT.
  Per-query p50: wide/limit-50 shape 0.99 → 0.12ms. A narrow rank-only index
  (no content) was tried first and discarded: the planner used it, but
  rare/phrase/no-match shapes paid ~1.5x in per-row table fetches.
  Order equivalence argued from zero `(importance, created_at)` tie groups and
  verified directly (identical ID sequence, old vs new plan, all 6 shapes).
- **Search `content_lower` with `instr()` instead of `content` with LIKE**
  (09e6619, run #112): LIKE's case-insensitive matcher costs ~1.3-1.8x a
  plain substring scan per row, and for rare/phrase/no-match shapes that
  per-row cost *is* the query. SQLite `lower()` and `LIKE` fold exactly the
  ASCII range, so an ASCII-lowered copy searched with `instr()` is
  semantics-preserving and needs no `%`/`_`/`\` escaping at all. Clean
  interleaved A/B on frozen corpora: 1.24x rare / 1.39x phrase / 1.65x
  no-match / neutral elsewhere, identical result ids on all 6 shapes.
  p50 0.592 → 0.539, p99 1.29 → 1.22. Auto-applies to existing DBs
  (ALTER + backfill on open). Index DDL is now compared against
  `sqlite_master` and rebuilt only when its column list changed, because
  `IF NOT EXISTS` matches on name alone.
- **Discarded in segment 1**: NULL-safe OR-fallback predicate
  (`instr(...) > 0 OR (content_lower IS NULL AND content LIKE ...)`) to make
  recall robust against rows written by a mixed-version process — cost
  1.17-1.25x on the scan-bound shapes because it forces a table fetch.
  Replaced by a migration-time backfill whose "anything to fix?" probe is a
  partial index on `content_lower IS NULL`, so the check is O(1) when clean.
- **Deferred (recorded in `.auto/ideas.md`)**: per-call storage construction
  in the MCP provider hot path (~6.4ms/call) and a trigram posting-list
  index to kill the residual full-scan cost.

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

## Ideas Backlog (moved: see `.auto/ideas.md`)
- FTS5 virtual table for recall (replace LIKE '%query%' with MATCH) —
  needs care: LIKE is substring/phrase semantics, FTS5 is token semantics
- Covering index already exists (idx_memories_recall); verify it is used
  (EXPLAIN QUERY PLAN) before adding more indexes
- SELECT only needed columns instead of SELECT *
- PRAGMA cache_size / temp_store=MEMORY tuning for read-heavy workload
- `instr(content,?)>0` instead of LIKE to dodge ESCAPE/casefold overhead
- Cache compiled query plan via persistent connection (measure per-call conn cost)
