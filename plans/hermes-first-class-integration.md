# Plan: Hermes Agent First-Class Integration — Review & Repair

Status: **Plan submitted for review. No code edits applied yet.** (Plannotator planning mode — no destructive/edit commands executed; only `plans/*.md` edited.)

## Context

Repo: `mnemosyne-hermes` (`C:/Users/juanm/Documents/GitHub/mnemosyne-hermes`). User asked to read `https://hermes-agent.nousresearch.com/docs` and review the repo to "ensure this will work seamlessly with hermes agent as a first class integration." The user selected:
- **Make code changes** (not audit-only)
- **All for production as the AI agent Hermes memory system** (provider contracts + namespace + skills + bot mode + adapter)
- **Plan with recommendations** (submit review plan first)

The repo is the canonical Hermes-first memory system but is currently broken for production use because the adapter source (`mnemosyne_rust_hermes/`) is missing, the binary digest is unknown (`sha256:` empty in `.auto/evidence/claim_001.json`), and the documentation has stale references (`mnemosyne-memory 3.15.1` upstream source missing per `docs/plans/item_02_python_baseline.md`).

## Evidence from Hermes Site Docs (fetched live)

- MCP stdio contract: Hermes expects `mcp_servers` block (`~/.hermes/config.yaml`), `command` + `args`, optional `env`, and supports `notifications/tools/list_changed` for runtime tool updates.
- Skills: compatible with `agentskills.io`; Hermes bundles `github/*` skills; skills are portable and shareable.
- Bot mode: named Bots with their own model, memory, skills, routines, chats.
- Memory provider contract (`mnemosyne-rust`): `memory.provider` id, namespace `agent:hermes`, DB at `MNEMOSYNE_DB_PATH`, tool aliases `mnemosyne_memory_search` / `mnemosyne_memory_remember`, persistent stdio session, fail-closed behavior (`on_pre_compress` raises `CheckpointError` rather than partial success).

These align with `docs/HERMES_INTEGRATION.md` and `integrations/hermes-memory-provider/README.md`, confirming the intended contract is correct.

## Evidence from Repo (Critical Gaps Found)

### 1. Adapter source is MISSING (`integrations/hermes-memory-provider/`)
Files present:
- `README.md` (contract description)
- `pyproject.toml` (entry point `mnemosyne-rust = mnemosyne_rust_hermes:register`; package `mnemosyne_rust_hermes`)
- `tests/test_provider.py` (references `mnemosyne_rust_hermes.config`, `.provider`, `.mcp_client`, `.contexts`)

Files MISSING (referenced in `docs/plans/item_02_python_baseline.md`, `.auto/`, `tests/`):
- `mnemosyne_rust_hermes/__init__.py`
- `mnemosyne_rust_hermes/config.py`
- `mnemosyne_rust_hermes/contexts.py`
- `mnemosyne_rust_hermes/mcp_client.py`
- `mnemosyne_rust_hermes/provider.py` (MemoryProvider adapter)
- `mnemosyne_rust_hermes/worker.py`
- `mnemosyne_rust_hermes/py.typed`

Without these files, the adapter package cannot be installed or run. The entry point (`hermes_agent.memory_providers`) has nothing to point to.

### 2. Provider identity is split / ambiguous
- `docs/plans/item_02_python_baseline.md` mentions transition: `memory.provider: mnemosyne` (current adapter uses `mnemosyne-rust`; future Python `mnemosyne`).
- `AGENTS.md`: contracts preserved: `memory.provider` (`mnemosyne-rust` / future `mnemosyne`).
- `README.md`: `mnemosyne-rust` provider with `agent:hermes` namespace.
- `docs/plans/item_07_deploy_repro.md`: provider identity `mnemosyne-rust` / `mnemosyne`.

For first-class production integration, the provider id must be unambiguous: `mnemosyne-rust` (the adapter id) is what Hermes imports. If the user intends `mnemosyne` as the final Python-only provider, the adapter must either register both or the config/docs must clearly distinguish them. Per user's answer ("all for production"), I recommend keeping `mnemosyne-rust` as the adapter id (matches Hermes docs and entry point) and adding a clear note that a future `mnemosyne` provider is planned.

### 3. Binary dependency unknown (no pinned digest)
- `.auto/evidence/claim_001.json`: `sha256:` empty.
- `docs/plans/item_07_deploy_repro.md`: no container digest found.
- `Makefile` and `scripts/rebuild-and-update-install.sh` manage the Rust binary (`mnemosyne`) build.

