# Task 9 — Knowledge Triples (Temporal S-P-O + Supersede) Audit

**Contract**: Temporal subject-predicate-object (`valid_from`/`valid_until`); multi-valued via `supersede=false`; supersede logic implemented.
**Evidence**: User spec (task 9); `docs/HERMES_INTEGRATION.md` (mnemosyne_triples); `docs/features/ICS_ARCHITECTURE.md`; adapter; migrations.

---

## 1. Contract References

- `docs/HERMES_INTEGRATION.md` (line 226-227): `mnemosyne_triples` — one current object per (`subject`, `predicate`) slot; supersede archive preserved; supersede audit trail.
- `docs/features/ICS_ARCHITECTURE.md`: triples extraction (subject-predicate-object) used in ICS semantic analysis; real-time extraction for editor diagnostics.
- User spec: `valid_from`/`valid_until`; multi-valued via `supersede=false` (i.e., new triple does NOT supersede previous — allows multiple concurrent values); supersede logic implemented.

---

## 2. Schema / Migration Evidence

- NO dedicated `migrations/*triples*` or `triples.sql` file exists.
- `memories` table (`001_initial_schema.sql`) supports temporal fields (`created_at`, `updated_at`, `expires_at`, `superseded_by`, `archived_at`) but does NOT have `valid_from`/`valid_until` or `subject`/`predicate`/`object` columns.
- `audit_log` (`001_initial_schema.sql`) has `operation` (`create`, `update`, `archive`, `supersede`, `link_create`, etc.) — supports supersede audit.
- `memory_modification_log` (ghost 011) could track supersede events; file missing.
- The contract implies triples could be stored as `memories` with a structured `keywords` or `tags` JSON mapping, or as a separate `triples` table (not present). The user's spec requires `supersede=false` for multi-valued triples — this is a logical flag (not a DB column in current schema), handled by adapter logic.

---

## 3. Adapter Status (No Implementation)

`src/lib/storage.py`: NO `triples`, `mnemosyne_triples`, `valid_from`, `valid_until`, or `supersede` logic beyond the basic `supersede` operation in `audit_log`.
- No `subject`/`predicate`/`object` extraction or storage.
- No temporal query (`valid_from <= ?` / `valid_until >= ?`).
- No `supersede=false` logic (would require adapter-level decision: if `supersede=false`, create new memory with new ID rather than updating `superseded_by`).

---

## 4. Contract Audit (Design Verified / Adapter Missing)

| Requirement | Evidence | Status |
|---|---|---|
| Subject-Predicate-Object | ICS architecture describes extraction; adapter has no storage | ⚠️ Contract defined; adapter missing |
| `valid_from` / `valid_until` | Not in `memories` schema; could use `created_at` / `updated_at` / `expires_at` as proxies; no explicit temporal range fields | ❌ Schema missing dedicated temporal fields |
| Multi-valued (`supersede=false`) | `supersede=false` requires adapter-level logic (create new row instead of superseding); no adapter method exists | ❌ Adapter missing |
| Supersede logic (`supersede=true`) | `audit_log.operation='supersede'`; `memories.superseded_by` FK; `memory_modification_log` ghost missing | ⚠️ Partial (audit + supersede_by present); ghost unrecoverable |
| `mnemosyne_triples` MCP tool | Referenced in `docs/HERMES_INTEGRATION.md`; adapter/CLI missing | ❌ Adapter missing |

---

## 5. Blocker Note

Blocked by upstream `785067...` (triples contract from upstream `mnemosyne-memory 3.15.1` unavailable), DB clone MISSING, native binary MISSING. Design contract preserved (ICS architecture + HERMES_INTEGRATION); adapter/schema gap documented.
