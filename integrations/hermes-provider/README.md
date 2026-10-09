# Hermes memory provider (canonical)

This directory holds **the** Hermes memory provider for this repository: a
vendored, byte-identical snapshot of `hermes_memory_provider` as shipped by
`mnemosyne-memory 3.15.1`, plus the plumbing that keeps it honest.

It is the only provider this repo registers. Provider id: **`mnemosyne`**.
There is no second entry point and no second provider implementation — the
previous slim `lib.storage`-backed provider in `integrations/hermes/` is retired
(it had keyword-only recall and is not shipped as a provider).

| | |
|---|---|
| Provider id | `mnemosyne` (`memory.provider: mnemosyne`) |
| Import name | `hermes_memory_provider` |
| Source | `mnemosyne-memory 3.15.1` (MIT, © Abdias J / AxDSan) |
| Provenance + hashes | `VENDORED_FROM.json` |
| Local patches + sync policy | `PATCHES.md` |
| Contract audit | `CONTRACT_AUDIT.md` |
| Plugins dir | `$HERMES_HOME/plugins/mnemosyne` → this package (symlink or verified copy) |

## Background consolidation

Auto-sleep starts at most one background consolidation worker per provider,
without waiting in turn sync. Session-end reuses that worker and keeps its
bounded wait (`MNEMOSYNE_SESSION_END_TIMEOUT`, default 15 seconds). The worker
captures the active session, database, author/channel and canonical owner before
starting, then owns and closes an independent SQLite connection. The audited
engine coordinates database operations with foreground access while releasing
the shared lock for model inference; foreground tool/turn connection access
remains serialized. See [P22](PATCHES.md) and the
[Honcho comparison](../../docs/HONCHO_RESEARCH.md).

Shutdown stops new workers and waits up to `MNEMOSYNE_SHUTDOWN_DRAIN_TIMEOUT`
(default 2 seconds). An overrun keeps the inherited host LLM backend available
until the worker finishes; cleanup cannot clear a later provider's registration.
Reinitialization is refused while consolidation is active. A daemon worker can
still be interrupted by process exit; this is not a durable job queue, and the
engine's existing additive consolidation/claim semantics remain in effect.
`MNEMOSYNE_AUTO_SLEEP_TIMEOUT` no longer controls turn latency because auto-sleep
does not join its worker. Re-run the installer for the audited engine patch and
restart the gateway after updating the provider.

## Install

Prerequisites: a Hermes install (`$HERMES_HOME`, default `~/.hermes`) and
`uv` (works in pip-less and root-owned venvs, e.g. an `/opt/hermes/.venv`
Docker default).

```bash
# 1. Install the provider AND the engine it imports into the Hermes venv.
uv pip install --python "$HERMES_VENV/bin/python" ./integrations/hermes-provider
"$HERMES_VENV/bin/python" scripts/apply_engine_patches.py

# 2. Make Hermes discover it as a memory provider plugin.
#    A symlink must point at the directory containing __init__.py.
ln -sfn "$(pwd)/integrations/hermes-provider/hermes_memory_provider" \
        "$HERMES_HOME/plugins/mnemosyne"
#    If directory symlinks are unavailable (e.g. Windows without Developer
#    Mode), use ./install.sh --copy to install a digest-verified copy instead.

# 3. Point the agent at this provider.
hermes config set memory.provider mnemosyne

# 4. Prove it before trusting it.
hermes mnemosyne doctor --no-fix     # must exit 0 in the gateway venv
```

`./install.sh` performs these same steps; run it with `--dry-run` first to see
the resolved venv, DB path and plugin target. It automatically falls back to a
copy if directory symlinks are unavailable; `--copy` forces that mode. Copies
carry format-v1 `PROVENANCE.json` with SHA-256 digests for every package file
(except the `__pycache__` directories Python creates at runtime and the root
`PROVENANCE.json` marker). Top-level `.pyc` files and nested marker-named files
are included. `doctor` rejects missing,
changed, missing/extra, legacy, or symlinked payload files. The installer also
refuses to overwrite or uninstall a directory unless its complete inventory
verifies against the current canonical source path. Re-run the installer after
updating a copied provider because the copy does not track the source; older
unversioned copies need to be moved aside or removed manually before upgrade.

