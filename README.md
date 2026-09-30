# mnemosyne-hermes

**Current Version**: 3.0.0

A Hermes memory-provider distribution: the Hermes provider this repo owns, plus
a small standalone SQLite store that ships alongside it.

Two things ship here, and they are deliberately separate:

| | What it is | Who installs it |
| --- | --- | --- |
| **The provider** — `integrations/hermes-provider/` | A vendored, engine-backed Hermes memory provider. Provider id `mnemosyne`. | `./install.sh` |
| **The lite surface** — `src/mnemosyne_lite/` | A standalone SQLite keyword store with a CLI (`mnemosyne-lite`) and an MCP stdio server. **Not** a Hermes provider. | `pip install -e .` |

The provider is the product. The lite surface is a small, keyless store you can
run without Hermes at all — useful for testing, scripts and MCP clients that are
not Hermes.

This repository is **not** [rand/mnemosyne](https://github.com/rand/mnemosyne).
The Rust product that used to live here is retired; `docs/archive/` and git
history keep it.

## What you need

- A Hermes install (`hermes-agent >=0.18,<0.20`) and its virtualenv.
- [`uv`](https://docs.astral.sh/uv/) — it works in pip-less and root-owned
  venvs, which is how some Docker Hermes installs ship.
- Python 3.11+ for the lite surface.

No cloud API key is required, for either half. Memory works keyless: the
provider is engine-backed keyword/vector search, and the lite store is plain
SQLite. Optional LLM work inside the engine goes through the model Hermes is
already configured with — this repo never asks for a second provider key.

## Quickstart — the provider

```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes
./install.sh --dry-run     # prints the resolved venv, DB path and plugin target
./install.sh               # provider + pinned engine, into the Hermes venv
```

`install.sh` does four things and nothing else: installs
`integrations/hermes-provider` plus the pinned engine into the Hermes venv,
installs `$HERMES_HOME/plugins/mnemosyne` as a symlink (or verified copy when
symlinks are unavailable), sets
`memory.provider: mnemosyne` via `hermes config set`, and verifies that exactly
one provider registers. Use `./install.sh --copy` to force copy mode; its
format-v1 `PROVENANCE.json` hashes the complete package inventory (excluding
runtime bytecode caches). `doctor` rejects drift, and install/uninstall refuse
to overwrite or remove an unverified directory; re-run the installer after
editing a copy. The installer never creates or opens the memory database — the
resolved DB path is printed up front so you can check it first.

## Verify

```bash
hermes mnemosyne doctor --no-fix   # must exit 0
hermes memory status               # mnemosyne installed / available / active
```

`doctor` is the acceptance gate. It prints the resolved DB path, the provider
package it loaded, the engine version and the detected Hermes version, and its
exit code is decided by five critical checks — engine importable, provider
registered exactly once, DB resolved and writable, DB integrity, canonical
provider deployed. A fresh install with no data directory yet passes: the check
walks up to the nearest existing ancestor and reports that the path will be
created.

The full clean-user acceptance lane is `bash scripts/smoke-hermes-onboarding.sh`
(fresh venv, fresh `HERMES_HOME`, real Hermes, then `doctor` + `memory status` +
a `sync_turn` round trip + a single-registration check). It needs real symlinks,
so it runs on Linux/macOS; on Windows it skips. The provider's curated default
and complete tool names are maintained in the
[canonical Hermes tool table](docs/HERMES_INTEGRATION.md#canonical-tool-names-and-default-exposure).

### Uninstall

```bash
./install.sh --uninstall          # removes the plugin link and the provider
./install.sh --uninstall --purge  # also removes the engine and the data dir
```

Memory is kept unless you pass `--purge`. Both forms print their plan first and
support `--dry-run`. Restart the gateway afterwards: Hermes caches a loaded
provider module for the life of the process.

## The lite surface

```bash
pip install -e .                  # installs `mnemosyne-lite`
mnemosyne-lite init               # creates ~/.mnemosyne-lite/mnemosyne.db
mnemosyne-lite remember --content "decided to use SQLite" --importance 8
mnemosyne-lite recall --query SQLite
mnemosyne-lite diagnostics        # resolved DB path, counts, live PRAGMA state
```

Commands: `init`, `remember`, `recall`, `list`, `bootstrap`, `backup`,
`restore`, `maintenance`, `diagnostics`, `mcp`. `recall` and `list` print an
aligned text table by default; pass `--format json` before or after a subcommand
for the same JSON document on stdout.

The store uses schema v3, which removes a redundant copy of every memory's text.
Opening an older lite store makes a timestamped `.pre-v3.*.bak` copy before
upgrading it. This migration is one-way: older lite releases refuse v3 stores.
Memory content is limited to 100,000 characters; unknown list sort orders and
`:memory:` paths are rejected.

`init`, `remember`, and an explicit `restore` from a backup can create the
store. Read/maintenance commands refuse to invent an empty database at a mistyped
path, and a file that is not a lite store is refused byte-identically rather
than adopted — that refusal is what keeps a mistyped `--db-path` away from an
engine bank.

The default database is `~/.mnemosyne-lite/mnemosyne.db`. Override it with
`--db-path` or `MNEMOSYNE_DB_PATH`; `DATABASE_URL` is honoured only for
`sqlite://`, `sqlite3://` and `file://`, and any other scheme is rejected
loudly instead of becoming a file whose name is a URL. Stores created by older
versions live at `~/.mnemosyne/mnemosyne.db` — point `--db-path` at them or move
the file; nothing is deleted for you.

### MCP over stdio

```bash
MNEMOSYNE_DB_PATH=~/.mnemosyne-lite/mnemosyne.db mnemosyne-lite mcp
```

Newline-delimited JSON-RPC 2.0 on stdin/stdout, logs on stderr. Four tools:
`mnemosyne_memory_search`, `mnemosyne_memory_remember`, `mnemosyne_prefetch`
and `mnemosyne_sync_turn` (`mnemosyne.recall` and `mnemosyne.remember` are
accepted as aliases). A failed call returns `isError: true` rather than an
empty result, so a client can tell "no match" from "store broken". See
[MCP_SERVER.md](MCP_SERVER.md) and [docs/MCP_CLIENT_CONFIGS.md](docs/MCP_CLIENT_CONFIGS.md).

## The real architecture

**The provider** is the vendored `hermes_memory_provider` from
`mnemosyne-memory 3.15.1` — vectors, FTS and graph ranking come from the engine
(`mnemosyne.core.*`), not from this repo. The snapshot is byte-hashed, and the
gates that keep it honest are the point of this repository:

| Concern | Where |
| --- | --- |
| Upstream provenance + per-file hashes | `integrations/hermes-provider/VENDORED_FROM.json` |
| Local patches, each with a reason | `integrations/hermes-provider/PATCHES.md` |
| Upstream contract audit | `integrations/hermes-provider/CONTRACT_AUDIT.md` |
| Drift gate | `tests/test_vendored_provider.py` |
| Loader contract (bare venv and engine present) | `tests/test_provider_loader.py` |
| DB path precedence + doctor checks | `tests/test_provider_db_path.py` |
| Re-vendor procedure | `scripts/vendor-provider-sync.sh` |

Editing a vendored file is a deliberate act: it needs a `# LOCAL PATCH:` marker,
a `PATCHES.md` entry and a `VENDORED_FROM.json` hash update in the same change,
or the drift gate fails.

The provider resolves its database in this order: `memory.mnemosyne.db_path`
(Hermes `config.yaml`) > `MNEMOSYNE_DB_PATH` > the engine default
(`MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` > `~/.hermes`). `db_path` wins over
`profile_isolation`, and `doctor` warns when the store sits outside
`$HERMES_HOME`. The full provider documentation is
[integrations/hermes-provider/README.md](integrations/hermes-provider/README.md).

**The lite store** is one SQLite file with an inline schema, no dependencies
beyond the standard library, and a per-thread connection per thread. Its recall
memo is per-thread for a reason: the version that validates a memo entry is
built from the calling thread's own connection, so a shared memo let one thread
read another's rows. `tests/test_python_hardening.py` and
`tests/test_recall_freshness.py` cover the storage-safety rules; `bench/` holds
the recall-latency harness and the one recorded measurement.

### Repository layout

```text
install.sh                       provider installer (and --uninstall)
integrations/hermes-provider/    the vendored provider + its gates
src/mnemosyne_lite/              the standalone store, CLI and MCP server
tests/                           contract and regression suites
scripts/                         repo gates, the smoke lane, re-vendor
bench/                           recall benchmark harness + its record
docs/                            AGENT_SETUP, HERMES_INTEGRATION, MCP_CLIENT_CONFIGS
```

## Contributing

```bash
./test-all.sh --skip-llm         # provider contract gates + local unit tests
bash scripts/checks.sh           # repo gates (version drift)
pre-commit run --all-files       # ruff, ruff format, mypy, shellcheck
```

`.pre-commit-config.yaml` uses tools on `PATH` rather than pinned hook repos, so
local and CI runs use the same executables. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

- **The engine and the upstream provider** are
  [AxDSan/mnemosyne](https://github.com/AxDSan/mnemosyne) by Abdias J (AxDSan),
  MIT licensed. `integrations/hermes-provider/` is a byte-hashed snapshot of the
  `hermes_memory_provider` package from `mnemosyne-memory 3.15.1`, plus the
  local patches listed in `PATCHES.md`. Upstream is the source of truth; this
  repository owns the Hermes-specific plumbing, not the memory engine.
- **Not related to** [rand/mnemosyne](https://github.com/rand/mnemosyne), a
  different project that shares the name. That is the Rust product this
  repository used to be a rewrite of; it is retired here.
- See [NOTICE](NOTICE) for attribution and [LICENSE](LICENSE) for terms.
