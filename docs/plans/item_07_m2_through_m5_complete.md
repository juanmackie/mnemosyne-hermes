# M2–M5 Complete — Planning/Deliverable Artifacts (No Fabricated Implementation)

Status: ALL DELIVERABLE ARTIFACTS CREATED. Implementation BLOCKED by upstream source `mnemosyne-memory 3.15.1` (`78506708aae344635e01a24f67a7319efc36fce9`) MISSING and verified DB clone (`MNEMOSYNE_DB_PATH`) NOT FOUND.

Evidence: `migrations/MANIFEST.md` (49 SQL files); `migrations/sqlite/001_initial_schema.sql` and `migrations/libsql/001_initial_schema.sql`; `docs/plans/item_03_m2_data_model.md`, `item_04_m3_capture.md`, `item_05_m4_retrieval.md`, `item_06_m5_maintenance.md` (all created in this session).

Completed (observable artifacts only):
- M2: Data model inventory preserved; all 49 migration files preserved; schema divergence (Global DB LibSQL F32_BLOB vs Project DB separate embeddings) documented; ghost migrations (003, 011, 012) preserved; no upstream adoption (blocked).
- M3: Capture pipeline contracts preserved (user-origin turns; skip contexts; raw source + pending job; direct SQLite memory path via `PythonMemoryStorage`; keyless `no-enrich` preserved; checkpoint v2 preserved; no JSONL fallback in memory path).
- M4: Retrieval/embedding references preserved (`ARCHITECTURE.md`, `HIERARCHICAL_MEMORY.md`, `REASONING_MEMORY.md`, `BOOTSTRAP.md`); embedding identity (`bge-small-en-v1.5`) preserved; vector index design (`libsql/006_vector_search.sql`) preserved. Tuning/retrieval parity tests deferred.
- M5: Security contracts preserved (`secrets.age`, no secrets in repo); backup/integrity design preserved (`sqlite3 .backup`, `integrity_check`, WAL `TRUNCATE`); self-hosting contracts preserved (`mnemosyne serve`, MCP stdio/HTTP). Implementation/deployment tests deferred.

Blocked (explicit):
- `mnemosyne-memory 3.15.1` upstream source: MISSING (documented in M0 and M1 artifacts).
- Verified DB clone (`MNEMOSYNE_DB_PATH` / `~/.hermes/mnemosyne/mnemosyne.db`): NOT FOUND (`.auto/data/template.db` is benchmark, not reconciled live clone).
- Deployed binary (`mnemosyne`): NOT FOUND.
- Any upstream schema adoption, migration execution, DB reconciliation, embedding identity verification, or retrieval quality/regression measurement: IMPOSSIBLE without upstream source and verified DB clone.

Contracts preserved through M0–M5 artifacts: provider (`mnemosyne-rust` / future `mnemosyne`), namespace (`agent:hermes`), DB path (`MNEMOSYNE_DB_PATH`), tool names, memory types, audit events, links, embeddings, checkpoints, profiles. No contracts broken; no fabricated upstream artifacts added.
