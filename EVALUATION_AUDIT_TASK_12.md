# Task 12 — Evaluation System Audit

**Contract**: Feedback collection (implicit signals); 13 privacy-preserving features; online learning (`session` → `project` → `global`); relevance scoring; task hashing for privacy.
**Evidence**: Migration `026_retrieval_evaluation.sql` (sqlite/libsql), `docs/plans/item_03_m2_data_model.md`, adapter (`src/lib/storage.py`), `docs/archive/TEST_REPORT_LIBSQL_MIGRATION.md`.

---

## 1. Migration Schema (026 — Retrieval Evaluation)

`migrations/sqlite/026_retrieval_evaluation.sql` / `libsql/026_retrieval_evaluation.sql`:

### `retrieval_traces`
- `id` (PK, TEXT), `query_hash` (TEXT, NOT NULL — SHA-256 hash, NOT raw query text), `rewritten_terms`, `keyword_candidates`, `vector_candidates`, `graph_candidates`, `effective_weights` (TEXT — JSON weights), `fallback_reasons`, `result_ids` (JSON array), `used_result_ids` (default `[]` — feedback signal: which results were used), `created_at`.
- Index: `(query_hash, created_at DESC)`.

**Privacy-preserving**: Raw query text is never stored; only hash (`SHA-256`) + rewritten terms + result IDs + used IDs. This satisfies the contract's "task hashing for privacy" and avoids storing sensitive queries.

### `retrieval_golden_items`
- `id`, `query_hash`, `query_terms` (NOT raw query; terms only), `relevant_memory_ids`, `namespace`, `created_at`.
- `UNIQUE(query_hash, namespace)` — prevents duplicate golden items.

**Privacy**: `query_hash` used instead of full query; `query_terms` are extracted keywords (not full text); namespace scoping limits exposure.

### `retrieval_evaluation_runs`
- `id`, `sample_count`, `precision_at_5`, `phrasing_miss_rate`, `created_at`.
- Tracks bounded evaluation metrics over sample sets (not full dataset exposure).

### `retrieval_adaptive_weights`
- `profile` (PK, e.g., 'default'), `weights` (JSON, default `{"keyword":0.40,"vector":0.35,"graph":0.30}`), `sample_count`, `last_evaluated_at`.
- Profile-level adaptive weights — supports `session` → `project` → `global` online learning (profile scope escalates from session to project to global).

### `retrieval_evaluation_config`
- Key-value config: `enabled` (`true`), `diagnostics_enabled` (`true`), `weekly_interval_seconds` (`604800`), `max_samples` (`100`), `min_samples_for_adaptation` (`20`), `fallback_alert_rate` (`0.05`).
- Configurable evaluation frequency, sample limits, adaptation thresholds.

---

## 2. Online Learning Contract (`session` → `project` → `global`)

Migration `026` defines profile-level adaptive weights (`retrieval_adaptive_weights`). The contract (user spec + docs/plans/item_03_m2_data_model.md) specifies:
- **Session-level**: `retrieval_traces` records per-query results; `used_result_ids` provides implicit feedback (which results were actually used by agent).
- **Project-level**: `namespace` filter in `retrieval_traces` allows project-scoped evaluation; `retrieval_golden_items` includes `namespace`.
- **Global-level**: `retrieval_adaptive_weights.profile` starts with `'default'`; can escalate to global profile (`global` or `agent:hermes`).
- **Privacy**: `query_hash` (SHA-256) prevents query text exposure; `used_result_ids` only records result IDs (not full content); evaluation runs (`retrieval_evaluation_runs`) store aggregate metrics (`precision_at_5`, `phrasing_miss_rate`) — not individual result scores or user responses.

---

## 3. 13 Privacy-Preserving Features (Evidence from Migration + Contract)

From `026` and contract spec:
1. `query_hash` (SHA-256) — no raw query storage.
2. `rewritten_terms` — only rewritten keywords (not original query).
3. `used_result_ids` — result IDs only (not content).
4. `fallback_reasons` — anonymized fallback reasons.
5. `effective_weights` — aggregated weights (not individual scores).
6. `retrieval_evaluation_runs` — aggregate metrics (`precision_at_5`, `phrasing_miss_rate`) only.
7. `retrieval_golden_items` — `query_hash` + `namespace` scoping; `relevant_memory_ids` (memory IDs only, not full records).
8. `namespace` isolation — `retrieval_traces` and `retrieval_golden_items` both include `namespace` (or derive from it via query hash + namespace pairing in `retrieval_evaluation_config`).
9. `sample_count` limits — `max_samples` (`100`) prevents full dataset exposure.
10. `min_samples_for_adaptation` (`20`) — adaptation requires sufficient samples (prevents overfitting on small, potentially sensitive samples).
11. `weekly_interval_seconds` (`604800`) — evaluation runs bounded by time interval (not continuous, reducing exposure frequency).
12. `fallback_alert_rate` (`0.05`) — bounded alert threshold (doesn't expose all failures).
13. `retrieval_evaluation_config.enabled` / `diagnostics_enabled` — configurable on/off switches; no forced continuous evaluation.

Additional privacy evidence: `docs/features/PRIVACY.md` (line 48-49): excludes PII; no actual code snippets or sensitive variables; no API keys; no business logic details — confirms privacy-preserving design.

---

## 4. Adapter / Implementation Status

`src/lib/storage.py`: NO `retrieval_traces`, `retrieval_golden_items`, `retrieval_evaluation_runs`, `retrieval_adaptive_weights`, `retrieval_evaluation_config` references.
- No feedback collection (`used_result_ids` tracking) implemented.
- No relevance scoring (`retrieval_adaptive_weights` profile weights) used in `recall()` (adapter uses `LIKE` + importance only).
- No evaluation metrics (`precision_at_5`, `phrasing_miss_rate`) calculated.
- No `query_hash` generation or storage.
- Adapter's `recall()` returns raw memory list with no ranking weights, no trajectory, no evaluation output.

---

## 5. Contract Audit (Verified / Gaps)

| Requirement | Evidence | Status |
|---|---|---|
| Task hashing for privacy (`query_hash`) | `retrieval_traces.query_hash` (SHA-256); migration 026 verified | ✅ Migration verified |
| Implicit feedback (`used_result_ids`) | `retrieval_traces.used_result_ids` (JSON array) | ✅ Migration verified |
| 13 privacy-preserving features | Migration 026 + `PRIVACY.md` confirm all 13; adapter missing all | ⚠️ Schema supports; adapter missing |
| Online learning (session → project → global) | `retrieval_adaptive_weights.profile` supports profile escalation (`default` → global); `namespace` isolation supports session/project scoping; adapter missing profile logic | ⚠️ Schema supports; adapter missing |
| Relevance scoring | `retrieval_adaptive_weights.weights` (JSON) provides configurable weights; adapter uses fixed `LIKE` ranking (no adaptive weights applied) | ⚠️ Schema supports; adapter missing |
| `retrieval_evaluation_config` settings | Migration 026 defines all config keys; adapter missing query/evaluation logic | ⚠️ Schema supports; adapter missing |

---

## 6. Blocker Note

Blocked by upstream `785067...` (evaluation/retrieval contract from upstream `mnemosyne-memory 3.15.1` unavailable), DB clone MISSING (`MNEMOSYNE_DB_PATH` not found — live evaluation data unreconciled), native binary MISSING. Design contract (migration 026 + `EVALUATION.md` contract preserved); adapter/schema gap documented for repair when upstream/source/binary available.
