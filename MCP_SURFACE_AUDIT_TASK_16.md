# Task 16 — Full MCP Tool Surface Audit

**Contract**: 23+ tools (`remember`, `recall`, `forget`, `list`, `context`, `graph`, `hierarchy`, `bootstrap`, `update`, `consolidate`, `used`, `prefetch`, `sync_turn`, `persona`, `canonical`, `triples`, `constraint_*`, `sleep`, `diagnose`, `export`, `import`, `sync_*`) + dotted + underscore aliases (`mnemosyne_memory_search`, `mnemosyne_memory_remember`, etc.).
**Evidence**: Adapter (`src/lib/storage.py`, `mnemosyne_client.py`), docs/MCP_CLIENT_CONFIGS.md, user spec, HERMES_INTEGRATION.md.

---

## 1. Adapter / Client Method Inventory

`src/lib/storage.py` (Python adapter backend):
- `remember()` — `CREATE` memory; basic metadata (id, namespace, content, summary, keywords, tags, context, memory_type, importance, confidence).
- `recall()` — `SELECT` with `LIKE %query%`; no FTS5 `MATCH`, no BM25, no adaptive weights.
- `list_memories()` — `SELECT * FROM memories` with namespace/sort filter; `sort_by='recent'` or `'importance'`.
- `consolidate()` — basic consolidation logic (not full LLM-assisted; no graph-based importance recalibration).
- `graph()` — namespace-level grouping only; no `memory_links` query, no typed relationships, no reverse edges.
- `count()` — simple count by namespace.

`src/lib/mnemosyne_client.py` (async client): Same 5 methods (`remember`, `recall`, `list_memories`, `consolidate`, `graph`) + `resolve_db_path()`.

---

## 2. Contract vs Adapter — Tool-by-Tool Audit

| Contract Tool | Adapter / Client Present? | Evidence / Gap |
|---|---|---|
| `remember` / `mnemosyne_memory_remember` | ✅ `remember()` exists | Basic storage; no enrichment, no namespace-specific memory-type checks |
| `recall` / `mnemosyne_memory_search` | ✅ `recall()` exists | `LIKE` only; no FTS5 `MATCH`, no BM25 (`task 2`), no adaptive weights (`task 12`), no hierarchical rerank (`task 5`) |
| `forget` | ❌ MISSING | No `DELETE` or archive method in adapter |
| `list` / `mnemosyne_memory_list` | ✅ `list_memories()` exists | Basic filter/sort only |
| `context` | ❌ MISSING | No `context()` method; no `mnemosyne.context` CLI (per `docs/HIERARCHICAL_MEMORY.md`) |
| `graph` / `mnemosyne_memory_graph` | ⚠️ `graph()` exists (minimal) | Namespace grouping only; no `memory_links` query; no typed relationships (`task 4`); no reverse edges; no weight decay |
| `hierarchy` / `mnemosyne_memory_hierarchy` | ❌ MISSING | No `L0`/`L1`/`L2` tier retrieval; no `RetrievalTrajectory` (`task 5`) |
| `bootstrap` / `mnemosyne_bootstrap` | ❌ MISSING | No `bootstrap()` method (`task 10`); contract preserved in `docs/BOOTSTRAP.md` |
| `update` | ❌ MISSING | No `update()` method for memory modifications; no `work_items` update logic (`task 13`); no `memory_modification_log` access |
| `consolidate` | ⚠️ `consolidate()` exists (minimal) | Basic consolidation; no LLM-assisted analysis (`task 13`), no graph-based importance recalibration, no link decay audit |
| `used` / `mnemosyne_memory_used` | ❌ MISSING | No `used()` feedback tracking (`task 12`); no `used_result_ids` collection |
| `prefetch` / `mnemosyne.prefetch` / `mnemosyne_prefetch` | ❌ MISSING | No `prefetch()` method; contract preserved in `docs/HIERARCHICAL_MEMORY.md` / HERMES_INTEGRATION.md |
| `sync_turn` / `mnemosyne_sync_turn` | ❌ MISSING | No `sync_turn()` method; no session capture/injection (`task 21`) |
| `persona` / `mnemosyne_persona` / `mnemosyne_memory_persona` | ❌ MISSING | No persona methods (`task 7`); no `profile:identity` filter; no system-prompt injection |
| `canonical` / `mnemosyne_canonical` / `mnemosyne_memory_canonical` | ❌ MISSING | No canonical methods (`task 8`); no `mnemosyne_canonical` MCP tool |
| `triples` / `mnemosyne_memory_triples` | ❌ MISSING | No triples storage/retrieval (`task 9`); no `mnemosyne_triples` tool |
| `constraint_*` / `mnemosyne_constraint_*` | ❌ MISSING | No constraint proposal/review/supersede methods (`task 10`); no `constraint_status:approved` filter in adapter; `constraint_proposals` migration exists (`024`) but adapter doesn't load |
| `sleep` / `mnemosyne_sleep` / `mnemosyne_memory_sleep` | ❌ MISSING | No `sleep()` method; no `reasoning_memory_items` / `reasoning_experiences` time-based retrieval (`task 6`) |
| `diagnose` / `mnemosyne_memory_diagnose` | ❌ MISSING | No `diagnose()` method; no `retrieval_traces` diagnostics (`task 12`) |
| `export` / `mnemosyne_memory_export` | ❌ MISSING | No `export()` method; `docs/BOOTSTRAP.md` references `mnemosyne export` CLI (not implemented) |
| `import` / `mnemosyne_memory_import` | ❌ MISSING | No `import()` method; adapter has no import pipeline; `docs/HERMES_INTEGRATION.md` references import from `mnemosyne.db` (`work_items`, `canonical_facts`, etc.) |
| `sync_*` / `mnemosyne_sync_*` (all sync variants) | ❌ MISSING | No sync methods; `mnemosyne_memory_sync`, `mnemosyne_sync_*` aliases not implemented |
| `context` / `mnemosyne_memory_context` | ❌ MISSING | Not in adapter |

