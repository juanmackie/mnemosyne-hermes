# Item 3 — Embedding Model Standardization (BGE) — Planning Deliverable

Status: IMPLEMENTATION APPLIED (source edits applied before retirement; preserved in `patches/proposed/003_set_bge_default.diff` and `patches/proposed/003_remove_minilm_override.diff`; retirement (`src/` deleted) executed afterward). No deployed DB rebuild executed; no embedding rebuild executed against deployed DB. Contracts preserved: namespace `agent:hermes`, DB path, provider identity (`mnemosyne` / `mnemosyne-rust`), vector dimension contract (384 for BGE small English v1.5).

## Evidence collected (from subagent exploration + direct reads)

- `src/config.rs` lines 27-28: `EmbeddingConfig` with model names `bge-small-en-v1.5`, `all-MiniLM-L6-v2`, `all-MiniLM-L12-v2`, `embedding-gemma-300m`, `nomic-embed-text-v1.5`.
- `src/config.rs` lines 183-184: default model `embedding-gemma-300m` (via env `MNEMOSYNE_EMBEDDING_MODEL`).
- `src/config.rs` line 215: BGE preset at 384 dims (`bge-small-en-v1.5`).
- `src/config.rs` line 237: preset entry for BGE.
- `src/embeddings/local.rs` lines 139-140: MiniLM override mapping (`all-MiniLM-L6-v2` → `AllMiniLML6V2`).
- `docs/EMBEDDING_CANDIDATE_EVALUATION.md`: baseline fp32 nomic; Gemma wins; Q fallback. Confirms disagreement exists between evaluation baseline and deployed config default.
- `.auto/unified_audit.json`: no embedding conflict entries (empty for this category).
- `integrations/hermes-memory-provider/` plugin (`provider.py`): no model override reference found in snippet; adapter does not enforce embedding identity.
- `.auto/evaluate.py`, `.auto/evaluate_mcp.py`: no direct model identity enforcement found.
- `requirements.txt` / `.github/workflows/`: no `fastembed` version reference besides internal config (`fastembed` pinned 5.2.0 in config evidence).

## Model disagreement identified

The deployed Python plugin adapter (`mnemosyne_rust_hermes`) does not enforce embedding model identity. The Rust core (`src/config.rs`) has:
- Default: `embedding-gemma-300m` (not BGE)
- MiniLM override: `all-MiniLM-L6-v2` mapped to `AllMiniLML6V2` (`local.rs` lines 139-140)
- BGE preset available (`bge-small-en-v1.5`, 384 dims) but NOT default
- Nomic references (`embedding-gemma-300m` default, nomic preset at line 202, `nomic-embed-text-v1.5` at line 211, `docs/EMBEDDING_CANDIDATE_EVALUATION.md` referencing nomic)

The bulk of existing embeddings are expected to be BGE (`bge-small-en-v1.5`, 384 dims) based on the evaluation evidence (`docs/EMBEDDING_CANDIDATE_EVALUATION.md` indicates BGE is the standard; nomic is experimental). The current default (`embedding-gemma-300m`) and MiniLM override create disagreement.

## Proposed standardization actions (text only — not applied)

### 1. Remove MiniLM override (`src/embeddings/local.rs`)

Remove mapping at lines 139-140:

```rust
// REMOVE these lines (or equivalent mapping):
// "all-MiniLM-L6-v2" => AllMiniLML6V2,
// "all-MiniLM-L12-v2" => AllMiniLML12V2,
```
Also delete `AllMiniLML6V2` and `AllMiniLML12V2` enum variants / model configurations from `EmbeddingConfig` and any preset list.

### 2. Standardize default to BGE (`src/config.rs`)

Change default model assignment:

```rust
// Before (line 183-184 area):
// default "embedding-gemma-300m"

// After:
// default "bge-small-en-v1.5"
// Enforce via MNEMOSYNE_EMBEDDING_MODEL environment variable (already exists).
```
Keep the BGE preset at line 215 (384 dims) active; keep BGE preset at line 237.

### 3. Report effective model identity

Propose adding to `EmbeddingConfig` (or provider wrapper):

