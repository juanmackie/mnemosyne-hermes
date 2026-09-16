# Task 11 — Static + Dynamic Profile Slices Audit

**Contract**: Standing profile (`identity`/`preferences`) + recency-ordered active work slice; rides beside ranked results, not inside.
**Evidence**: `docs/AGENT_SETUP.md`, user spec, adapter, docs/plans/item_03_m2_data_model.md.

---

## 1. Contract Evidence

`docs/AGENT_SETUP.md` (line 55-61):
- Always-on profile slice (`independent of matching`).
- `mnemosyne remember "..." --namespace "profile:identity" --importance 9` — profile memory stored with high importance.
- `mnemosyne recall --query ... --limit 5` — recall result carries profile / profile-dynamic block beside results.
- Standing context, independent of keyword hit.

`docs/plans/item_03_m2_data_model.md` (line 14): `memory_modifications` tracked for profile-related updates (though ghost 011 unrecoverable).

User spec (task 11): profile slices ride beside ranked results (not inside); recency-ordered active slice.

---

## 2. Adapter Status (No Implementation)

`src/lib/storage.py`: No `profile`, `profile_dynamic`, `standing_profile`, or `profile_slice` references.
- No `namespace == 'profile:identity'` filter for profile retrieval.
- No separate profile assembly logic (besides ranked results).
- The adapter's `recall()` returns flat `memories` list; no `profile` or `profile_dynamic` block appended.

---

## 3. Schema Evidence

- `profile:identity` namespace: `memories.namespace` (TEXT) supports this; adapter doesn't filter by it for profile retrieval.
- `memory_modification_log`: ghost 011 unrecoverable — can't verify profile-modification audit.
- `work_items` (ghost 012): extra `requirements`, `requirement_status`, `implementation_evidence` columns reference active work state; unrecoverable.
- `retrieval_traces` (026): retrieval evaluation could measure profile impact; adapter doesn't use it.

---

## 4. Contract Audit (Verified / Gaps)

| Requirement | Evidence | Status |
|---|---|---|
| Standing profile (identity, preferences) | `docs/AGENT_SETUP.md` confirms contract; adapter missing profile assembly | ⚠️ Contract verified; adapter missing |
| Recency-ordered active work slice | Not in adapter; `list_memories(sort_by='recent')` could serve as proxy but no profile-dynamic block | ❌ Adapter missing |
| Rides beside ranked results (not inside) | Contract preserved (AGENT_SETUP.md); adapter returns flat list without profile block | ⚠️ Contract preserved; adapter missing |
| Profile namespace (`profile:identity`) | Schema supports (`namespace`); adapter doesn't filter/select for profile | ⚠️ Schema supports; adapter missing |

---

## 5. Blocker Note

Blocked by upstream `785067...` (profile/retrieval contract from upstream unavailable), DB clone MISSING, native binary MISSING. Design contract (`AGENT_SETUP.md`) verified; adapter gap documented.
