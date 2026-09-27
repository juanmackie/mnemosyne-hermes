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
Hermes virtualenv, then installs `$HERMES_HOME/plugins/mnemosyne` as a symlink
to the directory containing `hermes_memory_provider/__init__.py`. Where
directory symlinks are unavailable it falls back to a copy with a
`PROVENANCE.json` digest record; `./install.sh --copy` forces that mode. Doctor
accepts a copy only while format-v1 provenance matches the complete package
inventory (excluding runtime bytecode caches). The installer then selects
`memory.provider: mnemosyne`. The database is created on
first write, not by the installer. `./install.sh --help` lists every flag,
including `--copy`, `--venv`, `--python`, `--hermes-home` and `--db-path`.

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
- **Tools** — exposes four curated defaults; the complete 40-tool surface is
  opt-in. Names, groups, and default status are in the
  [canonical tool table](#canonical-tool-names-and-default-exposure).
  `handle_tool_call` returns JSON strings and reports `memory_unavailable`
  with a reason when the engine is not importable.

No cloud API key is required. Core storage and keyword recall are local and
keyless; optional LLM work goes through the active Hermes model configured in
`$HERMES_HOME/config.yaml`, so there is no second provider key to manage.

### Configuration keys

| Key | Effect |
| --- | --- |
| `memory.provider` | Must be `mnemosyne` |
| `memory.mnemosyne.db_path` | Explicit store path; beats the env var |
| `memory.mnemosyne.tools` | Omit/null for four core tools; `['*']` opts into all 40; `[]` disables tool exposure |
| `memory.mnemosyne.profile_isolation` | Bank per Hermes profile |

Unknown tool names in `memory.mnemosyne.tools` fail loudly at startup; `'*'`
must be the only list entry. An explicit `db_path` wins over
`profile_isolation` (the provider warns when both are set) — a typo must not
silently move where memory is written.

### Canonical tool names and default exposure

This is the canonical Hermes provider tool-name table. The lite MCP tool names
are a separate API documented in [MCP_SERVER.md](../MCP_SERVER.md).
`memory.mnemosyne.tools: ["*"]` exposes every row marked **opt-in** as well as
the four default tools; a configured list can select any subset.

| Tool | Group | Default | Purpose |
| --- | --- | --- | --- |
| `mnemosyne_remember` | Core | yes | Store a durable memory. |
| `mnemosyne_recall` | Core | yes | Search private memories. |
| `mnemosyne_stats` | Core | yes | Show storage counts and tiers. |
| `mnemosyne_forget` | Core | yes | Permanently delete a memory by ID. |
| `mnemosyne_shared_remember` | Shared surface | opt-in | Store a cross-agent surface memory. |
| `mnemosyne_shared_recall` | Shared surface | opt-in | Search the shared surface store. |
| `mnemosyne_shared_forget` | Shared surface | opt-in | Delete a shared-surface memory by ID. |
| `mnemosyne_shared_stats` | Shared surface | opt-in | Show shared-surface path and counts. |
| `mnemosyne_sleep` | Lifecycle | opt-in | Run consolidation. |
| `mnemosyne_invalidate` | Lifecycle | opt-in | Expire or supersede a memory. |
| `mnemosyne_validate` | Lifecycle | opt-in | Attest, update, or invalidate a memory. |
| `mnemosyne_get` | Retrieval | opt-in | Fetch a memory by primary key. |
| `mnemosyne_update` | Lifecycle | opt-in | Update memory content or importance. |
| `mnemosyne_batch` | Lifecycle | opt-in | Apply supported mutations atomically. |
| `mnemosyne_apply_pending` | Lifecycle | opt-in | Commit staged writes when approval is enabled. |
| `mnemosyne_triple_add` | Knowledge graph | opt-in | Add a temporal fact triple. |
| `mnemosyne_triple_query` | Knowledge graph | opt-in | Query temporal fact triples. |
| `mnemosyne_triple_end` | Knowledge graph | opt-in | End a temporal fact without replacement. |
| `mnemosyne_remember_canonical` | Canonical profile | opt-in | Set a single-source-of-truth profile fact. |
| `mnemosyne_recall_canonical` | Canonical profile | opt-in | Read canonical profile facts. |
| `mnemosyne_forget_canonical` | Canonical profile | opt-in | Retire a canonical profile fact. |
| `mnemosyne_model_card` | Canonical profile | opt-in | Render canonical facts as a model card. |
| `mnemosyne_model_refresh` | Canonical profile | opt-in | Inspect inferred canonical updates. |
| `mnemosyne_task_progress` | Canonical profile | opt-in | Track cross-session task progress. |
| `mnemosyne_scratchpad_write` | Scratchpad | opt-in | Write a temporary note. |
| `mnemosyne_scratchpad_read` | Scratchpad | opt-in | Read temporary notes. |
| `mnemosyne_scratchpad_clear` | Scratchpad | opt-in | Clear temporary notes. |
| `mnemosyne_export` | Data management | opt-in | Export memories for backup or migration. |
| `mnemosyne_import` | Data management | opt-in | Import memories from a file or provider. |
| `mnemosyne_diagnose` | Diagnostics | opt-in | Run PII-safe installation diagnostics. |
| `mnemosyne_recall_diagnostics` | Diagnostics | opt-in | Report recall-path and fallback metrics. |
| `mnemosyne_graph_query` | Knowledge graph | opt-in | Traverse graph edges from a memory. |
| `mnemosyne_graph_link` | Knowledge graph | opt-in | Add a semantic edge between memories. |
| `mnemosyne_sync_push` | Remote sync | opt-in | Push local changes to a configured server. |
| `mnemosyne_sync_pull` | Remote sync | opt-in | Pull changes from a configured server. |
| `mnemosyne_sync_status` | Remote sync | opt-in | Show remote sync state. |
| `mnemosyne_persona_promote` | Persona | opt-in | Promote a memory to the persona tier. |
| `mnemosyne_persona_demote` | Persona | opt-in | Demote a persona fact. |
| `mnemosyne_persona_list` | Persona | opt-in | List persona facts. |
| `mnemosyne_persona_reinforce` | Persona | opt-in | Reinforce a persona fact. |

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
