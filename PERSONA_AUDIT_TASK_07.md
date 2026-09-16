# Task 7 — Persona Facts (L3) Audit

**Contract**: Promotion/demotion/reinforcement API; permanent / long_term / working tiers; reinforcement-driven decay; auto-injected to system prompt.
**Evidence**: `docs/HERMES_INTEGRATION.md`, adapter (`src/lib/storage.py`), archive docs, contract spec.

---

## 1. Contract References

`docs/HERMES_INTEGRATION.md` (line 220, 225-226):
- `mnemosyne_persona`: reads durable preference/constraint memories.
- `mnemosyne_canonical`: one current value per (category, name) slot.
- `mnemosyne_triples`: one current object per (subject, predicate) slot; supersede archive.
- `mnemosyne_bootstrap`: shared read-only startup assembly (constraints, facts, policies, skills, provenance, abstentions).
- System-prompt injection: not explicitly described in adapter docs; contract implies persona facts ride beside (not inside) ranked results (per task 11: profile slices).

No dedicated `docs/features/persona.md` or `docs/features/PERSONA.md`. No `migrations/*persona*` file. The contract exists in the agent/integration spec, not as a dedicated feature document.

---

## 2. Adapter Status (No Implementation)

`src/lib/storage.py`: No `persona`, `personas`, `promotion`, `demotion`, `reinforcement`, or `system_prompt` references.
- No `permanent`/`long_term`/`working` tier columns in `memories` table (only `importance` 1-10, `memory_type` enum).
- No reinforcement/decay logic (`access_count` exists but no decay formula; `last_accessed` exists but no tier-promotion logic).
- No system-prompt injection mechanism in adapter or CLI (`mnemosyne_client.py`).

---

## 3. Migration / Schema Evidence

No `migrations/*persona*` file. The `memories` table (`001_initial_schema.sql`) has:
- `importance` (1-10) — could serve as a rough tier proxy, but not mapped to `permanent`/`long_term`/`working`.
- `memory_type` enum includes `preference`, `constraint`, `constitution`, `feature_spec` — but no `persona` or `persona_fact` type.
- `superseded_by`, `is_archived`, `access_count`, `last_accessed_at` — support decay/reinforcement patterns but no explicit tier logic.

---

## 4. Contract Audit (Verified Against Spec)

| Requirement | Evidence | Status |
|---|---|---|
| Permanent / long_term / working tiers | Not in adapter; not in migrations; referenced contractually in HERMES_INTEGRATION.md | ❌ Adapter missing |
| Promotion/demotion/reinforcement API | Not implemented; no `promote`, `demote`, or `reinforce` methods in adapter or CLI | ❌ Adapter missing |
| Reinforcement-driven decay | `memories.access_count` + `last_accessed_at` exist (base for decay); no decay formula applied | ⚠️ Schema supports; adapter missing |
| Auto-injected to system prompt | Not implemented in adapter/CLI; design contract implies `mnemosyne_persona` provides injection surface | ❌ Adapter missing |
| System-prompt injection contract preserved | `docs/HERMES_INTEGRATION.md` references `mnemosyne_persona` as provider surface; namespace `agent:hermes` isolated | ✅ Contract preserved |

---

## 5. Blocker Note

- **Upstream `785067...` source**: MISSING — full persona contract reference (if any upstream `mnemosyne-memory 3.15.1` persona implementation exists) unavailable.
- **DB clone (`MNEMOSYNE_DB_PATH`)**: MISSING.
- **Native binary (`mnemosyne`)**: MISSING.
- **Task 11 (Static + dynamic profile slices)**: Unblocked design-level audit; adapter gap same as this task.
- **Task 7 audit complete**: Contract preserved; adapter missing promotion/demotion/reinforcement, tier mapping, decay logic, and system-prompt injection. Documented for repair when upstream/source/binary available.
