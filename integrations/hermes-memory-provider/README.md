# mnemosyne-rust-hermes

A pure-standard-library Hermes Agent **MemoryProvider** that gives Hermes
long-term memory backed by the local **Mnemosyne Rust binary**, spoken to over a
persistent, newline-delimited JSON-RPC 2.0 MCP **stdio** session.

* No Python dependency beyond the standard library (dependencies = [] stays true).
* No cloud keys, no keyring, no network: storage is Mnemosyne's local
  LibSQL/SQLite database.
* One long-lived child process (mnemosyne mcp), not one process per call.

> **Naming note.** This repository does **not** ship a Python provider named
> mnemosyne. This distribution is the Rust binary plus this adapter, whose
> provider id is deliberately mnemosyne-rust so it can never shadow or be
> confused with a third-party Python package that happens to be called
> mnemosyne. If you see memory.provider: mnemosyne, that is something else.

## Install

Python 3.11 or newer, and the mnemosyne binary on PATH (or point
MNEMOSYNE_BIN at it):

    # inside integrations/hermes-memory-provider
    python -m pip install .
    # or, for development without installing:
    python -m pip install -e .

The wheel registers the entry point
hermes_agent.memory_providers -> mnemosyne-rust = mnemosyne_rust_hermes:register,
which is what makes the provider discoverable by Hermes.

## Select the provider

In the Hermes agent configuration:

    memory:
      provider: mnemosyne-rust

Hermes imports the entry point, calls register(ctx) (which calls
ctx.register_memory_provider(provider)), then calls
initialize(session_id, hermes_home=...). Prefetched memories are injected
automatically; the adapter never emits the <memory-context> wrapper itself,
because Hermes applies that wrapper and its streaming scrubber.

## Environment variables

Every setting maps 1:1 onto a ProviderConfig field (see
mnemosyne_rust_hermes/config.py). Environment values win over the defaults; the
hermes_home kwarg from initialize() is used unless MNEMOSYNE_HERMES_HOME or
MNEMOSYNE_DB_PATH overrides it.

| Variable | Default | Meaning |
| --- | --- | --- |
| MNEMOSYNE_BIN | mnemosyne | Executable name or absolute path. |
| MNEMOSYNE_MCP_ARGS | mcp | Args for the binary; JSON array or shell-style string. |
| MNEMOSYNE_DB_PATH | <hermes_home>/mnemosyne/mnemosyne.db | Database file. |
| MNEMOSYNE_HERMES_HOME | hermes_home kwarg | Active Hermes home for storage resolution. |
| MNEMOSYNE_NAMESPACE | agent:hermes | Memory namespace written and searched. |
| MNEMOSYNE_POLICY_OWNER | MNEMOSYNE_PROVIDER_ID | Owner of automatic capture. |
| MNEMOSYNE_PROVIDER_ID | mnemosyne-rust | Reported provider / policy identity. |
| MNEMOSYNE_EXECUTION_CONTEXT | unset | Default execution context. |
| MNEMOSYNE_REQUEST_TIMEOUT | 10 | Seconds per JSON-RPC request. |
| MNEMOSYNE_INITIALIZE_TIMEOUT | 15 | Seconds for the MCP handshake. |
| MNEMOSYNE_PREFETCH_TIMEOUT | 3 | Seconds for a bounded prefetch. |
| MNEMOSYNE_SHUTDOWN_TIMEOUT | 5 | Seconds to drain pending captures. |
| MNEMOSYNE_EAGER_CONNECT | off | 1 connects during initialize() instead of lazily. |

## Behaviour contract

* is_available() spawns nothing and performs no network I/O.
* sync_turn() queues the capture on a background worker and returns promptly, so
  a slow MCP round-trip never blocks a turn; it is also serialized so turn N is
  recorded before turn N+1.
* prefetch() returns **unfenced** plain text and degrades to "" instead of
  raising if the transport fails.
* Automatic capture and injection are skipped for the cron, flush, subagent,
  background and skill_loop execution contexts: the MCP call is not even
  attempted, and the context is still forwarded to the Rust side for defence in
  depth. Assistant-authored turns are never captured.
* Capture is disabled entirely when MNEMOSYNE_POLICY_OWNER names any identity
  other than this provider.
* on_pre_compress() is **fail-closed**
  (pre_compress_checkpoint_api_version = 2): it writes a durable, fsynced,
  content-addressed checkpoint under <storage>/checkpoints/ and raises
  CheckpointError rather than reporting partial success. Re-compressing the same
  content is idempotent (same digest, one file).
* shutdown() is idempotent: the first call drains pending captures, stops the
  worker and closes the transport; later calls are no-ops.
* on_memory_write() records the write for diagnostics but deliberately captures
  nothing, because Hermes already persisted it.

## Tool surface

get_tool_schemas() advertises mnemosyne_memory_search and
mnemosyne_memory_remember; handle_tool_call() returns a JSON-serialisable
mapping ({ok: true, ...}).

## Tests

    python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v
    pytest integrations/hermes-memory-provider/tests/test_provider.py

The suite uses an in-process fake MCP server plus one real-stdio test that runs
a small scripted JSON-RPC server through the production StdioJsonRpcClient.

## Research note

The pinned MemoryProvider.initialize contract documents agent_context values
primary, subagent, cron and flush. background and skill_loop are treated as
additional non-interactive contexts (a best-effort superset); the list lives in
mnemosyne_rust_hermes/contexts.py.
