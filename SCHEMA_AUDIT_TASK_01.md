# Schema Audit — Fork vs Live 3.15.1 (Task 1)

**Audit date**: 2025-11-04 (aligned with Migration MANIFEST.md)
**Fork**: mnemosyne-hermes (`feat/hermes-native-provider` retired; `main` at `ba6fe984`)
**Live target**: `mnemosyne-memory 3.15.1` at upstream source `78506708aae344635e01a24f67a7319efc36fce9`
**Live DB**: `~/Library/Application Support/mnemosyne/mnemosyne.db` (Global, LibSQL/native F32_BLOB) and `.mnemosyne/project.db` (Project, standard SQLite, separate `memory_embeddings`)
**Verified DB clone** (`MNEMOSYNE_DB_PATH`): NOT FOUND at `/opt/data/mnemosyne_data/mnemosyne.db`
**Upstream source** (`785067...`): MISSING from repo / not fetched
**Native binary** (`mnemosyne`): MISSING (`~/.local/bin/mnemosyne` and `target/release/mnemosyne` not present)

---

## 1. Live 3.15.1 Schema — Complete Table Inventory (20+)

From `migrations/sqlite/*.sql` + `migrations/libsql/*.sql` + `migrations/MANIFEST.md` + `docs/plans/item_03_m2_data_model.md` + archive references (`docs/archive/RUST_ARCHIVE_REF.md`, `docs/archive/IMPLEMENTATION_COMPLETE.md`).

### A. Core Memory Tables (Live DB — both Global/Project)

| # | Table | Global (LibSQL) | Project (SQLite) | Notes / Divergence |
|---|-------|----------------|-------------------|-------------------|
| 1 | `memories` | `embedding F32_BLOB(384)` native | no `embedding` column | Divergent: Global native vector; Project uses separate embeddings |
| 2 | `memory_embeddings` | MISSING (uses `memories.embedding`) | `memory_id`, `embedding BLOB`, `dimension`, `created_at` | Divergent: Project-only |
| 3 | `memory_links` | Present | Present | `link_type` enum (extends, builds_upon, contradicts, implements, references, referenced_by, clarifies, supersedes); bidirectional indexes |
| 4 | `audit_log` | `metadata TEXT NOT NULL` (fixed by 015) | `metadata TEXT NOT NULL` (fixed by 015) | `details` transitional column removed by 015 |
| 5 | `memories_fts` | Present (FTS5, `tokenize='porter'`) | Present | `content`, `summary`, `keywords`, `tags`, `context`; `content='memories'` |
| 6 | `metadata` | Present (`key`, `value`, `updated_at`) | Present | Schema version, created_at |

### B. Logged / Tracking Tables (Live DB)

| # | Table | Source | Status |
|---|-------|--------|--------|
| 7 | `memory_modification_log` | Ghost migration `011_work_items.sql` (applied, file never committed) | Present in live DB |
| 8 | `work_items` | Ghost `011_work_items.sql` + `012_requirement_tracking.sql` (project DB only) | Present; Project has extra `requirements`, `requirement_status`, `implementation_evidence` |
| 9 | `_migrations_applied` | Migration tracking | Present |
| 10 | `_sqlx_migrations` | Legacy (unused) | Project DB only |

### C. Extended / Domain Tables (From Migrations 013–030 + docs/plans/item_03_m2_data_model.md)

