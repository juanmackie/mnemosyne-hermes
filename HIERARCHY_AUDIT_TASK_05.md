# Task 5 — Hierarchical Memory (L0/L1/L2 + Directory-Recursive Retrieval)

**Contract**: Token-budgeted context assembly + retrieval trajectories; score propagation documented.
**Evidence**: Inspect `docs/HIERARCHICAL_MEMORY.md`, adapter (`src/lib/storage.py`), archive docs.

---

## 1. Contract Documentation (Strong)

`docs/HIERARCHICAL_MEMORY.md` defines:

- **L0 Abstract**: ≤256 chars — quick relevance checks, launcher context.
- **L1 Overview**: ≤4000 chars — reranking, navigation, planning.
- **L2 Detail**: full content — on-demand via `mnemosyne.context`.
- **Topic paths**: `project:myapp/decisions/caching` (namespace + type + first tag).
- **Directory aggregation**: stable sampling (max 32 direct children, deterministic); directories carry L0/L1 sidecars bottom-up.
- **Freshness metadata**: `total_entries`, `sampled_entries`, `unsampled_entries`, `pending_child_changes`.
- **Hierarchical retrieval algorithm**:
  1. Global search: roots ranked by best descendant score; top-k seed priority queue.
  2. Recursion: pop best directory, score children, `final = α·own + (1−α)·parent_score` (α configurable, default 1.0).
  3. Convergence: stop when top-k unchanged for 3 rounds.
  4. Trajectory: `RetrievalTrajectory` records each step (`global_search` / `recurse` / `collect`).
- **Token-budgeted context assembler** (`src/context_assembler.rs`): breadth-first entry, depth fallback on leftover budget, oversize tiers fall back shallower, ledger reports budget use.
- **CLI**: `mnemosyne recall --hierarchical --trace --budget-tokens 2000 --format json` (gains `trajectory` + `assembled_context` fields).
- **MCP tools**: `mnemosyne.recall` (hierarchical + trajectory), `mnemosyne.used` (feedback), `mnemosyne.hierarchy` (browse tree without loading full content).

---

## 2. Adapter Implementation (Absent)

`src/lib/storage.py`: No `L0`, `L1`, `L2`, `hierarchical`, `retrieval_trajectory`, `token_budget`, or `context_assembler` references.
- `recall()` (line ~76): flat `LIKE` query, no directory recursion.
- `graph()` (line 492): namespace-level grouping only.
- No `mnemosyne.hierarchy` CLI command.
- No `RetrievalTrajectory` serialization.
- No `token_budget` parameter.
- `docs/HIERARCHICAL_MEMORY.md` references `src/hierarchy.rs`, `src/intention.rs`, `src/context_assembler.rs`, `src/session_extract.rs`, `benchmark/retrieval/locomo_eval.py` — none of these files exist in `src/lib/`.

---

## 3. Evidence References

- `docs/HIERARCHICAL_MEMORY.md`: complete contract specification.
- `docs/ARCHITECTURE.md`: references hierarchical memory design.
- `docs/features/`: design docs for ICS, semantic highlighting, etc.
- `migrations/sqlite/001_initial_schema.sql`: `memories` table supports `summary` (L1-like) and `content` (L2), but no separate L0 abstract column or tiered views.
- `migrations/sqlite/025_session_transcripts.sql`: durable transcripts (L2-level session content) defined but adapter does not load them hierarchically.
- `docs/plans/item_03_m2_data_model.md`: mentions `retrieval_traces` (evaluation) but not hierarchical retrieval.
- Archive (`docs/archive/IMPLEMENTATION_COMPLETE.md`, `TEST_REPORT_LIBSQL_MIGRATION.md`): no hierarchical retrieval benchmark results.

---

## 4. Score Propagation (Documented, Not Implemented)

The contract requires `final = α·own + (1−α)·parent_score`. No adapter code calculates or propagates parent scores to children. The adapter's `graph()` returns only flat nodes with namespace grouping.

---

## 5. Verification Contract (Task 5)

- [x] `docs/HIERARCHICAL_MEMORY.md` read fully; L0/L1/L2 limits (≤256, ≤4000, full), topic paths, directory aggregation, fresh metadata, retrieval algorithm (global → recurse → converge → trajectory), token-budgeted assembler, CLI flags, MCP tool contracts all documented.
- [x] Adapter (`src/lib/storage.py`) inspected: NO hierarchical retrieval, NO trajectory, NO token budget, NO `L0`/`L1` tier methods.
- [x] Migration files (`001_initial_schema.sql`) provide base `memories` (content/summary) but no tiered schema.
- [x] Blocker noted: upstream `785067...` needed for upstream `mnemosyne-memory 3.15.1` hierarchical implementation reference; DB clone missing; native binary missing. None prevent design audit.

**Result**: Contract fully documented; adapter implementation missing. Audit complete.
