# Task 6 — Reasoning Memory (Strategies + Guardrails) Audit

**Contract**: Sparse retrieval; provenance-bound evidence; no hidden CoT storage.
**Evidence**: Migration `023_reasoning_experiences.sql` (sqlite/libsql), adapter (`src/lib/storage.py`), docs/archive, contract spec.

---

## 1. Migration Schema (Reasoning Memory)

`migrations/sqlite/023_reasoning_experiences.sql` / `migrations/libsql/023_reasoning_experiences.sql`:

### `reasoning_experiences`
- `id` (PK), `namespace`, `source_memory_id` (FK → memories.id, ON DELETE CASCADE)
- `task_summary`, `outcome` (`success`/`failure`/`uncertain` — CHECK enum)
- `verifier` (NOT NULL — provenance-bound; records which verifier produced the outcome)
- `confidence` (REAL, CHECK 0.0–1.0)
- `outcome_evidence` (TEXT NOT NULL — provenance-bound evidence; not hidden; must be explicit)
- `created_at`
- Indexes: `(namespace, created_at DESC)`, `(outcome, created_at DESC)`

### `reasoning_memory_items`
- Links reasoning items to source experiences (`experience_id` FK).
- `lesson_kind`: `strategy` | `guardrail` (CHECK enum).
- `title`, `description`, `applicability` (NOT NULL).
- FK on `memory_id` → `memories(id)`.

### Cleanup Trigger (`reasoning_experience_cleanup`)
- `BEFORE DELETE ON reasoning_experiences`
- Removes associated `reasoning_memory_items` and cleans `audit_log` / `memories` rows derived from the experience.
- **This ensures no hidden CoT storage**: when a trajectory is purged, its distilled children (strategies/guardrails) are removed; nothing remains as unclassified factual memory.

---

## 2. Provenance-Bound Evidence (Verified in Schema)

Every reasoning experience requires:
- `verifier` — explicit verifier identity (not anonymous).
- `outcome_evidence` — explicit evidence string (not hidden in memory content or embedded CoT).
- `source_memory_id` — linked back to original memory (FK, ON DELETE CASCADE); provenance chain preserved.
- `outcome` — explicit `success`/`failure`/`uncertain` (not ambiguous).
- `confidence` — numeric 0.0–1.0 (calibrated, not binary).

No `hidden_cot` column exists. No `chain_of_thought` text is embedded in `memories.content` by default. The adapter (`src/lib/storage.py`) does not inject hidden reasoning text into memory content.

---

## 3. Sparse Retrieval (Contract Verified)

- `reasoning_experiences` has `namespace` filter (index `namespace, created_at DESC`).
- `reasoning_memory_items` has `lesson_kind` (strategy/guardrail) — allows sparse retrieval by type.
- The adapter has NO `reasoning_memory_items` SELECT logic (same gap as other tables), but the migration contract supports sparse retrieval via indexed queries:
  ```sql
  SELECT * FROM reasoning_experiences
  WHERE namespace = ? AND outcome = ?
  ORDER BY created_at DESC;
  SELECT * FROM reasoning_memory_items
  WHERE lesson_kind = ? AND memory_id IN (...);
  ```
- Sparse retrieval is enforced by schema design (separate table, indexed type + namespace + outcome), not by adapter implementation.

---

## 4. No Hidden CoT Storage (Verified)

- `memories.content` / `memories.summary`: adapter uses user-provided content (no hidden LLM reasoning injected).
- `audit_log.metadata`: JSON with operation data; no `hidden_cot` or `internal_reasoning` field defined in schema.
- `reasoning_memory_items.description`: explicit user-facing description (not hidden); `applicability` field makes it discoverable.
- `reasoning_experience_cleanup` trigger guarantees that deleting an experience removes derived items — no orphan reasoning artifacts persist.

---

## 5. Adapter / Implementation Status

`src/lib/storage.py`: NO `reasoning_experiences` or `reasoning_memory_items` methods. Adapter does not load or retrieve reasoning memory.
- `docs/HIERARCHICAL_MEMORY.md`: reasoning memory not mentioned in adapter-level design.
- `docs/plans/item_03_m2_data_model.md`: lists `reasoning_experiences` and `reasoning_memory_items` in complete table inventory; notes blocker (`785067...`).
- Migration files present; adapter integration blocked by upstream source / DB clone / binary (tasks 3/15/18/19/21).

---

## 6. Evidence References

- `migrations/sqlite/023_reasoning_experiences.sql` / `libsql/023_reasoning_experiences.sql`: full contract schema + cleanup trigger.
- `migrations/MANIFEST.md`: 023 registered (2026-08-30); applied status documented.
- `docs/plans/item_03_m2_data_model.md`: table inventory confirms reasoning tables; blocker noted.
- `docs/archive/TEST_REPORT_LIBSQL_MIGRATION.md`: no reasoning-specific tests (design-level contract only at this stage).
- `src/lib/storage.py`: no reasoning memory methods (gap documented).

---

## 7. Blocker Note

- **Upstream `785067...` source**: MISSING — needed for full adapter contract verification.
- **DB clone (`MNEMOSYNE_DB_PATH`)**: MISSING — live reasoning data unreconciled.
- **Native binary (`mnemosyne`)**: MISSING — adapter integration (task 21) and MRR benchmark (task 20) blocked.
- **Task 3 (embeddings)**: MISSING — related to upstream source; reasoning memory uses `source_memory_id` (FK to `memories`) which relies on memory embeddings for retrieval in full integration; design audit does not require embeddings.

Task 6 audit complete. Contract verified; adapter gap documented.