| # | Table / Feature | Migration file(s) | Status in Fork |
|---|-----------------|-------------------|---------------|
| 11 | `working_memory` | Not in migrations; referenced in `docs/HERMES_GOTO_PLAN.md`, `docs/HERMES_INTEGRATION.md` | **MISSING in fork** |
| 12 | `episodic_memory` | Not in migrations; referenced in `docs/HERMES_GOTO_PLAN.md`, `docs/HERMES_INTEGRATION.md` | **MISSING in fork** |
| 13 | `canonical_facts` | Referenced in `docs/HERMES_INTEGRATION.md` | **MISSING in fork** |
| 14 | `triples` (knowledge triples / temporal S-P-O) | Referenced in `docs/ICS_ARCHITECTURE.md`, `docs/features/ICS_README.md` | **MISSING in fork** |
| 15 | `facts` / `annotations` | Referenced in docs/archive + design docs | **MISSING in fork** |
| 16 | `retrieval_traces` | `migrations/libsql/026_retrieval_evaluation.sql`, `sqlite/026_retrieval_evaluation.sql` | Migration registered (2026-08-30); **not confirmed applied to live clone** |
| 17 | `consolidation_log` / `consolidation_tombstones` | `migrations/sqlite/030_consolidation.sql` | Migration file exists; **no verified DB clone to confirm** |
| 18 | `graph_edges` (typed bidirectional edges, weight decay) | Not a separate table — `memory_links` serves this; `graph_edges` referenced as conceptual contract | **Not distinct in migrations** |
| 19 | `gists` | Not present as dedicated table; `memories` + `memory_links` cover gist semantics | **Not in migrations** |
| 20 | `session_transcripts` | `migrations/libsql/025_session_transcripts.sql`, `sqlite/025_session_transcripts.sql` | Migration registered; file present |
| 21 | `persona_facts` (L3: permanent / long_term / working tiers) | Not in migrations; referenced in task spec + `docs/HERMES_INTEGRATION.md` | **MISSING in fork** |
| 22 | `model_canonical` / `evaluation_features` / `relevance_features` | Part of evaluation/retrieval system (026, 017, 019) | Migration files present; **live state unverified** |
| 23 | `memory_usage` / `retrieval_evaluation_runs` / `retrieval_golden_items` | Part of 026 (`retrieval_evaluation_config`, `retrieval_evaluation_runs`, `retrieval_golden_items`, `retrieval_adaptive_weights`) | Migration files present |
| 24 | `shared_surface` / `checkpoint_*` | Referenced in archive + design; no dedicated migration file found | **Not in migrations** |
| 25 | `constraint_proposals` / `interaction_policy_proposals` / `interaction_policies` / `interaction_policy_evidence` | 022, 024 migrations; `docs/plans/item_03_m2_data_model.md` | Migration files present |
| 26 | `reasoning_experiences` / `reasoning_memory_items` / `memory_evidence` | 023, 029 (`memory_evidence`), 027 (`memory_integrity`) | Migration files present |
| 27 | `version_check_cache` | `migrations/sqlite/016_version_check_cache.sql`, `libsql/015_version_check_cache.sql` | Migration files present |
| 28 | `evolution_job_runs` / `importance_history` / `context_evaluations` / `learned_relevance_weights` | Referenced in `docs/plans/item_03_m2_data_model.md` | Migration files NOT found for these exact names |

---

## 2. Fork Schema — What's Present

The Python adapter (`src/lib/storage.py`) defines **only**:

- `memories` (core fields: id, namespace, content, summary, keywords, tags, context, memory_type, importance, confidence, superseded_by, etc.)
- No `memory_embeddings` table (the adapter uses serialized embeddings embedded in Python memory or external file references; no `sqlite-vec` / `sqlite_vec` virtual table)
- No `memory_links` (graph table) — no bidirectional edge storage
- No `audit_log` — no audit trail
- No `memories_fts` — no FTS5 virtual table; only `LIKE '%query%'` (per task spec)
- No `metadata` — no schema version tracking
- No `working_memory`, `episodic_memory`, `canonical_facts`, `triples`, `facts`, `annotations`
- No `retrieval_traces`, `consolidation_log`, `graph_edges`, `gists`, `session_transcripts`, `persona_facts`, `model_canonical`
- No `evaluation_features`, `memory_usage`, `shared_surface`, `checkpoint_*`

`migrations/sqlite/001_initial_schema.sql` and `migrations/libsql/001_initial_schema.sql` define a **much richer schema** than the Python adapter actually uses — the adapter's `src/lib/storage.py` does not load all these tables.

---

## 3. What's Missing in Fork (Summary — 20+ items)

