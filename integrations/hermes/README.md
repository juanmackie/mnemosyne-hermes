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

The standalone surface (`src/lib` + `src/mnemosyne`) is unaffected by this
retirement; it remains as a standalone CLI, and is not a Hermes provider.