---

## 3. Alias Verification

User spec requires: `dotted + underscore aliases` (e.g., `mnemosyne_memory_search`, `mnemosyne_memory_remember`). Adapter (`mnemosyne_client.py`) uses method names without underscore aliases; no alias mapping layer. The `MCP_SERVER.md` (if existed) or adapter surface doesn't define alias mappings.

---

## 4. Contract Evidence References

- User spec (task 16): 23+ tools listed with dotted + underscore aliases.
- `docs/HERMES_INTEGRATION.md`: `mnemosyne_memory_search` / `mnemosyne_memory_remember` referenced as contract tool names; `mnemosyne_persona`, `mnemosyne_canonical`, `mnemosyne_triples`, `mnemosyne_bootstrap` mentioned.
- `docs/BOOTSTRAP.md`: `mnemosyne.bootstrap` CLI; `mnemosyne_bootstrap` alias.
- `docs/HIERARCHICAL_MEMORY.md`: `mnemosyne.recall` gains `hierarchical` + `trajectory`; `mnemosyne.hierarchy`; `mnemosyne.used`; `mnemosyne.prefetch` / `mnemosyne.sync_turn` (provider lifecycle tools).
- `migrations/sqlite/*.sql`: Tables support all contract surfaces; adapter doesn't load them.
- Adapter (`src/lib/storage.py`, `mnemosyne_client.py`): Only 5 methods (`remember`, `recall`, `list_memories`, `consolidate`, `graph`).

---

## 5. Blocker Note

Blocked by upstream `785067...` (full MCP tool surface from upstream `mnemosyne-memory 3.15.1` unavailable), DB clone MISSING, native binary MISSING. Contract preserved (docs + user spec); adapter gap documented (only 5 of 23+ tools implemented; no aliases; no FTS5 BM25; no adaptive weights; no evaluation feedback; no hierarchical retrieval; no profile/constraint/triple/bootstrap/update/import/export/diagnose/sleep/sync_* methods). Repair requires adapter rebuild + binary build + DB clone verification.