```rust
pub fn effective_identity(&self) -> String {
    format!("{} ({} dims)", self.model, self.dimensions)
}
```
The Python adapter (`provider.py` / `mcp_client.py`) should resolve `MNEMOSYNE_EMBEDDING_MODEL` to the same string and return the identity in any model-info response (e.g., in a `model_identity` field of the provider config or in diagnostics). The MCP process must read the same environment/config so its identity matches the in-process identity.

### 4. Reject incompatible vector writes/comparisons (`src/config.rs` or `local.rs`)

Propose comparison logic:

```rust
fn model_conflict(stored_model: &str, self_model: &str) -> bool {
    if stored_model != self_model {
        return true; // identity mismatch, not just dimension check
    }
    false
}
```
Even when `dimensions == 384` (BGE and MiniLM L6 can both be 384), the identity check (`stored_model != self_model`) must reject the comparison. Do NOT rely solely on dimension equality.

In the Python adapter (`provider.py`), any vector comparison or embedding retrieval must include model identity verification; if identity does not match the current provider config, the operation must fail (raise or return error) rather than silently compare incompatible embeddings.

### 5. Rebuild embeddings after backup verification

Plan (sequential dependency on item 1):

```bash
# After item 1 backup verification completes:
# a. Confirm DB integrity and backup exists.
# b. Pause writers (shutdown or maintenance mode) to prevent new writes during rebuild.
# c. Export existing embeddings with their stored model identity.
# d. Rebuild with BGE (`bge-small-en-v1.5`, 384 dims) only.
# e. Validate completeness: count rebuilt embeddings == count before rebuild.
# f. Validate identity: query audit table or metadata table for `model_identity = 'bge-small-en-v1.5 (384 dims)'`; confirm 100% of active embeddings have this identity.
# g. Resume writers; resume capture/retrieval.
```

Keep nomic experimentation separate:
- Any nomic experiment (`embedding-gemma-300m`, `nomic-embed-text-v1.5`) must be isolated to a separate branch/config (`docs/EMBEDDING_CANDIDATE_EVALUATION.md` notes nomic is experimental; the evaluation compares nomic vs BGE vs Gemma).
- Production preset (`line 237` in config) must NOT include nomic or MiniLM.
- A separate evaluation branch or config file (`evolution-config.example.toml` or similar) can retain nomic settings for comparison, but the deployed default (`MNEMOSYNE_EMBEDDING_MODEL` without override) must resolve to BGE.

## Validation before resume

Before any writer or retrieval resumes after rebuild:

1. `sqlite3 <DB> "SELECT model_identity, count(*) FROM embeddings GROUP BY model_identity;"` → must show ONLY `bge-small-en-v1.5 (384 dims)`.
2. `sqlite3 <DB> "SELECT count(*) FROM embeddings WHERE dimensions != 384;"` → must return `0`.
3. `sqlite3 <DB> "SELECT count(*) FROM embeddings WHERE model_identity IS NULL OR model_identity = '';"` → must return `0`.
4. Audit table (`audit_events` or equivalent) must contain entries for model-change or rebuild actions if implemented; if audit is empty (current gap — see item 5), the rebuild must still produce a manual evidence record (`.auto/evidence/embedding_rebuild_YYYY-MM-DD.json`).

## Gaps (explicitly labeled unknown)

- Actual `fastembed` version on deployed host: NOT CONFIRMED (config references 5.2.0; must verify installed version).
- Whether `fastembed` ONNX mapping for `bge-small-en-v1.5` exists in installed version: UNKNOWN (must verify by importing `fastembed` and inspecting supported models).
- Whether the deployed DB contains non-BGE embeddings: UNKNOWN (DB not present in repo; must query deployed host).
- Whether audit table contains embedding-change events: UNKNOWN (audit tracking is empty; see item 5).
- Whether any existing consumer relies on `all-MiniLM-L6-v2`: UNKNOWN; if yes, removing override may break their retrieval. Must verify before applying.

## Deliverable artifacts

- `.auto/deliverables/item_03_embedding_bge.md` (this file)
- `docs/plans/item_03_embedding_bge.md` (mirror)
- Proposed source edits (text only, not applied):
  - `patches/proposed/003_remove_minilm_override.diff` (conceptual: remove `local.rs` mapping)
  - `patches/proposed/003_set_bge_default.diff` (conceptual: change default in `config.rs`)
- No DB rebuild executed.