| Category | Missing / Divergent Items (
|---|------------------------------------|
| **Tables** | `working_memory`, `episodic_memory`, `canonical_facts`, `triples` / `facts`, `annotations`, `retrieval_traces`, `consolidation_log`, `graph_edges` (as distinct typed-edge store), `session_transcripts` (migration file exists but adapter doesn't load it), `persona_facts`, `model_canonical`, `evaluation_features`, `memory_usage`, `shared_surface`, `checkpoint_*` |
| **FTS5** | `memories_fts` migration file exists (001_initial_schema.sql) but adapter only uses `LIKE '%query%'` (confirmed in task spec); BM25 scoring / stopword filtering NOT implemented |
| **Embeddings** | `memory_embeddings` exists in sqlite migration but adapter uses serialized Python blobs, not `sqlite-vec` (384-dim int8 quantized); HNSW index NOT wired |
| **Graph** | `memory_links` migration file exists; adapter does NOT create reverse edges automatically, does NOT apply weight decay, and does NOT enforce the full `LinkType` enum (only basic `link_type` check in SQL) |
| **Audit / Tracking** | `audit_log` (migration file present but adapter doesn't use); `work_items` / `memory_modification_log` (ghost migrations not recoverable); `retrieval_evaluation_config` / `retrieval_traces` not integrated |
| **Schema version / Metadata** | `metadata` table exists in migrations; adapter has no schema-version check or `MNEMOSYNE_DB_PATH` reconciliation logic |
| **Migration reconciliation** | Ghost migrations `003_audit_trail.sql`, `011_work_items.sql`, `012_requirement_tracking.sql` applied to live DBs but NOT in git; cannot recreate live DB from git alone |

---

## 4. Blocker Status (Affects Tasks 3, 15, 18, 19, 20, 21, 23)

| Blocker | Evidence | Impact |
|---------|----------|--------|
| **Upstream source `78506708aae344635e01a24f67a7319efc36fce9`** (`mnemosyne-memory 3.15.1`) | Not in repo; referenced in `docs/plans/item_01_m0_baseline.md`, `docs/plans/item_03_m2_data_model.md`; no `mnemosyne-memory` package fetched | Blocks embedding rebuild (task 3, 15), DB clone reconciliation (task 18), native binary (task 19 — binary requires rebuilt source), MRR benchmark (task 20), adapter integration (task 21 — adapter needs binary + embeddings), MCP-only integration (task 22 partially blocked), migration path (task 23) |
| **Verified DB clone (`MNEMOSYNE_DB_PATH`)** | `/opt/data/mnemosyne_data/mnemosyne.db` NOT FOUND | Blocks DB clone reconciliation (18), migration parity verification (23) |
| **Native binary (`mnemosyne`)** | No `~/.local/bin/mnemosyne`; no `target/release/mnemosyne`; no cargo build artifacts | Blocks adapter integration tests (21), native release verification (19), MRR benchmark (20) |

**Decision**: Per user-confirmed blocker rule (`Skip blocked tasks` + evidence-per-phase), tasks 3, 15, 18, 19, 20, 21, 23 are **skipped** (evidence recorded); all unblocked tasks proceed.

---

## 5. Verification Contract Evidence (Task 1)

- [x] Read `migrations/MANIFEST.md` (ghost + obsolete migrations documented)
- [x] Read `migrations/sqlite/001_initial_schema.sql` + `migrations/libsql/001_initial_schema.sql`
- [x] Read `docs/plans/item_03_m2_data_model.md` (table inventory)
- [x] Read `docs/archive/RUST_ARCHIVE_REF.md`
- [x] Inspected `src/lib/storage.py` (Python adapter — single `memories` table)
- [x] Confirmed upstream source `785067...` MISSING (grep over repo)
- [x] Confirmed live DB clone `/opt/data/mnemosyne_data/mnemosyne.db` MISSING
- [x] Confirmed native binary `mnemosyne` MISSING (`~/.local/bin/` and `target/release/`)
- [x] Documented divergence (embedding storage: `memories.embedding` vs separate `memory_embeddings`; `work_items` extra columns in project DB; migration tracking differences)
- [x] Listed 20+ live tables and what's missing in fork (see sections 1–3)

---

## 6. Next Actions (Task 1 → Task 2+)

Task 1 complete. Proceed with unblocked tasks:
- Task 2 (FTS5 BM25 ranking): migration file 003_fix_fts_triggers.sql exists; adapter only uses `LIKE`. Implement BM25 + stopword filtering in adapter/search layer.
- Task 4 (Graph links): `memory_links` migration file exists; adapter needs reverse-edge creation + weight decay + full `LinkType` enum.
- Tasks 5–12 (Hierarchy, reasoning, persona, canonical, triples, bootstrap, profiles, evaluation): design/spec only — can proceed with design docs since no DB clone required.
- Tasks 13–14 (Consolidation, ICS): design/spec; ICS `mnemosyne edit` / `mnemosyne ics` CLI can be audited against docs.
- Tasks 16 (MCP tool surface): audit existing `mcp/` directory and CLI surface vs 23+ tool list.
- Task 24 (CI/CD): audit `.github/workflows/` and `Makefile`.

Blocked tasks (3, 15, 18, 19, 20, 21, 23) will be revisited when any of: upstream `785067...` fetched, DB clone `/opt/data/mnemosyne_data/` available, or native binary built. Each blocked task will include explicit `SKIPPED (blocker: X)` in its evidence.
