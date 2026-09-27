# Troubleshooting

Two products ship here and they fail differently:

- the **Hermes provider** (provider id `mnemosyne`, installed by `./install.sh`)
- the **lite surface** (`mnemosyne-lite` CLI and its MCP stdio server)

Neither one needs a cloud API key to store or search memory. If a step here
tells you to set one, that is a bug — report it.

## Hermes provider

### `hermes mnemosyne` is not a known command

The provider is not loaded. Either it is not installed, or the running gateway
was started before the install.

```bash
./install.sh --dry-run    # shows the venv, symlink target and DB path
./install.sh              # install provider + engine, link/copy the plugin
hermes memory status      # provider installed / available / active
```

Restart the gateway after installing or updating provider code: Hermes caches
the loaded module in `sys.modules` for the life of the process, so a running
gateway keeps executing the old code while a fresh CLI probe looks healthy.

### `hermes mnemosyne doctor` exits non-zero

Read the first `FAIL` line. The most common cause is a missing engine import in
the Hermes venv: the provider imports `mnemosyne.core.*` and friends, and
deliberately reports itself unavailable instead of pretending to work.

```bash
hermes mnemosyne doctor --no-fix     # diagnose only
hermes mnemosyne doctor --dry-run    # show the fixes without applying them
hermes mnemosyne doctor              # diagnose and fix
```

`./install.sh` installs the provider and the pinned engine together; re-run it
rather than hand-installing one half.

### The provider loads but is unavailable

Check the plugin entry at `$HERMES_HOME/plugins/mnemosyne`. A symlink must
point at the directory containing `__init__.py`, not at the repository root. On
systems where directory symlinks are unavailable, the installer falls back to
a copy; run `./install.sh --copy` to force that mode.

```bash
ls -ld "$HERMES_HOME/plugins/mnemosyne"
# symlink: resolves to integrations/hermes-provider/hermes_memory_provider
# copy: contains PROVENANCE.json
hermes mnemosyne doctor --no-fix
```

A copied install is accepted only while format-v1 provenance covers every
package file (excluding runtime bytecode caches and the marker) and the exact
inventory and SHA-256 digests still match. Re-run the installer after editing a
copy. The installer refuses to overwrite/uninstall an unverified directory;
legacy unversioned copies need to be moved aside or removed manually. Hermes
0.18.2/0.19.0 discovery
prefers a bundled provider over a same-name user plugin and deduplicates that
name; the clean-user smoke tests this collision and one-provider registration.
Do not add another manual registration entry point.

### Memory is written somewhere unexpected

Print the resolved path and pin it if needed.

```bash
hermes mnemosyne doctor --no-fix   # prints the resolved DB path
hermes mnemosyne stats             # counts, per session
```

Path precedence is `memory.mnemosyne.db_path` > `MNEMOSYNE_DB_PATH` > engine
default. The provider warns, and `doctor` reports it, when the store sits outside
`$HERMES_HOME`, because logs and memory are then split across two roots. The lite
surface's store (`~/.mnemosyne-lite/mnemosyne.db`) is a different file — seeing
one and not the other is expected.

### Version warning on startup

The provider warns — it does not refuse to start — when the installed
`hermes-agent` is outside the range it was tested against. `doctor` prints the
detected version beside the range. Update Hermes or the provider to move back
inside it.

### A tool returns `memory_unavailable`

The engine import failed after startup. The response carries the reason; the
fixes are the same as for a non-zero `doctor`.

## Lite surface

### `no Mnemosyne database at <path> (nothing was created)`

Read and maintenance commands refuse to invent a store, so a mistyped path
cannot answer "0 memories". Create it with `init` or `remember`, restore it from
an explicit backup, or point the command at an existing store:

```bash
mnemosyne-lite init
mnemosyne-lite --db-path /path/to/mnemosyne.db list
```

`mnemosyne-lite diagnostics` prints the resolved path even when the file is
missing — it is the command to run when the store is suspect.

### `Unsupported DATABASE_URL scheme 'postgres'`

Storage is SQLite-only. `DATABASE_URL` is accepted for the `sqlite`, `sqlite3`
and `file` schemes (`sqlite:///var/lib/mnemosyne.db` and a plain path both work);
anything else is rejected loudly rather than used to create a file named after
the URL.

### The store is refused as foreign or unreadable

The path holds a database this project did not write — for example the provider's
engine store, which has a different schema. Use a path you created with
`mnemosyne-lite init`, or a backup made by `mnemosyne-lite backup`.

### The MCP server exits immediately

It serves an **existing** store and never creates one, so a wrong or missing
path fails at startup instead of answering every request with an error. Run
`mnemosyne-lite init` first, or set `MNEMOSYNE_DB_PATH` / `--db-path` at the
client. Nothing but JSON-RPC goes to stdout, so a client that logs stray output
will show it as a protocol error.

### I need structured output

Pass `--format json` (before or after the subcommand). `init`, `remember`,
`bootstrap`, `backup`, `restore`, `maintenance` and `diagnostics` print one JSON
object; `recall` and `list` print one JSON array. Text mode preserves existing
command-specific output.

```bash
mnemosyne-lite recall --query "race condition" --format json | jq -r '.[].content'
```

### `restore` did not do what I expected

`restore` replaces the file at `--db-path` (default `~/.mnemosyne-lite/mnemosyne.db`),
asks for confirmation unless `--yes` is given, writes a
`<db>.pre-restore.<timestamp>` copy first, and stages the new file before moving
it into place. A refused or truncated backup leaves the old store untouched; look
for the pre-restore copy if you want to go back.

### `maintenance --auto-apply` deleted nothing

With no `--yes`, it asks before deleting exactly-identical duplicates, and a
closed or piped stdin counts as "no". Near-duplicate proposals are only reported.

### `mnemosyne-lite: command not found`

It is the console script of this repository's package, not the engine's
`mnemosyne` script:

```bash
pip install -e .
python -m mnemosyne_lite.cli --help
```

Keep the lite surface out of the Hermes virtualenv: install it in its own
environment. The name collision that used to break the engine is described in
[integrations/hermes-provider/README.md](integrations/hermes-provider/README.md).

## Repository checks

```bash
./test-all.sh --skip-llm     # provider contract gates + local pytest suite
bash scripts/checks.sh       # notes and version-drift gates
pre-commit run --all-files   # ruff, mypy, shellcheck
```

`pre-commit` uses tools on `PATH`, not pinned downloads: install `ruff`,
`mypy`, `shellcheck-py` and `pre-commit` first, or the hooks fail with "command
not found". `scripts/checks.sh` runs the gates listed in
`tests/` and `scripts/`; if one is missing from that runner it is invisible in CI.

## Getting help

Open an issue at <https://github.com/juanmackie/mnemosyne-hermes/issues> and use
the bug report template — it asks for the version, the platform, the install
method and the output that made you look here. Redact paths and any credentials
before pasting output.