**Do not install upstream's bundled provider alongside this one.** The vendored
copy *is* the `mnemosyne` provider, and two registered paths for the same id is
the failure mode this directory exists to remove. If `mnemosyne-memory` is
installed as a dependency (it is, for the engine), ignore its
`hermes_memory_provider/` copy for plugin discovery.

**Do not install this repository's lite surface into the Hermes venv.** The
engine owns the `mnemosyne` distribution name, import package and console
script. The lite surface used to be named `mnemosyne` too, and pip merges
same-named packages instead of refusing, so a wheel install overwrote exactly two
engine files — `mnemosyne/__init__.py` and `mnemosyne/cli.py` (measured by
copying those two files over a copy of the engine tree):

```text
import mnemosyne.core.beam        # still works — the engine's core/ directory survives
from mnemosyne import Mnemosyne   # ImportError: cannot import name 'Mnemosyne' from 'mnemosyne'
mnemosyne.__version__             # the lite package's version, not the engine's
# Scripts/mnemosyne is now the lite CLI instead of the engine's run_cli
```

That is fixed: the lite surface is now `mnemosyne-lite` / `mnemosyne_lite`
(`pyproject.toml`, `src/mnemosyne_lite`), so a fresh install cannot collide. The
hazard remains for any *other* `mnemosyne` distribution, and for putting `src/`
on `sys.path` ahead of the engine (`import mnemosyne.core` then raises
`ModuleNotFoundError`). Install the lite surface in its own virtualenv.
`install.sh` verifies the engine import afterwards and refuses rather than
leaving a broken venv.

## Database paths

The provider resolves its DB path in this order (first match wins):

| Order | Source | Notes |
|---|---|---|
| 1 | `memory.mnemosyne.db_path` (Hermes `config.yaml`) | Per-install override |
| 2 | `MNEMOSYNE_DB_PATH` env var | Preserved contract; read by the provider and the CLI |
| 3 | engine default `_default_db_path()` | `MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` > `~/.hermes`, then `mnemosyne/data/mnemosyne.db` |

There is no separate `$MNEMOSYNE_DATA_DIR` fallback *after* the env var — it
only moves the engine default. If the resolved DB sits outside `$HERMES_HOME`,
the provider logs a warning at init and `hermes mnemosyne doctor` reports it,
because logs and memory are then split across two roots.

`db_path` wins over `profile_isolation` (per-profile banks); the provider warns
when both are set. The standalone lite surface uses `~/.mnemosyne-lite/mnemosyne.db`
— that is **not** the provider's store. Check `hermes mnemosyne inspect` or
`doctor` to confirm which store is live.

## Inspect evidence and recorded history

```bash
hermes mnemosyne inspect --id <memory-id> --session-id <engine-session-id>
hermes mnemosyne history --id <memory-id> --session-id <engine-session-id> --format text
hermes mnemosyne export --format markdown --session-id <engine-session-id> --output evidence.md
```

By-ID inspection, history, and Markdown export use an existing engine store
through read-only SQLite connections. They refuse missing, foreign and corrupt
stores without creating tables. The existing positional `inspect <query>` and
whole-store JSON export keep their behavior. New reads select the given engine
session plus global rows; the default session is `hermes_default`. Provider
sessions commonly have a `hermes_` prefix, so use the stored engine session ID
reported by `mnemosyne_stats`, rather than assuming it is the host transcript ID.
`--all-sessions` explicitly selects the local operator's broader view. These
local CLI selectors are filters, not authentication. `history --profile` only
narrows matching event labels; it does not select a different database.

History is **partial recorded history**, not a complete revision log or undo
facility. Remember, successful update/forget/invalidate, and private validation
events retain known target ownership, including after deletion. Legacy events
with missing ownership are excluded from session views. Other capture and
background operations may remain uncovered. Writes are best effort with a
100 ms SQLite busy wait, per-operation connections and sanitized health counters
available through `get_audit_diagnostics()` and the existing full-surface
`mnemosyne_diagnose` tool. Failed history reads report an error.

Turn capture attaches a `source_ref` containing the host session and exact
message ID only when the full message list supplies one unique matching
message. Older hosts or ambiguous messages omit it. Explicit remember metadata
can supply `source_ref: {session_id, message_id}`; it is labeled `caller` rather
than `host`. References do not verify what a message says, and no transcript
lookup is supplied by these commands. Identity captures retain eligible refs.
Imported or externally edited metadata can contain an asserted `host` label;
the inspection view cannot authenticate those labels. Provider tool writes
normalize caller-supplied origins to `caller`, including batch and pending writes.
Inspection includes `origin_authenticated: false`; Markdown labels the origin
as a stored claim, including references captured through the host callback.
The engine's summary rows retain lineage separately; inspection and export show
only original IDs eligible for the selected session, with unavailable evidence
marked. Consolidation does not automatically copy all source metadata.

