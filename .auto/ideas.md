# Ideas backlog — latency session (retrieval latency)

Done and on main (do not redo): shared libsql connection handle; WAL +
synchronous=NORMAL (warm p95 417.9→20.4ms); column-info memoization;
retrieval diagnostics sampled off the recall path; PPR adjacency OR →
UNION ALL of indexed halves + weights/settings Lazy caches (61625e0,
quality steady at 0.9815 heldout MRR, #27).

## Found 2026-09-09 (consolidation work, run #29)

- **BUG (pre-existing, data loss): `memory_links.link_type` CHECK whitelists
  only extends/contradicts/implements/references/supersedes, but `LinkType`
  has 9 variants. `add_bidirectional_links` uses `INSERT OR IGNORE`, so
  builds_upon/referenced_by/clarifies links are silently discarded at store
  time (reproduced in consolidation_tests). Fix = table-rebuild migration
  widening the CHECK (or dropping it) + a backfill story for lost edges.
- **BUG (pre-existing, flaky-ish test): `mn_mgr_forget_best_effort_never_errors`
  fails on clean HEAD: migration 012 executed twice -> "duplicate column name:
  requirements". Multi-statement migration + re-run path needs a guard
  (duplicate-tables/022_work_items.sql exists for the same reason).
- Consolidation delivered (7122ff7): `mnemosyne consolidate [--apply] [--json]`,
  migrations 030 (consolidation_runs/tombstones), single-transaction supersede,
  pre-apply backup + WAL checkpoint gate. CLI `remember` dedupes at write time,
  so real duplicates only arrive via crash/import/concurrent paths.

## Still open (latency)


- **Clean keep for the OR split**: re-run measure with
  `checks_timeout_seconds: 600` — checks include a `full,distributed`
  compile that exceeds the 300s default (that timeout, not a failure,
  blocked #27).
- **Warm-p95 delta for the OR split**: the session's primary latency
  metric (warm MCP p95 vs the 20.4ms baseline) has not been re-measured
  since the connection/WAL work; `measure.sh` profiles are cold-server.
  Add a warm-server block to `measure.sh` if the delta is wanted.
- **Prepare hot SQL once** (`Connection::prepare`): keyword_search, batch
  fetch, trace insert — only if a profile shows parse overhead matters.
- **PPR dense-array iteration** (`utils/ppr.rs`): only at 10k+ memories;
  re-baseline with `BENCH_MEMORIES=10000` first.
- **Ingest benchmark** for store/link path (write-side, 5k memories).
- Wire `.auto/unify_audit.py` output into `.auto/audit.json` automatically.

## Notes (measured, don't re-derive)

- `run_experiment` hard-caps at 600s unless `timeout_seconds` (correct
  param name) is raised; full 3-profile `measure.sh` needs ~960s.
- python-provider profile needs system `libpython3.11-dev` (installed).
- The 1.00 heldout-MRR "best" predates the corpus-DB rebuild (3072-byte
  embeddings) and is not comparable in this container; treat 0.9815 as
  the reference for ranking-unchanged checks.
- `graph_traverse_with_limit` CTE is already split per-direction by
  design; no OR-join left on the graph path.
