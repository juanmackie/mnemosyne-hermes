# Task 10 — Project Bootstrap Assembly Audit

**Contract**: Token-budgeted assembly (`budget-tokens`, `min-confidence`, `project`, `task`, `agent`, `capability`); `mnemosyne.bootstrap` / `mnemosyne_bootstrap` MCP tool available; reads only approved constraints/facts/guardrails/policies/skills/provenance/abstentions; never writes.
**Evidence**: `docs/BOOTSTRAP.md`, `docs/HERMES_INTEGRATION.md`, adapter (`src/lib/storage.py`), user spec.

---

## 1. Contract (Strong Documentation)

`docs/BOOTSTRAP.md` defines:

- CLI: `mnemosyne bootstrap --project ... --task ... --agent hermes --capability ... --budget-tokens 3500 --min-confidence 0.5`
- MCP tool: `mnemosyne.bootstrap` (alias `mnemosyne_bootstrap` per HERMES_INTEGRATION.md).
- Read-only: never writes; not a second memory store.
- Response versioned: `bootstrap.v1`; separate channels:
  - `constraints`: active constraint/constitution memories (`constraint_status:approved` required).
  - `facts`: confident project knowledge (excluding constraints, reasoning lessons).
  - `guardrails`: failure-derived reasoning lessons (returned as fallible guidance, not facts).
  - `policies`: eligible interaction policies.
  - `skills`: relevant project-local skill metadata (relative path reported, content not copied into output).
  - `abstentions`: reasons a channel was empty or budget-limited.
- Every item includes: source ID, namespace, confidence, provenance IDs when available.
- Ordering deterministic; approximate token usage never exceeds `budget_tokens`.
- Constraint lifecycle (`mnemosyne constraint propose / list / approve / reject / supersede / export`) ensures only approved, unexpired proposals enter bootstrap; Markdown export is projection only (not second source of truth).
- Safety contract: read-only; no arbitrary file reads outside project root.

---

## 2. Adapter / Implementation Status

`src/lib/storage.py`: NO `bootstrap`, `mnemosyne_bootstrap`, `budget_tokens`, `min-confidence`, or `constraints`/`guardrails` selection logic.
- `docs/BOOTSTRAP.md` references `mnemosyne.bootstrap` CLI and MCP tool; adapter (`mnemosyne_client.py`) does not implement `bootstrap()`.
- Constraint proposal lifecycle (`constraint_*` commands) not implemented in adapter.
- No `constraint_status:approved` filter logic in adapter.
- Token budget enforcement (`budget-tokens`) not implemented.
- `abstentions` reporting channel not implemented.

---

## 3. Migration / Schema Support

- `constraints`: `memory_type` enum includes `constraint` (001_initial_schema.sql); `constraint_proposals` (024), `interaction_policy_proposals` (022) migrations exist.
- `facts`: `memories` table covers facts (`memory_type` includes `insight`, `feature_spec`, `implementation_plan`, etc.).
- `guardrails`: `reasoning_experiences` + `reasoning_memory_items` (`023_reasoning_experiences.sql`) cover strategy/guardrail items (task 6); adapter does not load them for bootstrap.
- `policies`: `interaction_policies` / `interaction_policy_proposals` / `interaction_policy_evidence` (022 migration) — adapter does not query them.
- `skills`: no dedicated skills table; `skills` could reference `memories.memory_type='feature_spec'` or external files; adapter does not load.
- `provenance` / `abstentions`: `audit_log` supports provenance tracking; `abstentions` is a computed response channel, not a DB table.

---

## 4. Contract Audit (Verified / Gaps)

| Requirement | Evidence | Status |
|---|---|---|
| Token-budgeted (`budget-tokens`) | Contract defined; adapter missing | ❌ Adapter missing |
| `mnemosyne.bootstrap` / `mnemosyne_bootstrap` | Contract preserved; adapter/CLI missing | ❌ Adapter missing |
| Read-only (no writes) | Contract preserved; adapter has NO write path for bootstrap | ✅ Contract preserved |
| `constraints` (approved only) | Schema supports (`constraint` memory_type + `constraint_proposals` table + `constraint_status:approved` filter defined); adapter missing filter | ⚠️ Schema supports; adapter missing |
| `facts` (confident knowledge) | `memories` covers facts; adapter missing selection logic | ⚠️ Schema supports; adapter missing |
| `guardrails` (failure lessons) | `reasoning_experiences` + `reasoning_memory_items` (023) cover guardrails; adapter missing load logic | ⚠️ Schema supports; adapter missing |
| `policies` (eligible interaction policies) | `interaction_policies` (022) exists; adapter missing query | ⚠️ Schema supports; adapter missing |
| `skills` (relative path, not copied) | No dedicated skills table; contract references file paths; adapter missing load logic | ⚠️ Design contract preserved |
| `provenance` (source ID, namespace, confidence) | `audit_log` + `memories.id` + `memories.namespace` + `memories.importance` / `confidence` (if added) provide provenance; adapter missing assembly | ⚠️ Schema supports; adapter missing |
| `abstentions` (budget-limited / empty reasons) | Not a DB table; computed response; adapter missing assembly logic | ❌ Adapter missing |
| Constraint lifecycle (`propose` / `approve` / `reject` / `supersede` / `export`) | `docs/BOOTSTRAP.md` defines CLI; adapter missing; `constraint_proposals` (024) table exists | ⚠️ Schema supports; CLI/adapter missing |

---

## 5. Blocker Note

Blocked by upstream `785067...` (bootstrap implementation reference from upstream unavailable), DB clone MISSING, native binary MISSING. Design contract (`docs/BOOTSTRAP.md`) verified; adapter/CLI implementation gaps documented for repair when upstream/source/binary available.
