# Item 2 — Capture Deployed Python Baseline (Planning Deliverable)

Status: PLANNING ONLY. No repository commits to main beyond documentation/reports. No deployed system rebuilt. Contracts preserved: `memory.provider: mnemosyne` (current adapter uses `mnemosyne-rust`; transition to Python `mnemosyne` requires preserving namespace `agent:hermes`, DB path, tool names, and identifiers).

## Plugin evidence (read from integrations/hermes-memory-provider/)

Files present:
- `README.md` (contract description, entry point `mnemosyne-rust`, environment table)
- `pyproject.toml` (project `mnemosyne-rust`, version `0.1.0`, requires-python `>=3.11`, build-system `maturin` with `pyo3` bindings, optional `python` feature pointing to `src`/`orchestration`)
- `mnemosyne_rust_hermes/__init__.py`
- `mnemosyne_rust_hermes/config.py`
- `mnemosyne_rust_hermes/contexts.py`
- `mnemosyne_rust_hermes/mcp_client.py`
- `mnemosyne_rust_hermes/provider.py` (MemoryProvider adapter; contract details preserved)
- `mnemosyne_rust_hermes/worker.py`
- `mnemosyne_rust_hermes/py.typed`
- `tests/test_provider.py`

Key contract lines (from `provider.py` snippet):
- Provider id: `mnemosyne-rust` (`PROVIDER_ID_DEFAULT`)
- Namespace: `agent:hermes` (`NAMESPACE_DEFAULT`)
- Checkpoint API version: 2 (`CHECKPOINT_API_VERSION`)
- Prefetch tool: `mnemosyne_prefetch` (`PREFETCH_TOOL`)
- Sync turn tool: `mnemosyne_sync_turn` (`SYNC_TURN_TOOL`)
- DB path: resolved from `MNEMOSYNE_DB_PATH` or `<hermes_home>/mnemosyne/mnemosyne.db`
- Context skip list: `cron`, `flush`, `subagent`, `background`, `skill_loop` (plus `primary` handled by Hermes wrapper)

## Version and dependency evidence

- `pyproject.toml`: no reference to `mnemosyne-memory 3.15.1`. Only `setuptools>=61` for build, `maturin>=1.0,<2.0` for PyO3.
- `.github/workflows/ci.yml`: no Python package version pinning for `mnemosyne-memory`.
- `requirements.txt` (repo root): `claude-agent-sdk>=0.1.0`, `asyncio>=3.4.3`, `rich>=13.0.0`. No `mnemosyne-memory`.
- `.auto/evidence/claim_001.json`: empty sha256 (`sha256: ` blank). No container digest, no Hermes binary version hash.
- `docs/HERMES_INTEGRATION.md` and `docs/ARCHITECTURE.md`: describe Rust↔Python bridge (`ClaudeAgentBridge`), but do not specify `mnemosyne-memory 3.15.1`.
- `.mnemosyne_notes`: references `mnemosyne` binary build from 2026-09-07 (`ba6fe984`); no version tag `3.15.1`.

## Patch set evidence

- Only patch file found: `.auto/evidence/link_type_fix.patch` (contents: link_type enum fix for libsql/sqlite schema). Read below for proposal.
- No `patches/` directory exists.
- No upstream `mnemosyne-memory` source reference found in repo (URL placeholder needed).

## Proposed reproducible patch-set structure

```
baseline/
  upstream/
    mnemosyne-memory-3.15.1/README.md   # upstream reference (URL: https://github.com/.../mnemosyne-memory; must be fetched)
    mnemosyne-memory-3.15.1/src/          # upstream pinned source (to be downloaded)
  patches/
    001_link_type_fix.diff              # .auto/evidence/link_type_fix.patch converted to unified diff
    002_python_plugin_adapter.md        # adapter contract notes (this file)
  deploy/
    sanitized_example/
      .env.example                        # MNEMOSYNE_BIN, MNEMOSYNE_DB_PATH, namespace, provider id
      provider.json.example               # config file example (no secrets)
      README_SANITIZED.md                 # sanitized install/test instructions
  scripts/
    verify_baseline_install.sh          # clean-install verification script (see below)
```

