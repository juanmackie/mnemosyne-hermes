# Baseline Deploy Configuration (Planning Only)

Status: Design deliverable for item 2 (Python baseline capture). Not executed.
Upstream source reference (`mnemosyne-memory 3.15.1`): UNKNOWN (must be fetched).
Container digest / Hermes binary version: UNKNOWN (empty sha256 in `.auto/evidence/claim_001.json`).

Contracts preserved:
- Provider id: `mnemosyne-rust` (preserved during transition; future `mnemosyne` must preserve)
- Namespace: `agent:hermes`
- DB path: `MNEMOSYNE_DB_PATH` or `<hermes_home>/mnemosyne/mnemosyne.db`
- Tool names: `mnemosyne_memory_search`, `mnemosyne_memory_remember`
- Entry point: `mnemosyne-rust` (`hermes_agent.memory_providers`)

Excluded (must NOT be committed):
- `.env` or any file containing secrets (`ANTHROPIC_API_KEY`, `.age` passphrases, tokens)
- `mnemosyne.db` or any SQLite database
- `~/.config/mnemosyne/secrets.age`
- Python virtual environments (`venv/`, `.venv/`, `__pycache__/`, `.pytest_cache/`)
- Model caches (`.cache/torch/`, `.cache/huggingface/`)
- Private memory content (any `.jsonl` or `.db` with user memory)
- `shared/nous_auth.json` (confirmed absent; do NOT invent)

Patch set:
- `patches/001_link_type_fix.patch` (from `.auto/evidence/link_type_fix.patch`)
- Proposed patches: `patches/proposed/003_remove_minilm_override.diff`, `patches/proposed/003_set_bge_default.diff`
