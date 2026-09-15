# M2 — Preserve Complete Data Model (Deliverable)

Status: INVENTORY COMPLETE / ADOPTION BLOCKED. Existing migration files (sqlite/libsql) account for all required tables. Migration MANIFEST.md (2025-11-04) records applied/ghost/obsolete status. Schema divergence between Global DB (LibSQL native vectors in memories.embedding) and Project DB (separate memory_embeddings table) is documented and preserved.

Complete table inventory (from migrations/sqlite/*.sql + migrations/libsql/*.sql): memories, memory_embeddings, memory_links, audit_log, memories_fts, metadata, memory_modification_log, work_items, agent_sessions, agent_preferences, memory_coaccesses, evolution_job_runs, importance_history, context_evaluations, learned_relevance_weights, relevance_features, session_transcripts, retrieval_traces, consolidation_runs, consolidation_tombstones, constraint_proposals, evaluation_config, interaction_policies, interaction_policy_evidence, interaction_policy_proposals, memory_change_proposals, memory_entities, memory_evidence, memory_facts, memory_maintenance_runs, memory_modifications, memory_provenance, reasoning_experiences, reasoning_memory_items, retrieval_adaptive_weights, retrieval_evaluation_config, retrieval_evaluation_runs, retrieval_golden_items, version_check_cache, weight_update_history.

Blocker: Upstream `mnemosyne-memory 3.15.1` source (`78506708aae344635e01a24f67a7319efc36fce9`) MISSING; verified DB clone (`MNEMOSYNE_DB_PATH`) NOT FOUND; `.auto/data/template.db` (benchmark) exists but not reconciled with upstream. Any adoption/rebuild of upstream schema requires these artifacts.

Completed (evidence only):
- `migrations/sqlite/*.sql` and `migrations/libsql/*.sql` preserved (49 SQL files).
- `migrations/MANIFEST.md` preserved; ghost migrations (003, 011, 012) documented.
- `migrations/libsql/001_initial_schema.sql` defines memories, memory_links, audit_log, FTS, metadata.
- `migrations/libsql/006_vector_search.sql` defines sqlite-vec vector table (vec0).
- All public contracts preserved: namespace (`agent:hermes`), DB path (`MNEMOSYNE_DB_PATH`), memory types, links, audit events.

Not done (intentionally minimized): No new migrations applied; no upstream schema adopted; no DB rebuilt; no design changes to existing schema.