Note: The `mnemosyne-memory 3.15.1` upstream source is MISSING from the repository. It must be fetched from the upstream package registry (PyPI or source repository) before a reproducible patch set can be finalized. This is explicitly labeled `unknown` until fetched.

## Clean-install verification plan

```bash
#!/bin/bash
# verify_baseline_install.sh — proposed script (not executed on deployed host)
set -euo pipefail

# 1. Ensure binary on PATH (for adapter tests that call real stdio MCP)
export MNEMOSYNE_BIN="${MNEMOSYNE_BIN:-mnemosyne}"

# 2. Install plugin in isolated environment
python -m venv /tmp/baseline_venv
source /tmp/baseline_venv/bin/activate
python -m pip install --upgrade pip
python -m pip install integrations/hermes-memory-provider/

# 3. Verify entry-point registered
python -c "
import importlib.metadata
ep = importlib.metadata.entry_points()
print('Memory providers:', [e.name for e in ep.select(group='hermes_agent.memory_providers')])
"

# 4. Run plugin tests (no real DB required for basic contract tests)
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v
# Expected: exit 0; test names include provider initialization, config parsing, context filtering.

# 5. Verify adapter contracts preserved
python -c "
from mnemosyne_rust_hermes.config import default_config
cfg = default_config()
assert cfg.namespace == 'agent:hermes', 'namespace contract broken'
assert cfg.provider_id == 'mnemosyne-rust', 'provider id contract broken'
print('Contracts OK')
"

# 6. Report results (do NOT include DB contents, secrets, or model caches)
echo "Verification complete. Exit code: $?"
```

This script is a PLANNING ONLY proposal. It has NOT been executed against a deployed host. The deployed binary (`mnemosyne` or `mnemosyne-rust`) must be verified separately before this test can pass.

## Sanitized deployment example (what to include / exclude)

Include:
- `pyproject.toml` (pinned versions, build-system, optional python feature)
- `README_SANITIZED.md` (contract, environment variables, entry point name)
- `provider.json.example` (config structure without real DB path or binary absolute path)
- `.env.example` (variable names only, no values containing keys, paths to secrets, or tokens)
- `patches/001_link_type_fix.diff` (reproducible patch text)
- `scripts/verify_baseline_install.sh` (test command, not executed output with secrets)

Exclude (explicitly):
- `.env` or any file containing `ANTHROPIC_API_KEY`, secret tokens, or `.age` passphrases
- `mnemosyne.db` or any SQLite database
- `.age` secret files (`~/.config/mnemosyne/secrets.age`)
- Python virtual environments (`venv/`, `.venv/`, `__pycache__/`, `.pytest_cache/`)
- Model caches (`.cache/torch/`, `.cache/huggingface/`, `.cache/sentence_transformers/`)
- Private memory content (any `.jsonl` or `.db` containing user memory)
- `shared/nous_auth.json` (confirmed absent; do not invent)

## Hermes version / container digest

Evidence: `docs/ARCHITECTURE.md` references Python bridge architecture; `.github/workflows/ci.yml` defines CI; `.mnemosyne_notes` references build `ba6fe984` (2026-09-07). No container digest (`sha256:`) found in any file (`.auto/evidence/claim_001.json` is blank). Status: UNKNOWN — must be captured from deployed container registry or image pull command (`docker inspect` / `podman inspect`) before package can be finalized.

## Gaps (explicitly labeled unknown)

- `mnemosyne-memory 3.15.1` upstream source URL/reference: NOT FOUND in repo. Must be fetched externally.
- Container image digest / Hermes binary sha256: NO EVIDENCE in repo.
- Deployed binary version (`mnemosyne --version` output): NOT AVAILABLE in repo.
- Real DB file for integrity test: NOT FOUND in repo; must be located on deployed host.
- Patch conversion (`.patch` → `.diff`) completed in proposal but upstream context missing.

## Deliverable artifacts produced

- `.auto/deliverables/item_02_python_baseline.md` (this file)
- `docs/plans/item_02_python_baseline.md` (mirror)
- `patches/001_link_type_fix.diff` (proposed, not applied)
