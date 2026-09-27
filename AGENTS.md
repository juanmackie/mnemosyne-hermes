# mnemosyne-hermes agent contract

Nearest-owning contract for this repository. It refines the parent policy with
repository-specific facts and cannot weaken a mandatory parent rule; conflicts
resolve to the parent.

Read [README.md](README.md) for what the project is. This file is about what an
agent must not break.

## What ships

Two artifacts, deliberately separate:

1. **The provider** — `integrations/hermes-provider/`. A vendored, engine-backed
   Hermes memory provider. Provider id **`mnemosyne`**. Installed by `./install.sh`.
   This is the product.
2. **The lite surface** — `src/mnemosyne_lite/`. A standalone SQLite keyword
   store with a CLI (`mnemosyne-lite`) and an MCP stdio server. **Not** a Hermes
   provider, and it must never be installed into the Hermes venv.

The Rust product that used to live here is retired. `docs/archive/` and git
history keep it; do not restore it.

## Scope and ownership

| Path | Owns |
| --- | --- |
| `install.sh` | Provider install, `--copy`, `--uninstall`, `--purge`, `--dry-run` |
| `integrations/hermes-provider/` | The vendored snapshot and every gate around it: `VENDORED_FROM.json` (hashes), `PATCHES.md`, `CONTRACT_AUDIT.md`, `LIVE_VERIFICATION.md`, `README.md`, `pyproject.toml` (engine pin) |
| `src/mnemosyne_lite/` | `cli.py`, `mcp.py`, `tools.py`, `storage.py`, `db_path.py` |
| `tests/` | Contract and regression suites (see Verification) |
| `scripts/` | Repo gates and helpers: `checks.sh` (registry), `check_notes.sh`, `check_version_drift.sh`, `smoke-hermes-onboarding.sh`, `engine-parity-check.sh`, `vendor-provider-sync.sh`, `upstream-drift-check.py` |
| `bench/` | The recall-latency harness (`measure.sh`, `autoresearch.sh`, `run.sh`) and its record (`README.md`) |
| `docs/` | `AGENT_SETUP.md`, `HERMES_INTEGRATION.md`, `MCP_CLIENT_CONFIGS.md`; `docs/archive/` is historical |
| `examples/` | Runnable usage examples |
| `plans/` | Working plans; historical context, not current contracts |
| Root docs | `README.md`, `QUICK_START.md`, `TROUBLESHOOTING.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `MCP_SERVER.md`, `AGENTS.md`, `LICENSE`, `NOTICE` |

## Contracts that must not drift

- **Provider id** `mnemosyne`, a single registration, one CLI command. Hermes
  discovery gives a bundled provider precedence over a same-name user plugin;
  the onboarding smoke exercises that collision and still requires one loaded
  provider. Do not add another registration entry point.
- **Vendored snapshot discipline.** `integrations/hermes-provider/hermes_memory_provider/`
  is a byte-hashed upstream snapshot. Any edit requires, in the same change:
  a `# LOCAL PATCH:` marker at the site, a `PATCHES.md` entry, and an updated
  hash/bytes/lines in `VENDORED_FROM.json`. `python tests/test_vendored_provider.py`
  is the gate. Never "fix" a vendored file without the manifest update.
- **Engine pin** `mnemosyne-memory[embeddings]>=3.15.1,<3.16` in `install.sh`,
  `integrations/hermes-provider/pyproject.toml` and `VENDORED_FROM.json`. The
  upper bound is a real contract; widening it requires re-running
  `CONTRACT_AUDIT.md`.
- **Provider DB precedence**: `memory.mnemosyne.db_path` > `MNEMOSYNE_DB_PATH` >
  engine default (`MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` > `~/.hermes`).
  `db_path` wins over `profile_isolation`.