Inspection caps content at 4,000 characters. History limits events to 100 per
call and supports JSON (default) or text. Markdown export caps rows at 200,
each content body at 4,000 characters, its index at 8,000 characters and each
lineage list at 20 IDs. Omission/truncation is visible. It produces a single
read-only projection with escaped Markdown and stable IDs, without Git writes
or import synchronization. Output must be a new file in an existing directory;
database/WAL/SHM/journal targets and existing outputs are refused. Existing JSON export
remains the full engine format.

The snapshot bounds legacy metadata and lineage text before loading them into
Python (65,536 characters each). Oversized metadata yields unavailable evidence;
oversized lineage is explicitly marked incomplete. Unknown scopes are excluded.

## Fail loud, never hollow

The provider imports the engine (`mnemosyne.core.*`, `mnemosyne.batch_tool`,
`mnemosyne.hermes_config`, `mnemosyne.integrations.hermes_persona_prompt`). If
the engine is missing, the provider must say so instead of reporting
`available ✓` and then silently no-op-ing every prefetch/sync/tool call:

- `is_available()` returns False **and** exposes the reason.
- `hermes mnemosyne doctor --no-fix` exits non-zero when the engine is missing.
- `tests/test_provider_loader.py` covers both the bare (no engine) and the
  installed case.

## Gateway restart rule

Hermes caches a loaded provider module in `sys.modules` by name for the life of
the process. After changing provider code you must **restart the gateway**;
a fresh one-shot CLI probe will look healthy while the running gateway is still
executing the old module. Symptom of a stale process: a fix "works in the CLI"
but not in the live session.

## Supported Hermes range

`hermes mnemosyne` depends on Hermes' plugin CLI discovery internals, so the
supported range is a contract: **`>=0.18,<0.20`**, audited against the two
published Hermes releases in the matrix: `0.18.2` and `0.19.0`. The provider
logs a warning — it does not refuse to start — when the detected `hermes-agent`
version is outside that range, and `doctor` prints the detected version beside
the range. Versions outside the audited range are unsupported until their
plugin-discovery and provider contracts are reviewed.

## Uninstall

Native Windows also has [`install.ps1`](../../install.ps1): use `-DryRun` to
inspect, `-Yes` to install, and `-Uninstall` with optional `-Purge` to remove.
It uses a provenance-verified copy. Newer Hermes PM installations admit the
plugin's declared pinned dependencies through `hermes pm install`; the legacy
manual uv instructions above apply to unmanaged environments.

Automatic context is capped at 8,000 characters by default and excludes raw
tool/delegation transcripts. Exact-query warming/cache is experimental and
opt-in. Native memory corrections retire only explicitly owned mirrors.
Advanced controls and the version boundary for newer hooks are documented in
the [configuration reference](../../docs/HERMES_CONFIGURATION.md); verification
includes the [public-prefetch evaluation](../../bench/HERMES_PREFETCH_RESULTS.md).

```bash
./install.sh --uninstall          # remove provider link/package; keep engine and data
./install.sh --uninstall --purge  # also remove engine + $HERMES_HOME/mnemosyne
```

The installer prompts before changing anything (or pass `--yes` explicitly).
`--purge` removes the default Hermes-home data directory; an explicitly
configured database outside that directory is not removed. Back up data and
check the resolved path before purging. Restart the gateway afterwards.

## Sync policy

Upstream is the source of truth. On each `mnemosyne-memory` release:

```bash
scripts/vendor-provider-sync.sh /path/to/site-packages/hermes_memory_provider
python tests/test_vendored_provider.py
```

Triage per `PATCHES.md`, then re-vendor the changed files and update
`VENDORED_FROM.json` in the same commit. The engine pin
(`mnemosyne-memory[embeddings]>=3.15.1,<3.16`) may only be widened after
re-running the contract audit. A re-vendor or pin change must pass
`./test-all.sh --require-engine` (the engine-backed contract lane:
every tool against the real engine, never skips when strict).
