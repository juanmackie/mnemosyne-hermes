# Retired — see `integrations/hermes-provider/`

This directory used to hold `mnemosyne_hermes`, a slim `lib.storage`-backed
memory provider. It was **retired** because it could not meet the provider
contract:

- recall was keyword-only (`instr(content_lower, ?)`), with none of the
  vectors + FTS + graph ranking the engine provides;
- it read and wrote the standalone lite store, so it shared every storage-safety
  bug that store had (see `src/lib/storage.py` and
  `tests/test_python_hardening.py`);
- its `pyproject.toml` declared a `mnemosyne-hermes` console script pointing at
  `mnemosyne_hermes.cli:main`, a module that never existed.

The canonical Hermes provider is now the vendored, engine-backed one in
[`integrations/hermes-provider/`](../hermes-provider/README.md) — provider id
`mnemosyne`, single registration, no second entry point.

## What was dropped with it

The retired provider implemented a **fail-closed `on_pre_compress` checkpoint**
(`on_pre_compress(messages, require_checkpoint=True)` writing
`checkpoints/*.json` and raising `CheckpointError` to block compression when the
checkpoint could not be written; covered by the deleted
`tests/test_native_provider.py`). The canonical provider does **not** override
`on_pre_compress` — see finding F3 in
[`CONTRACT_AUDIT.md`](../hermes-provider/CONTRACT_AUDIT.md) and decision F3 in
`plans/dev-todo-v2.md`: upstream's provider uses sync_turn + session-end
consolidation instead, and the engine exposes no message-accepting extraction
API to forward the messages to. If that checkpoint behaviour is wanted, it has
to be implemented deliberately in the canonical provider (as a declared local
patch), not inherited from this retired one.

The standalone surface (`src/lib` + `src/mnemosyne_lite`) is unaffected by this
retirement; it remains a standalone CLI **and** MCP stdio server
(`mnemosyne-lite mcp`), and is not a Hermes provider.
