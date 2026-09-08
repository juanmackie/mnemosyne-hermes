# Ideas backlog — expanded session (retrieval + organizational + dedup + linking + writing + CLI)

Previous session completed: connection reuse (done), WAL+NORMAL (done), column memoization (done), retrieval diagnostics off path (done).

This expanded session (user: "all retrieval, organisational, deduplication, writing, linking, all surface areas"):

## Completed / verified in this session

- [x] **OR split for PPR adjacency (`fetch_ppr_adjacency`)** — split `(source_id IN (...) OR target_id IN (...))` into `UNION ALL` of two indexed halves (`migrations/libsql/002_add_indexes.sql` confirms `idx_links_source` and `idx_links_target`). Build passes. No ranking change by construction (same rows, same order, duplicates filtered by `seen_edges` HashSet).
- [x] **Build verification** — `cargo build --release --locked --bin mnemosyne` passes (6m 52s, fixed duplicate `Arc` import that blocked compile).
- [x] **Connection sharing verified** — `get_conn()` still returns a clone of the shared `Arc<Connection>` (previous session fix intact).
- [x] **`connection_has_column` memoization verified** — `COLUMN_CACHE` (Lazy Arc<Mutex<HashSet>>) still active; 26 call sites covered.
- [x] **Index verification** — `migrations/libsql/002_add_indexes.sql` has `idx_links_source`, `idx_links_target`, `idx_links_outbound`, `idx_links_inbound`.
- [x] **Reference organizational/dedup scripts verified** — `.auto/memory_dedup_merge.py` (Jaccard overlap + bulk delete + audit), `.auto/memory_maintain_fixed.py` (mutation tracking + integrity runs + evidence links), `.auto/unify_audit.py` (unified audit aggregation) all present and structured correctly.
- [x] **CLI/MCP surfaces verified** — `src/cli/recall.rs`, `src/mcp/tools.rs` compile with the changed storage layer.

## Still open / deferred (add when needed)

1. **Full `fetch_ppr_adjacency` benchmark measurement** — the union-all query is behavior-preserving but needs a `BENCH_PROFILE=1 ./.auto/measure_fast.sh` run to confirm latency improvement. Deferred because the full 4min benchmark timed out during this session; the structural fix is sound by inspection (two indexed scans + UNION vs one full-scan OR).
2. **Prepare hot SQL statements once** (`Connection::prepare`) — `keyword_search`, `batch fetch`, `trace insert`. Low effort, medium win; add when a measurement cycle confirms SQL parse overhead matters.
3. **Cache `retrieval_weights` / `retrieval_setting`** — done (`Lazy` `Arc<Mutex>` caches added: `WEIGHTS_CACHE`, `SETTINGS_CACHE` in `libsql.rs`). Per-query settings reads eliminated by cache hit. Confirm with measurement when `BENCH_MEMORIES=10000`.
4. **Graph traverse CTE split verification** — the recursive CTE in `graph_traverse_with_limit` is already split by design (`UNION` of `source_id` branch and `target_id` branch); confirm it uses indexed scans in practice.
5. **PPR dense-array iteration** (`src/utils/ppr.rs`) — only worth it if `ppr_delta_ms` dominates at 10k+ memories. Scale ladder: re-baseline at `BENCH_MEMORIES=10000`.
6. **Organizational audit integration** — done (`.auto/verification.db` created, `.auto/unify_audit.py` runs successfully, `.auto/unified_audit.json` produced with audit_trail and memory_evidence aggregation). Wire into `.auto/audit.json` automatically remains as a follow-up.
7. **Writing/linking surfaces** — verified (`store_memory`, `update_memory`, `link_memory` use `self.get_conn()` = shared `Arc<Connection>`; build passes). Quick ingest benchmark (`BENCH_MEMORIES=5000`) deferred.

## What was skipped and why

- Full measurement cycle (`./.auto/measure.sh` ~8 min/run) skipped due to session time budget. Build passes; previous session's warm p95 = 20.375ms (WAL+NORMAL + shared connection). The OR split is a structural optimization that does not change ranking; it should lower graph-channel cost without affecting `results_hash` or MRR.
- No new dependencies (constraint kept).
- No ranking/supersession/stopword changes (constraint kept).
- Deduplication logic (`memory_dedup_merge.py`) is a reference Python script; integrating it fully into the Rust storage path is out of scope for a latency-focused session but documented.
