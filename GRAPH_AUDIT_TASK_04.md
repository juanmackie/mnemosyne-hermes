# Task 4 — Graph Links Audit (Bidirectional Edges, Typed Relationships, Weight Decay)

**Contract**: Auto-create reverse edges; weight decay over time implemented; types: `extends`, `references`, `supersedes`, `caused`, `ctx`, `rel`, `syn`.
**Evidence method**: Read adapter (`src/lib/storage.py`), migrations (`migrations/sqlite/001_initial_schema.sql`, `migrations/libsql/001_initial_schema.sql`), archive docs (`docs/archive/TEST_REPORT_LIBSQL_MIGRATION.md`), docs (`docs/ARCHITECTURE.md`, `docs/features/`), and check for any weight-decay logic.

---

## 1. Migration Schema (Memory Links)

`migrations/sqlite/001_initial_schema.sql` defines:

```sql
CREATE TABLE IF NOT EXISTS memory_links (
    ... link_type IN (
        'extends', 'builds_upon', 'contradicts', 'implements',
        'references', 'referenced_by', 'clarifies', 'supersedes'
    ),
    strength REAL NOT NULL DEFAULT 0.5 CHECK(strength BETWEEN 0.0 AND 1.0),
    ...
    -- Unique constraint prevents duplicate links
    UNIQUE (source_id, target_id, link_type)
);
```

- **Status**: ✅ Migration file defines typed relationships (8 types) and `strength` (0.0–1.0). Bidirectional lookup indexes (`idx_links_target_source`) present.
- **Note**: The user's contract specifies 7 types (`extends`, `references`, `supersedes`, `caused`, `ctx`, `rel`, `syn`) — slightly different naming from SQL enum. The adapter must map/translate between contract types and SQL enum.

---

## 2. Adapter Implementation (Graph / Links)

`src/lib/storage.py` (`graph()` method, line 492+):

- Creates `nodes` from `list_memories()` (first 100 chars only).
- Creates `edges` only by grouping memories by `namespace`: `for ns, ids in by_namespace.items(): for i ... edges.append({"source": ..., "target": ...})`.
- **Does NOT query `memory_links` table.**
- **Does NOT create reverse edges.** There is no `INSERT INTO memory_links ...` in adapter code.
- **Does NOT enforce typed relationships.** The adapter has no mapping from contract types (`caused`, `ctx`, `rel`, `syn`) to SQL enum (`caused` is missing from SQL enum; `references` and `referenced_by` are separate; `syn`, `rel` are not in SQL enum).
- **Does NOT apply weight decay.** No `updated_at`-based decay formula (`strength = strength * exp(-lambda * age_days)`) found in adapter or docs.

**Evidence lines** (grep `src/lib/storage.py`):
- `edge` creation: `for i in range(len(ids) - 1): edges.append({"source": ids[i], "target": ids[i+1], ...})` — only namespace-adjacent links, no typed relationships.
- No `memory_links` SELECT query in adapter.
- No `reverse` or `reverse_edges` logic.
- No `strength` update or decay logic.

---

## 3. Weight Decay — Not Implemented Anywhere

Searched repo (`grep -rni -l "decay\|weight_decay\|age_decay"`):
- `src/orchestration/agents/optimizer.py`: `decay` mentioned in context of importance decay (LLM optimization), not graph-edge weight decay.
- No `decay` logic tied to `memory_links.strength` or `last_traversed_at`.

Archive docs (`docs/archive/IMPLEMENTATION_COMPLETE.md`):
- Graph traversal tested (✅), but no mention of time-decay on link weights.

---

## 4. Contract vs Migration Mismatch (Important Finding)

| Contract Type (User Spec) | SQL Enum (`memory_links`) | Match? |
|---|---|---|
| `extends` | `extends` | ✅ |
| `references` | `references` | ✅ |
| `supersedes` | `supersedes` | ✅ |
| `caused` | MISSING | ❌ |
| `ctx` | MISSING | ❌ |
| `rel` | MISSING | ❌ |
| `syn` | MISSING | ❌ |

The SQL enum also includes `builds_upon`, `contradicts`, `implements`, `referenced_by`, `clarifies` — which are NOT in the user's contract. Any adapter implementation must either:
1. Add the missing contract types to the SQL `CHECK` enum (requires migration edit — not allowed per migration rules), OR
2. Map contract types to closest SQL types (e.g., `caused` → `references` + reason; `syn` → `references`; `rel` → `references`; `ctx` → `references` + metadata), OR
3. Create a separate contract-specific edge table (exceeds scope).

**Recommendation for adapter hardening**: The adapter's `graph()` method should query `memory_links`, translate types, create reverse edges on INSERT (via trigger or application logic), and apply weight decay based on `last_traversed_at` / `created_at`.

---

## 5. Evidence References

- `migrations/sqlite/001_initial_schema.sql`: `memory_links` definition, indexes, `UNIQUE (source_id, target_id, link_type)`.
- `migrations/libsql/001_initial_schema.sql`: same (LibSQL version).
- `src/lib/storage.py`: `graph()` method (line 492+); `edges` created by namespace grouping only; no `memory_links` SELECT; no reverse-edge creation.
- `docs/archive/TEST_REPORT_LIBSQL_MIGRATION.md`: graph traversal ✅ (3-memory chain, `Implements` link); no weight-decay test.
- `docs/archive/IMPLEMENTATION_COMPLETE.md`: graph links mentioned; no decay logic.
- `docs/plans/item_03_m2_data_model.md`: data model mentions typed relationships but does not specify exact enum.
- `docs/HERMES_INTEGRATION.md`: memory links / graph edges described contractually.

---

## 6. Blocker Note (Unblocked for Audit; Blocked for Full Implementation)

- **Upstream `785067...` source**: MISSING — needed for full adapter contract parity verification against upstream `mnemosyne-memory 3.15.1`.
- **DB clone**: MISSING — can't verify `memory_links` data or reverse-edge triggers against live DB.
- **Native binary**: MISSING — adapter integration tests (task 21) need binary.

This audit confirms: **migrations exist for typed links; adapter does NOT implement reverse edges, typed contract mapping, or weight decay**. The contract audit is complete; full adapter implementation requires the upstream source / DB clone / binary (blocked tasks 3/18/19).
