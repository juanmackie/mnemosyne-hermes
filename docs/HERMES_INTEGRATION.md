# Mnemosyne + Hermes Agent

This is the setup guide for running this repository's Hermes memory provider.
It takes you from a clean checkout to a verified install, then explains the
configuration keys and tool surface the provider exposes.

Two products ship here, and this page covers only the first:

| Product | What it is | Doc |
| --- | --- | --- |
| Hermes provider | Engine-backed, provider id `mnemosyne` | this page |
| Lite surface | Standalone SQLite store + MCP server | [QUICK_START.md](../QUICK_START.md) |

The provider is the only provider this repository registers; the lite surface has
its own protocol notes in [MCP_SERVER.md](../MCP_SERVER.md). The provider is a vendored
snapshot of `hermes_memory_provider` from `mnemosyne-memory 3.15.1` (MIT, ©
Abdias J / AxDSan) with a declared patch layer — provenance, hashes and the sync
policy live in [integrations/hermes-provider/README.md](../integrations/hermes-provider/README.md).

## 1. Install

Prerequisites: a Hermes install (`$HERMES_HOME`, default `~/.hermes`) and `uv`.

```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes
./install.sh --dry-run     # plan only: venv, symlink target, resolved DB path
./install.sh               # uv install + plugin symlink + provider selection
```

`./install.sh` installs the vendored provider and its pinned engine into the
Hermes virtualenv, symlinks `$HERMES_HOME/plugins/mnemosyne` at the directory
containing `hermes_memory_provider/__init__.py`, and selects
`memory.provider: mnemosyne`. The database is created on first write, not by the
installer. `./install.sh --help` lists every flag, including `--venv`,
`--python`, `--hermes-home` and `--db-path`.

## 2. Verify

```bash
hermes mnemosyne doctor --no-fix   # must exit 0
hermes memory status               # provider installed / available / active
ls -l "$HERMES_HOME/plugins/mnemosyne"   # must point at the provider package
bash scripts/smoke-hermes-onboarding.sh  # clean-user acceptance lane
```

`doctor` exits non-zero when the engine import is missing, instead of leaving a
provider that reports `available` and then no-ops every call. The loader test
covers both the bare and the installed case.

**Restart the gateway after any provider change.** Hermes caches a loaded
provider module in `sys.modules` for the life of the process, so a running
gateway keeps executing the old module while a one-shot CLI probe already looks
healthy.

## 3. Where memory lives

The provider resolves its database in this order (first match wins):

| Order | Source | Notes |
| --- | --- | --- |
| 1 | `memory.mnemosyne.db_path` in `$HERMES_HOME/config.yaml` | Per-install override |
| 2 | `MNEMOSYNE_DB_PATH` environment variable | Read by the provider and the CLI |
| 3 | Engine default | `MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` > `~/.hermes` |

`doctor` and `hermes mnemosyne stats` print the resolved path. The provider logs
a warning (and `doctor` reports it) when the store sits outside `$HERMES_HOME`,
because logs and memory are then split across two roots.

If you already have an engine database from an earlier install, there is nothing
to migrate: point the provider at that file with `memory.mnemosyne.db_path` or
`MNEMOSYNE_DB_PATH` and it becomes the live store. `hermes mnemosyne import
--list-providers` lists the external sources the CLI can import from.

The lite surface's store (`~/.mnemosyne-lite/mnemosyne.db` by default) is a
different file and a different product. Nothing written by the provider is
visible to `mnemosyne-lite`, and the reverse is true too.

## 4. What the provider does

It implements the Hermes `MemoryProvider` contract:

- **Injection** — prefetch before each model call, returned unfenced so Hermes
  can apply its own `<memory-context>` wrapper and streaming scrubber.
- **Capture** — user-originated turns are recorded; `cron`, `flush`, `subagent`,
  `background` and `skill_loop` runs are skipped for both capture and injection.
- **Tools** — `get_tool_schemas()` exposes the engine's tools (`mnemosyne_remember`,
  `mnemosyne_recall`, and the rest of the engine's set); `handle_tool_call`
  returns JSON strings and reports `memory_unavailable` with a reason when the
  engine is not importable.

No cloud API key is required. Core storage and keyword recall are local and
keyless; optional LLM work goes through the active Hermes model configured in
`$HERMES_HOME/config.yaml`, so there is no second provider key to manage.

### Configuration keys

| Key | Effect |
| --- | --- |
| `memory.provider` | Must be `mnemosyne` |
| `memory.mnemosyne.db_path` | Explicit store path; beats the env var |
| `memory.mnemosyne.tools` | Restrict exposed tools (`[]` = none) |
| `memory.mnemosyne.profile_isolation` | Bank per Hermes profile |

Unknown tool names in `memory.mnemosyne.tools` fail loudly at startup, and an
explicit `db_path` wins over `profile_isolation` (the provider warns when both
are set) — a typo must not silently move where memory is written.

## 5. Using the lite store from Hermes

If you want explicit memory tools over the *lite* store rather than the
provider, register the lite MCP server as an MCP server instead:

```yaml
mcp_servers:
  mnemosyne-lite:
    command: /absolute/path/to/mnemosyne-lite
    args: ["mcp"]
    env:
      MNEMOSYNE_DB_PATH: /home/you/.mnemosyne-lite/mnemosyne.db
```

That path is the lite store, not the provider's DB. Use an absolute `command`:
Hermes may start the server from a different working directory than your shell.
Running both is fine — they are separate stores with separate schemas, and the
MCP tools never touch the provider's database. See
[MCP_SERVER.md](../MCP_SERVER.md) for the protocol and
[MCP client configuration examples](MCP_CLIENT_CONFIGS.md) for other clients.

## 6. Uninstall

```bash
./install.sh --uninstall            # symlink + provider package, memory kept
./install.sh --uninstall --purge    # also the engine and $HERMES_HOME/mnemosyne
```

Restart the gateway afterwards, and point `memory.provider` elsewhere if
`$HERMES_HOME/config.yaml` still selects `mnemosyne`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Doctor exits non-zero | Read the first `FAIL` line; `--dry-run` lists the fixes |
| No provider in `hermes memory status` | Check the plugin symlink, re-run `./install.sh` |
| Unexpected store path | `hermes mnemosyne doctor --no-fix` prints the resolved path |
| Fix works in CLI only | Restart the gateway; the old module stays cached |

More in [TROUBLESHOOTING.md](../TROUBLESHOOTING.md).