The adapter depends on `MNEMOSYNE_BIN` (default `mnemosyne`) being on PATH. For production use, either the binary must be built/released and pinned, or the adapter must include a graceful fallback (e.g., error message instructing user to run `scripts/rebuild-and-update-install.sh`).

### 4. Skills / bot mode / events not fully wired in adapter
- Hermes docs: skills (`agentskills.io`), bot mode (`~/.hermes/config.yaml`), `notifications/tools/list_changed`.
- `docs/HERMES_INTEGRATION.md`: describes install/config but does not mention skills metadata or bot mode config templates.
- `docs/MCP_CLIENT_CONFIGS.md`: exists but not fully read; may contain config templates.
- Adapter (`provider.py`) is missing; therefore skills integration (e.g., `get_tool_schemas()` returning skill-aware descriptions) and event handling (`notifications/tools/list_changed`) cannot be verified or implemented.

### 5. Checkpoint / audit repair deferred (per `docs/plans/item_05_audit_repair.md` / `item_07_deploy_repro.md`)
- `docs/plans/item_07_deploy_repro.md` lists smoke check 7 (`sqlite3 <DB> SELECT count(*) FROM audit_events`) requiring item 5 repair.
- Adapter contract (`on_pre_compress`) is fail-closed (good for Hermes), but audit table may be missing/mis-aligned with schema migrations (`migrations/libsql/` through `027_...sql`).

### 6. Python-only runtime pivot (archive reference)
- `README.md` notes Python-only quickstart (`python -m pip install .`), no `maturin`/`pyo3`.
- `docs/plans/item_02_python_baseline.md`: Python adapter (`mnemosyne-rust`) preserved initially; future Python `mnemosyne` requires contract preservation.
- `AGENTS.md`: Python adapter preserved initially; contracts preserved.

This means the adapter source (`mnemosyne_rust_hermes/`) is the bridge between the Python runtime and the Rust binary (`mnemosyne`). Without it, neither the Python adapter nor a pure-Python replacement (`mnemosyne`) can function.

## Approach (Recommended)

Given the user's instruction ("Plan with recommendations"), I will NOT apply edits in this turn. The recommended approach for first-class production integration is a three-phase repair:

**Phase A — Adapter Source Reconstruction (critical)**
Recreate `mnemosyne_rust_hermes/` source files based on preserved contracts from:
- `docs/plans/item_02_python_baseline.md` (contract details, file list)
- `integrations/hermes-memory-provider/README.md` (behavior contract, env vars)
- `tests/test_provider.py` (test expectations for provider initialization, config parsing, context filtering, MCP stdio, checkpoint)
- `.auto/` deliverables (provider id, namespace, checkpoint API version)

Files to create:
```
integrations/hermes-memory-provider/
  mnemosyne_rust_hermes/
    __init__.py
    config.py      (ProviderConfig, default_config, NAMESPACE_DEFAULT='agent:hermes', PROVIDER_ID_DEFAULT='mnemosyne-rust')
    contexts.py     (SKIP_CONTEXTS = ['cron','flush','subagent','background','skill_loop'])
    mcp_client.py   (StdioJsonRpcClient, McpDisconnected)
    provider.py     (MemoryProvider adapter: initialize, prefetch, sync_turn, shutdown, get_tool_schemas, handle_tool_call, on_pre_compress)
    worker.py       (Background capture worker, serialized turn recording)
    py.typed
```

**Phase B — Contract & Config Alignment**
- Ensure `MNEMOSYNE_PROVIDER_ID` defaults to `mnemosyne-rust` (matches Hermes docs and entry point).
- Ensure `MNEMOSYNE_NAMESPACE` defaults to `agent:hermes`.
- Ensure `MNEMOSYNE_DB_PATH` resolves correctly (`MNEMOSYNE_DB_PATH` or `<hermes_home>/mnemosyne/mnemosyne.db`).
- Ensure `MNEMOSYNE_BIN` is documented and the adapter produces a clear error if binary not found (fail-closed, not silent failure).
- Add skills compatibility note to adapter docs (skills portable; adapter doesn't block skills, but doesn't advertise skill-specific schemas yet — acceptable for production since skills are handled by Hermes bot mode, not adapter).
- Ensure event/broadcast compatibility: adapter uses `notifications/tools/list_changed` if supported by Hermes version (optional enhancement; adapter should work without it).

