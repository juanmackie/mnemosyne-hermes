# Ideas backlog

Seeded from recon (2026-09-07). Ordered by expected win / effort.

1. **Reuse one libsql `Connection` instead of `db.connect()` per call**
   (`src/storage/libsql.rs:1922`). ~8-10 connects per `hybrid_search`, one per
   `store_memory`. libsql `Connection` is `Clone` and cheap; hold one per
   `LibsqlStorage` (or a small pool) and hand out clones. Suspected dominant win
   on every metric, including `ingest_ms`.
2. **Memoize `connection_has_column` / `connection_has_table`** (26 call sites,
   several per query: 3805, 7025, 7331, 7387, 10086). Schema cannot change while
   the process runs → `OnceCell`/`HashSet<(table,column)>` on the struct.
3. **Index `memory_links (source_id)` and `(target_id)`** (check `migrations/`
   first). Then split the `OR` joins in `graph_traverse_bounded` and
   `fetch_ppr_adjacency` into two indexed halves + `UNION`. `graph_traverse_bounded`
   is 292 ms for 290 rows at 300 memories — worst single channel.
4. **Cache `retrieval_setting` / `retrieval_weights`** (a settings-table read per
   query, plus one inside `record_retrieval_trace`).
5. **Prefer `EXISTS`/narrow select in `active_ppr_nodes`** — it re-checks every
   visited node against `memories` with an `IN (...)` of the whole visited set.
6. **`get_memories_batch` chunking** — verify it does not build one huge `IN (...)`
   per query (SQLite parses it each time); reuse a prepared statement per chunk size.
7. **PPR power iteration on indices instead of `HashMap<&str>`** (`src/utils/ppr.rs`)
   — precompute out-strength once per iteration, index nodes into a `Vec`, use
   `VecMap`-style dense arrays. Only worth it if the bench shows the blend
   (`ppr_delta_ms`) mattering; currently ~150 ms of p95 at 800 memories.
8. **Prepare statements once and reuse** (`Connection::prepare`) for the fixed
   hot SQL instead of `conn.query(&format!(...))` strings — biggest wins where
   the SQL text is actually constant (keyword_search, batch fetch, trace insert).
9. Scale ladder: once the fixed per-query overhead is gone, re-baseline at
   10 000 memories (`BENCH_MEMORIES=10000`) and see what actually scales.
