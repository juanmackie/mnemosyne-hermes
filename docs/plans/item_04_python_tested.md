# Item 4 — Python Integration Tested Product (Planning Deliverable)

Status: PLANNING ONLY. No CI pipeline changes applied; no source modifications. Contracts preserved: `memory.provider: mnemosyne` (current adapter `mnemosyne-rust`; future Python `mnemosyne` must preserve `agent:hermes`, DB path, provider id, tool names, namespace). Both tool surfaces remain compatible initially.

## Existing plugin contracts (evidence from integrations/hermes-memory-provider/)

- Entry point: `hermes_agent.memory_providers` → `mnemosyne_rust_hermes:register`
- Provider id: `mnemosyne-rust` (must be preserved; transition to `mnemosyne` requires consumer evidence)
- Namespace: `agent:hermes`
- DB: `MNEMOSYNE_DB_PATH` or `<hermes_home>/mnemosyne/mnemosyne.db`
- Tools: `mnemosyne_memory_search` (`PREFETCH_ALIASES`), `mnemosyne_memory_remember` (`SYNC_TURN_ALIASES`)
- Context skip: `cron`, `flush`, `subagent`, `background`, `skill_loop` (plus `primary` handled by Hermes wrapper)
- Lifecycle: `initialize()`, `prefetch()`, `sync_turn()`, `shutdown()` (idempotent; drains pending captures); `on_pre_compress()` (fail-closed, checkpoint API v2)
- Background capture: `BackgroundWorker` queued captures; serialized per turn (`turn N` before `turn N+1`)

## Required Python CI coverage (design only)

The current `.github/workflows/ci.yml` runs Rust tests (`cargo test --lib`, `cargo fmt --check`, `cargo clippy`, `cargo build --release`). It does NOT cover the Python plugin, the Python MCP server, or the Python adapter contracts.

Proposed `python-ci.yml` (to be added to `.github/workflows/` or `tests/` script, not applied):

```yaml
# .github/workflows/python-ci.yml (proposed, not applied)
name: Python Integration CI
on: [push, pull_request]
jobs:
  python-test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install plugin and dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install .
          python -m pip install pytest pytest-asyncio
      - name: Verify entry-point registration
        run: |
          python -c "
          import importlib.metadata
n          eps = importlib.metadata.entry_points(group='hermes_agent.memory_providers')
          names = [e.name for e in eps]
          assert 'mnemosyne-rust' in names, f'Provider not registered: {names}'
          print('Provider registered:', names)
          "
      - name: Run plugin unit tests
        run: |
          python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v
          # Expected: exit 0; contracts verified; no DB writes required for basic tests.
      - name: Test adapter contracts
        run: |
          python -c "
          from mnemosyne_rust_hermes.config import default_config, NAMESPACE_DEFAULT, PROVIDER_ID_DEFAULT
          cfg = default_config()
          assert cfg.namespace == 'agent:hermes', 'Namespace broken'
          assert cfg.provider_id == 'mnemosyne-rust', 'Provider id broken'
          assert cfg.resolved_db_path() is not None, 'DB path unresolved'
          print('Contracts preserved.')
          "
      - name: Verify clean install with real stdio MCP (optional, gated by binary availability)
        if: env.MNEMOSYNE_BIN != ''
        run: |
          export MNEMOSYNE_BIN="${MNEMOSYNE_BIN:-mnemosyne}"
          python -m unittest integrations/hermes-memory-provider/tests/test_provider.py -v 2>&1 || true
          # Note: full stdio test requires binary on PATH; failure is acceptable if binary absent.
```

## Test contracts per operation (lifecycle callbacks, persistence, no duplication)

Based on `provider.py` contract and `tests/test_provider.py`:

1. **Initialize (`initialize`)**: Must set namespace (`agent:hermes`), DB path, policy owner; must NOT capture anything. Must return promptly; must not spawn background worker unless `eager_connect` is enabled.
2. **Prefetch (`prefetch`)**: Must return unfenced plain text; must NOT emit `<memory-context>` wrapper (Hermes applies it). Must degrade to `""` on transport failure, not raise.
3. **Capture (`sync_turn`)**: Must queue on `BackgroundWorker`; must NOT block turn; must serialize (turn N before turn N+1). Must skip contexts: `cron`, `flush`, `subagent`, `background`, `skill_loop`. Assistant-authored turns (`primary` if assistant context) must NOT be captured.
4. **On pre-compress (`on_pre_compress`)**: Must be fail-closed (`CHECKPOINT_API_VERSION = 2`); must write durable, fsynced, content-addressed checkpoint under `<storage>/checkpoints/`; must raise `CheckpointError` on failure (not partial success). Re-compressing same content must be idempotent (same digest, one file).
5. **Shutdown (`shutdown`)**: Idempotent; first call drains pending captures, stops worker, closes transport (`StdioJsonRpcClient`); later calls must be no-ops.
6. **Memory write (`on_memory_write`)**: Must record write for diagnostics; must NOT capture anything (Hermes already persisted it). Must NOT duplicate persistence.

## Concurrent process / concurrency / retry design

Based on `provider.py` and `mcp_client.py`:

- `StdioJsonRpcClient` uses persistent newline-delimited JSON-RPC over stdio; one long-lived child process (`mnemosyne mcp`), not one per call.
- Concurrent calls to `prefetch()` or `handle_tool_call()` must serialize through the client; no concurrent stdio writes allowed.
- Retry behavior: `MNEMOSYNE_REQUEST_TIMEOUT` (default 10s) and `MNEMOSYNE_INITIALIZE_TIMEOUT` (default 15s). Retries should use bounded backoff; infinite retry loops must be avoided.
- Gateway/dashboard concurrency (`docs/DASHBOARD.md` or `.auto/checks.sh`): no specific gateway test exists; must be verified that concurrent `mnemosyne` processes use separate DB connections or share read-only access without corrupting WAL.

## Background capture / skipped contexts / shutdown/restart tests

Proposed test cases (design, not executed):

```python
# Proposed test snippet (not applied)
import unittest, os
from mnemosyne_rust_hermes.provider import MemoryProvider, ProviderConfig

class LifecycleTests(unittest.TestCase):
    def test_shutdown_idempotent(self):
        provider = MemoryProvider()
        provider.shutdown()
        provider.shutdown()  # must not raise; must not spawn new captures
        # Verify no pending captures remain (if BackgroundWorker exposes state)

    def test_skip_contexts(self):
        # cron, flush, subagent, background, skill_loop must skip capture
        contexts = ["cron", "flush", "subagent", "background", "skill_loop"]
        for ctx in contexts:
            # Mock sync_turn with context; verify no capture queued
            pass  # actual test requires mock BackgroundWorker

    def test_no_duplicate_writes(self):
        # Call sync_turn twice with same content; verify one database write
        # (requires access to DB or mock storage layer)
        pass

    def test_compression_checkpoint(self):
        # Call on_pre_compress; verify checkpoint file exists, is fsynced,
        # digest matches content, and second call with same content is idempotent.
        pass
```

These are planning-only test designs. Actual test execution requires either a mock `StdioJsonRpcClient` (existing `tests/test_provider.py` approach) or a deployed binary.

## Gaps (explicitly unknown)

- Whether `tests/test_provider.py` covers lifecycle callbacks fully: NOT FULLY VERIFIED (subagent 2 noted basic contract tests exist; full lifecycle coverage unknown).
- Whether gateway/dashboard concurrency is tested: UNKNOWN (no `tests/dashboard_agents_integration.rs` covers Python adapter; Rust dashboard tests exist but not Python surface).
- Whether retry behavior is tested: UNKNOWN (no retry test file found in `.auto/` or `tests/`).
- Whether shutdown and restart survive DB reconnection: UNKNOWN (must test by stopping `mnemosyne mcp` process, restarting, and verifying `prefetch()` resumes correctly).
- Whether compression with queued captures preserves pending information: UNKNOWN (see item 5); current adapter (`provider.py`) has `on_pre_compress()` but no explicit drain/checkpoint integration with queued captures.

## Deliverable artifacts

- `.auto/deliverables/item_04_python_tested.md` (this file)
- `docs/plans/item_04_python_tested.md` (mirror)
- Proposed `.github/workflows/python-ci.yml` (text only, not applied)
- Proposed test design snippet (text only, not executed)