- **Hermes provider tool surface**: default exposure is the four core tools;
  `memory.mnemosyne.tools: ["*"]` opts into the full set. The only canonical
  Hermes tool-name table is in
  [docs/HERMES_INTEGRATION.md](docs/HERMES_INTEGRATION.md#canonical-tool-names-and-default-exposure).
- **Lite MCP tool names** `mnemosyne_memory_search`, `mnemosyne_memory_remember`,
  `mnemosyne_prefetch`, `mnemosyne_sync_turn`; the `mnemosyne.recall` /
  `mnemosyne.remember` aliases; the `sync_turn` skip semantics.
- **Lite default DB** `~/.mnemosyne-lite/mnemosyne.db`, and `DATABASE_URL`
  accepted only for `sqlite`/`sqlite3`/`file`.
- **Storage safety.** Classification runs before any DDL/DML/persistent pragma,
  and a refusal leaves the file byte-identical. Do not move a write ahead of the
  classification. WAL stays bounded. One connection per thread, and the recall
  memo is per-thread because its version is built from the calling thread's
  connection.
- **Fail loud.** A store that cannot be read raises `StorageError`; it does not
  return `[]` or `0`. "No match" and "store broken" must stay distinguishable on
  both surfaces.
- **Keyless by design.** Memory must work with no cloud API key and no OS
  keyring. If a step appears to require one, that is a bug.
- **LLM inheritance.** Optional engine LLM work uses the active Hermes model
  through Hermes' own local proxy. Do not add a second provider key. This tree
  has no LLM-marked tests (the retired adapter suite was removed); any future
  LLM lane must prefer `hermes proxy start` before the legacy
  `ANTHROPIC_API_KEY` fallback.
- **Secrets never live in the repo.** This repo needs none; do not introduce
  one. Never commit `.env*`, connection configs, keys or tokens.
- **The engine owns the `mnemosyne` name.** Distribution, import package and
  console script. That is why the lite distribution is `mnemosyne-lite` /
  `mnemosyne_lite`. Do not put `src/` ahead of the engine in a Hermes venv.

## Verification

```bash
./test-all.sh --skip-llm          # provider contract gates + local unit tests
bash scripts/checks.sh            # repo gates (notes ledger, version drift)
pre-commit run --all-files        # ruff, ruff format, mypy, shellcheck
bash scripts/smoke-hermes-onboarding.sh   # clean-user acceptance (Linux/macOS)
```

The provider gates, runnable on their own in a bare venv:

```bash
python tests/test_vendored_provider.py   # snapshot hashes + declared patches
python tests/test_provider_loader.py     # loader contract, engine absent and present
python tests/test_provider_db_path.py    # DB path precedence + doctor checks
```

Health checks after an install: `hermes mnemosyne doctor --no-fix` (must exit 0)
and `hermes memory status`.

`scripts/checks.sh` is the registry of repo gates: a gate that is not listed
there is invisible. Add new gates to it.

## Working rules

- Keep the vendored provider's type-check and lint exclusions in place
  (`[tool.ruff] force-exclude`, `[tool.pyright] ignore`, and the mypy module
  override in `integrations/hermes-provider/pyproject.toml`). They exist because
  the snapshot is upstream's code and its optional imports are absent in a bare
  venv — not because the checks were inconvenient.
- One version source: `pyproject.toml` == `mnemosyne_lite.__version__` == the
  README's `**Current Version**` line. `scripts/check_version_drift.sh` enforces
  it, including that the literal appears nowhere else under `src/mnemosyne_lite/`.
- Commit messages describe the work, not the tool. Do not attribute commits to
  an AI unless explicitly asked.
- Prefer a branch for non-trivial work. CI runs on pushes to `main` and on pull
  requests.
- Do not commit scratch state: `.dream-rsi/`, `.pi/`, `bench/data/`,
  `bench/log.jsonl`, `bench/last_measure.txt`, `*.egg-info/`.

## Documentation index

- `README.md` — what ships, quickstart, verify, the lite surface, credits.
- `QUICK_START.md` — the short version of the same.
- `docs/AGENT_SETUP.md` — the step-by-step runbook an agent executes.
- `integrations/hermes-provider/README.md` — the canonical provider document.
- `docs/HERMES_INTEGRATION.md` — the longer Hermes integration guide.
- `MCP_SERVER.md`, `docs/MCP_CLIENT_CONFIGS.md` — the lite MCP surface.
- `TROUBLESHOOTING.md` — the failure modes and what they mean.
- `CONTRIBUTING.md`, `CHANGELOG.md`, `docs/archive/RUST_ARCHIVE_REF.md`.

## Known gaps

- `plans/` and `docs/archive/` are historical. Do not treat them as contracts
  and do not update them unless a task targets them.
- `integrations/hermes-provider/LIVE_VERIFICATION.md` has not been executed
  against a live gateway in this repository's history. The CI smoke lane is the
  substitute evidence.
- The local patches in `PATCHES.md` have not been sent upstream; the sync
  procedure in `scripts/vendor-provider-sync.sh` re-applies them.
- There is no browser/UI harness: the CLI and MCP server are exercised by
  `tests/test_lite_cli.py` and `tests/test_lite_mcp.py`.