**Phase C — Verification & Documentation**
- Rebuild adapter package (`python -m pip install integrations/hermes-memory-provider/`).
- Verify entry point (`python -c "import importlib.metadata; ..."`).
- Verify contracts (`python -c "from mnemosyne_rust_hermes.config import default_config; ..."`).
- Run adapter tests (`python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v`).
- Verify MCP stdio handshake (`mnemosyne mcp` responds to `initialize` with correct server info).
- Document results in `docs/HERMES_INTEGRATION.md` (update install/config section with skills/bot mode notes if needed).

## Reuse / Existing Code

- `tests/test_provider.py`: contains the full test specification (fake MCP server + real stdio test). This is the specification for `provider.py` and `mcp_client.py`.
- `docs/plans/item_02_python_baseline.md`: contains the exact file list and contract details for adapter reconstruction.
- `.auto/evidence/claim_001.json`: contains provider id (`mnemosyne-rust`) and namespace evidence.
- `docs/HERMES_INTEGRATION.md`: contains install/config instructions to be updated after adapter reconstruction.
- `Makefile` / `scripts/rebuild-and-update-install.sh`: manage Rust binary build; adapter depends on this binary being available.
- `docs/plans/item_07_deploy_repro.md`: contains deployment/reproducible package design that includes adapter source.

## Files to Modify / Create

### Must create (adapter source missing):
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/__init__.py`
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/config.py`
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/contexts.py`
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/mcp_client.py`
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/provider.py`
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/worker.py`
- `integrations/hermes-memory-provider/mnemosyne_rust_hermes/py.typed`

### Should update (docs/config):
- `docs/HERMES_INTEGRATION.md` — add skills/bot mode notes; confirm provider id and namespace.
- `README.md` — clarify `mnemosyne-rust` adapter vs future `mnemosyne` Python provider; add skills reference.
- `docs/plans/item_02_python_baseline.md` — update status from MISSING to RECONSTRUCTED once adapter files exist.
- `docs/plans/item_07_deploy_repro.md` — add pinned binary version / digest reference once binary built.
- `integrations/hermes-memory-provider/README.md` — add skills/bot mode compatibility note; clarify dependency on `MNEMOSYNE_BIN`.

## Verification (After Plan Approval)

Once user approves this plan, verification steps will be:
1. Create adapter source files based on contracts from `docs/plans/item_02_python_baseline.md` and tests.
2. Install adapter (`python -m pip install integrations/hermes-memory-provider/`).
3. Verify entry point (`python -c "import importlib.metadata; ..."`).
4. Verify adapter contracts (`python -c "from mnemosyne_rust_hermes.config import default_config; cfg = default_config(); assert cfg.provider_id == 'mnemosyne-rust'; assert cfg.namespace == 'agent:hermes'"`).
5. Verify adapter tests (`python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v`).
6. Verify binary dependency (`MNEMOSYNE_BIN=mnemosyne` resolves; adapter produces clear error if missing).
7. Confirm docs (`docs/HERMES_INTEGRATION.md`) match adapter contracts.
8. Confirm `README.md` clarifies provider identity (`mnemosyne-rust`) and future `mnemosyne` path.

## Risks / Open Items (Not Blocked by This Plan)

- Binary digest (`sha256:`) still unknown; adapter should handle missing binary gracefully (error message) rather than crash silently.
- `mnemosyne-memory 3.15.1` upstream source reference is missing; adapter does not require it (adapter uses stdio JSON-RPC over `MNEMOSYNE_BIN`), but if user plans a pure-Python `mnemosyne` provider, that upstream will be needed separately.
- Skills integration (`agentskills.io`) and bot mode (`~/.hermes/config.yaml`) are Hermes-side configurations; adapter does not block them, but documentation should confirm compatibility.
- Audit repair (`migrations/libsql/` schema) is deferred per `docs/plans/item_05_*`; adapter's `on_pre_compress` is fail-closed (good), but full audit repair is out of scope for this integration plan.

## Next Action

**Await user approval of this plan** before creating adapter source files or making edits. Once approved:
1. Reconstruct adapter source (`Phase A`).
2. Update docs/config (`Phase B`).
3. Verify installation and tests (`Phase C`).
4. Submit results.
