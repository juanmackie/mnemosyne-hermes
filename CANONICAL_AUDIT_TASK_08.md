# Task 8 — Canonical Facts Audit

**Contract**: One current value per slot (`category`, `name`); `include_history` flag; supersede audit trail.
**Evidence**: User contract (task 8 spec); `docs/HERMES_INTEGRATION.md`; adapter; migrations.

---

## 1. Contract (From User Spec / HERMES_INTEGRATION)

`docs/HERMES_INTEGRATION.md` (line 235-236): `canonical_facts` is listed as a common Python provider table (import path). The user spec defines:
- Slot key: (`category`, `name`) → one current value.
- `include_history` flag (return superseded values).
- Supersede audit trail (`superseded_by` / `audit_log`).
- Contract preserved: `mnemosyne_canonical` MCP tool.

---

## 2. Schema / Migration Status

- NO dedicated `migrations/*canonical_facts*` or `canonical_facts.sql` file exists.
- NO `canonical_facts` table defined in `sqlite/001_initial_schema.sql` or `libsql/001_initial_schema.sql`.
- The `memories` table (`001_initial_schema.sql`) has `superseded_by` (self-referencing FK) and `audit_log` has `supersede` operation — these support supersede audit for ANY memory, including canonical facts.
- `memory_modification_log` (ghost migration 011) could track supersede events, but file missing.
- `work_items` (ghost 011/012) could hold canonical slots (category/name), but file missing.

The canonical-facts contract relies on the general `memories` supersede mechanism (`superseded_by` + `audit_log`) + an external mapping of (`category`, `name`) to `memory.id`. No separate canonical table exists.

---

## 3. Adapter Status (No Implementation)

`src/lib/storage.py`: No `canonical_facts`, `canonical`, `slot`, or `include_history` references.
- `mnemosyne_canonical` tool (contract) not implemented in adapter.
- `supersede()` logic exists in `audit_log.operation` (`supersede`) but adapter has no `update()` or `supersede()` method for canonical slots.

---

## 4. Contract Audit (Verified / Gaps)

| Requirement | Evidence | Status |
|---|---|---|
| Slot (`category`, `name`) | No dedicated table; could use `namespace` + `memory_type` + `tags` (JSON array) as proxy; contract specifies dedicated canonical slot mapping | ❌ Schema missing; adapter missing |
| `include_history` flag | `audit_log` supports history; `superseded_by` links historical value; `include_history` would filter `audit_log` + `memories` by `supersede` chain | ⚠️ Schema supports via `audit_log` + `superseded_by`; adapter missing |
| Supersede audit trail | `audit_log.operation` includes `supersede`; `memories.superseded_by` FK; `memory_modification_log` (ghost) missing | ⚠️ Partial (audit_log + supersede_by present); ghost migration unrecoverable |
| `mnemosyne_canonical` MCP tool | Referenced in `docs/HERMES_INTEGRATION.md`; adapter/CLI missing | ❌ Adapter missing |

---

## 5. Blocker Note

Blocked by upstream source `785067...` (canonical facts contract reference from upstream `mnemosyne-memory 3.15.1` unavailable), DB clone (`MNEMOSYNE_DB_PATH` NOT FOUND), native binary MISSING. Design contract preserved; adapter/schema gap documented for repair when upstream available.
