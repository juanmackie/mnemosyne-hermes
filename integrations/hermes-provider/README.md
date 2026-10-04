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
