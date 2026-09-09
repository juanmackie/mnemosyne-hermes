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

- **Bench `results_hash` is not comparable across runs**: the bench builds a
  fresh store with `MemoryId::new()` (random UUIDv4) each run, so the
  id-based hash changes every rebuild regardless of ranking. Guard is only
  meaningful within one store. Fixing (content-derived v5 ids) would touch
  the pinned fixture seed = off-limits; documented instead. The 10k
  "nondeterminism" observed 2026-09-09 was mostly this, masking (and being
  masked by) the real per-store PPR frontier ordering, which is now sorted
  deterministic (#33).

- **PPR nondeterminism (pre-existing, opt-in channel):** at scale the
  `fetch_ppr_adjacency` frontier is a `HashSet` (`next_frontier.into_iter()`)
  and the node/edge budgets (200/400) truncate in traversal order, so PPR
  scores — and bench `results_hash` — vary run-to-run on high-degree stores
  (observed at 10k/degree-6, same code both runs). Deterministic fix = sort
  frontier before iteration (order-stable BFS). Not done here: PPR is
  `enable_ppr=false` in production defaults, so recall is unaffected.
- `PPR_QUERY_BATCH` 50→450 A/B: ppr_delta at 10k looked like 136→21ms but the
  bench tail is unstable (same-code reruns moved results_hash and hybrid_p95
  by >20%); adjacency probe best-of-5 was equal (56 vs 61ms). No evidence of
  gain; constant left at 50. PPR channel costs are traversal+row-parse bound,
  not statement-count bound.
- Bench harness note: `benches/hermes_recall_bench.rs` must be kept in sync
  with `fetch_ppr_adjacency` signature (checks.sh doesn't build benches);
  restored 2026-09-09 after 61625e0 broke `measure_fast.sh` silently.
- ingest at bench scale is slow (10k: ~340s in-bench, 2k: ~57s) — write-path
  optimization would need a dedicated write metric.

- Warm recall is encoder-bound: quiet-state attribution (2026-09-09,
  template-model-backed.db, warm serial MCP): model call 6.5-9.8ms vs keyless
  1.3-3.2ms on the same store -> query encode ~6ms, whole rest of pipeline
  ~1-2ms. The 19-29ms harness band = contention from its own parallel
  evaluate phase, not per-call cost. Floor is the pinned bge-small encoder;
  encoder swap is off-limits. Do not micro-optimize SQL for warm p95 again.
- Dead ends verified 2026-09-09: (a) query-embedding memo — storage's
  embedding_service is None on the real recall path (libsql.rs:1393), so
  there is exactly ONE embed per recall call and harness queries are unique;
  (b) tokio::join!(hybrid lane, embed) in MCP recall — measured <0.5ms in
  quiet state (phase-1 DB ~0.3ms), sub-noise; revisit only if Hermes-side
  concurrency makes DB segments long.

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

## Finalized 2026-09-09 (session 2)
- Stopped after run #36. Main contains all kept latency, determinism, migration,
  consolidation, and benchmark-harness changes; discarded experiments were reverted.
- Quality reference: held-out MRR 0.9815, Hit@1 0.963, Hit@5 1.0.
- Warm MCP recall is in the measured 19-29ms noise band and is encoder-bound;
  further SQL micro-optimization is not justified by quiet-state attribution.
- Deliberately left for explicit re-scope: widen/backfill `memory_links.link_type`
  (ranking/data-loss issue) and high-scale PPR dense-array work (PPR is opt-in).
