# Task 13 — Consolidation Audit (Dedup, Importance Recalibration, Link Decay, Archival, Supersede Audit)

**Contract**: LLM-assisted analysis option available; link decay + supersede audit trail verified.
**Evidence**: `migrations/sqlite/030_consolidation.sql`, adapter `consolidate()` (line 435+), `docs/archive/comprehensive-testing-final-report.md`, `docs/archive/comprehensive-testing-session-summary.md`, archive design docs.

---

## 1. Migration Schema (`030_consolidation.sql`)

Migration 030 defines consolidation bookkeeping (not schema change to `memories`, but audit/staging tables):

### `consolidation_runs`
- `id` (PK, TEXT)
- `started_at` (TEXT NOT NULL), `finished_at` (TEXT, nullable)
- `dry_run` (INTEGER 0/1, default 0)
- Metrics: `groups_found`, `duplicates_superseded`, `edges_repointed`, `edges_tombstoned`, `vectors_tombstoned`, `lineage_repointed`, `orphan_links_staged`
- Before/after counts: `active_before`, `active_after`

### `consolidation_tombstones`
- `id` (PK autoincrement), `run_id` (FK to `consolidation_runs.id`), `reason` (TEXT), `payload` (TEXT — serialized tombstone data), `created_at`
- Index: `run_id`

**Design contract**: Consolidation uses supersede (not hard delete) for exact duplicates; dangling link edges are repointed; supersede audit trail preserved via `audit_log` + `superseded_by` (existing `001_initial_schema.sql`) + this new `consolidation_tombstones` staging table. Link decay and importance recalibration are design-level contracts (not fully implemented in adapter).

---

## 2. Adapter Implementation (`consolidate()` — Line 435+)

`src/lib/storage.py` (`consolidate()`):

- **Basic dedup only**: groups by `namespace` + first 50 chars of `content` (`key = f"{row[2]}:{row[1][:50].lower()}"`).
- **Simple delete** (not supersede): `DELETE FROM memories WHERE id = ?` for duplicate IDs (except first in group). This is **hard delete**, not supersede (violates contract: exact-duplicate dedup supersedes, never deletes, per `030_consolidation.sql` description).
- **No `dry_run` logic**: adapter ignores `dry_run` parameter (always deletes if `auto_apply=True`); no preview/staging.
- **No `audit_log` insertion**: adapter does not write `audit_log.operation='consolidate'` or link to `consolidation_runs`.
- **No link decay / importance recalibration**: adapter does not update `memory_links.strength`, does not recalculate `importance`, does not create `consolidation_tombstones` entries.
- **No LLM-assisted analysis**: adapter uses simple content-prefix grouping; no LLM call or analysis option available (contract: "LLM-assisted analysis option available"). The adapter's `optimizer.py` has `optimizer()` logic but is not called from `consolidate()`.
- **No graph-based importance recalibration**: adapter doesn't traverse graph or recalculate importance based on graph proximity.

---

## 3. Contract vs Implementation — Audit Evidence

| Contract Requirement | Migration / Contract Evidence | Adapter Evidence (`storage.py`) | Status |
|---|---|---|---|
| Exact-duplicate dedup supersedes (never deletes) | `030_consolidation.sql`: `duplicates_superseded` metric; `consolidation_tombstones` for supersede audit; `dry_run` flag; `edges_repointed` / `edges_tombstoned` / `lineage_repointed` | `DELETE FROM memories WHERE id = ?` (hard delete) — violates supersede contract; no tombstone staging | ❌ Adapter violates contract |
| Link decay (graph edges) | `030_consolidation.sql`: `edges_tombstoned`, `lineage_repointed`; contract implies decay of `memory_links.strength` over time; adapter `graph()` has `last_traversed_at` (not decay logic) | No decay formula (`strength *= exp(-lambda * age_days)`) applied; `graph()` returns flat namespace edges only (`GRAPH_AUDIT_TASK_04.md`) | ⚠️ Contract preserved; adapter missing |
| Importance recalibration (graph-based) | Contract: graph-based recalibration; adapter `consolidate()` uses fixed `LIKE` grouping (no graph traversal, no importance recalibration) | No `importance` update logic; no `memory_links` query for recalibration (`GRAPH_AUDIT_TASK_04.md`) | ❌ Adapter missing |
| Archival / supersede audit trail | `audit_log.operation='supersede'` + `memories.superseded_by` FK (`001_initial_schema.sql`); `030_consolidation.sql`: `consolidation_tombstones` + `consolidation_runs` | Adapter does NOT write `audit_log`; does NOT create `supersede` links; does NOT insert `consolidation_runs` / `tombstones` | ❌ Adapter missing audit trail |
| LLM-assisted analysis option | Contract: available; adapter has no `LLM-assisted analysis` parameter or integration with `optimizer.py` / `reviewer.py` / `executor.py` agents; design docs (`docs/archive/TEST_REPORT_LIBSQL_MIGRATION.md`) mention LLM enrichment but adapter `consolidate()` doesn't use it | `optimizer.py` has `stopwords` + keyword extraction logic (`optimizer()`); `agent_factory.py` has agent definitions; adapter `consolidate()` does not call `optimizer()` or any agent | ⚠️ Contract preserved (LLM-assisted option available in design); adapter doesn't wire it |
| Dry-run / preview (`dry_run`) | `030_consolidation.sql`: `dry_run` column (`0`/`1`); `consolidation_runs` records dry-run results | Adapter ignores `dry_run` (always applies delete when `auto_apply=True`) | ❌ Adapter violates contract |

---

## 4. Archive / Design Evidence

- `docs/archive/comprehensive-testing-final-report.md`: no consolidation benchmark results; design-level reference only.
- `docs/archive/comprehensive-testing-session-summary.md`: no consolidation-specific tests (adapter consolidation is minimal; no full integration suite executed).
- `migrations/sqlite/030_consolidation.sql`: contract preserved (migration file complete and registered in `MANIFEST.md` as 2026-08-30).
- `docs/archive/RUST_ARCHIVE_REF.md`: previous Rust implementation (`feat/hermes-native-provider` at `09a6973`) had richer consolidation logic; adapter (Python) is a reduced version.
- `docs/plans/item_03_m2_data_model.md`: `consolidation_runs`, `consolidation_tombstones` listed in complete table inventory; blocker (`785067...`) noted.

---

## 5. Blocker Note

Task 13 is **unblocked** (does not require upstream `785067...`, DB clone, or native binary — it operates on adapter-level design contract and existing migration files). The adapter's `consolidate()` does not fully implement the contract (no supersede audit trail, no LLM-assisted analysis, no link decay, no importance recalibration, violates supersede contract with hard delete). This audit verifies the contract gap and provides the evidence for repair when upstream/binary available (or for adapter hardening in task 17).

---

## 6. Verification Contract (Task 13)

- [x] `migrations/sqlite/030_consolidation.sql` read; `consolidation_runs` + `consolidation_tombstones` tables verified.
- [x] Adapter `consolidate()` (line 435+) inspected: basic `LIKE` grouping + hard delete; no supersede; no audit log insertion; no dry-run logic; no LLM integration.
- [x] `docs/archive/comprehensive-testing-*` checked: no consolidation-specific benchmark results.
- [x] `docs/plans/item_03_m2_data_model.md`: `consolidation_*` tables listed; blocker noted.
- [x] `docs/archive/RUST_ARCHIVE_REF.md`: previous Rust consolidation logic preserved (archive reference).
- [x] Contract audit completed; adapter gap documented with exact line references (`storage.py` lines 435+, `delete` line). LLM-assisted analysis option preserved (design contract) but adapter does not wire it.
