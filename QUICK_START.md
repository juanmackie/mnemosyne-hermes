# Quick Start

This repository ships two products, installed separately. They do not share a
store and neither one needs a cloud API key to store or search memory.

| Component | What it is | Installed with |
| --- | --- | --- |
| Hermes provider | Engine-backed memory provider, provider id `mnemosyne` | `./install.sh` |
| Lite surface | Standalone SQLite store (CLI + MCP stdio server) | `pip install -e .` |

The engine (`mnemosyne-memory`, PyPI) and the vendored provider are by
Abdias J / AxDSan (MIT); see the provider README for provenance and hashes.

## What you need

- `git` and `uv` — the installer drives `uv pip`, which also works in pip-less
  and root-owned Hermes virtualenvs.
- A working Hermes install: `hermes` on `PATH` with `$HERMES_HOME` set (default
  `~/.hermes`).
- Python 3.11+ for the lite surface (core memory uses only the stdlib).

## 1. Install the Hermes provider

```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes
./install.sh --dry-run     # plan only: resolved venv, plugin target, DB path
./install.sh               # asks before changing anything
```

`./install.sh` does this, in order:

1. installs the vendored provider plus its pinned engine into the Hermes venv;
2. installs `$HERMES_HOME/plugins/mnemosyne` as a symlink to
   `integrations/hermes-provider/hermes_memory_provider` (the directory that
   contains `__init__.py`), or as a digest-verified copy when symlinks are
   unavailable. Use `./install.sh --copy` to force copy mode;
3. selects `memory.provider: mnemosyne`.

The memory database is created on first write, not by the installer.

### Verify the install

```bash
hermes mnemosyne doctor --no-fix        # must exit 0
hermes memory status                    # installed / available / active
bash scripts/smoke-hermes-onboarding.sh # clean-user acceptance lane
```

Then restart the gateway: Hermes caches the loaded provider module per process,
so a running gateway keeps executing the old code until it restarts.

### Use it

```bash
hermes mnemosyne stats                  # memory counts
hermes mnemosyne inspect "storage"      # search the store by hand
hermes mnemosyne doctor --no-fix        # diagnose; --dry-run shows fixes
```

The provider injects relevant context before each model call and captures
user-originated turns (`cron`, `flush`, `subagent`, `background` and
`skill_loop` runs are skipped). Its optional LLM work goes through the active
Hermes model from `$HERMES_HOME/config.yaml` — no second API key.

The store it writes is resolved in this order: `memory.mnemosyne.db_path` >
`MNEMOSYNE_DB_PATH` > engine default (`MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` >
`~/.hermes`, then `mnemosyne/data/mnemosyne.db`). `doctor` prints the path it
resolved.

## 2. The lite surface (standalone)

```bash
pip install -e .
mnemosyne-lite init
mnemosyne-lite remember --content "Storage decision: SQLite for the lite store"
mnemosyne-lite recall --query "storage decision"
mnemosyne-lite list --limit 10
```

| Command | What it does |
| --- | --- |
| `mnemosyne-lite init` | Create the schema (idempotent) |
| `mnemosyne-lite remember` | Store a memory (`--content`, `--namespace`, `--importance`) |
| `mnemosyne-lite recall` | Search by token (`--query`, `--max-results`) |
| `mnemosyne-lite list` | List memories (`--limit`, `--sort-by`) |
| `mnemosyne-lite bootstrap` | Bounded constraints, provenance and abstentions |
| `mnemosyne-lite backup` | Copy the store with SQLite's backup API |
| `mnemosyne-lite restore` | Replace the store from a backup (asks first) |
| `mnemosyne-lite maintenance` | Report duplicate groups; `--auto-apply` deletes them |
| `mnemosyne-lite diagnostics` | Path, size, counts, versions, live PRAGMA state |
| `mnemosyne-lite mcp` | MCP stdio server (alias: `serve`) |

Things worth knowing:

- Default store: `~/.mnemosyne-lite/mnemosyne.db`. Override with `--db-path` or
  `MNEMOSYNE_DB_PATH`. `DATABASE_URL` is honoured only for `sqlite`, `sqlite3`
  and `file` schemes; any other scheme is rejected with an error rather than
  used as a filename.
- Text mode preserves existing command-specific output. `--format json` is
  accepted before or after the subcommand and prints one document per command:
  an object for `init`, `remember`, `bootstrap`, `backup`, `restore`,
  `maintenance`, and `diagnostics`; `recall` and `list` return arrays.
- `remember --namespace` defaults to `default`. `agent:hermes` is still accepted
  — it is the Hermes provider's namespace, not this surface's default.
- `--no-enrich` is accepted for compatibility and does nothing: core memory
  never calls an LLM.
- Read commands refuse to invent a store: a missing or foreign database is an
  error, so a mistyped path cannot report "0 memories".
- This is not a Hermes provider. Install it in its own virtualenv (see the
  provider README for the collision it used to cause).

### MCP server

```bash
mnemosyne-lite mcp          # newline-delimited JSON-RPC 2.0 on stdio
```

It serves an existing store over the four lite tools
(`mnemosyne_memory_search`, `mnemosyne_memory_remember`, `mnemosyne_prefetch`,
`mnemosyne_sync_turn`). See [MCP_SERVER.md](MCP_SERVER.md) for the protocol and
[examples/hermes/mcp-config.json](examples/hermes/mcp-config.json) for a client
entry.

## 3. Checks to run before opening a PR

```bash
./test-all.sh               # provider contract gates, then pytest
bash scripts/checks.sh      # repo gates (notes, version drift)
pre-commit run --all-files  # ruff, mypy, shellcheck
```

## Troubleshooting

Common failures and their fixes are in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Where to go next

- [integrations/hermes-provider/README.md](integrations/hermes-provider/README.md)
  — the provider, its DB precedence, sync policy and uninstall.
- [docs/HERMES_INTEGRATION.md](docs/HERMES_INTEGRATION.md) — wiring the provider
  into a Hermes install.
- [docs/MCP_CLIENT_CONFIGS.md](docs/MCP_CLIENT_CONFIGS.md) — MCP client entries.
- [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) — agent-executed setup runbook.
- [AGENTS.md](AGENTS.md) — repository layout and contracts for contributors.
