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
| Plugins dir | `$HERMES_HOME/plugins/mnemosyne` → this package |

## Install

Prerequisites: a Hermes install (`$HERMES_HOME`, default `~/.hermes`) and
`uv` (works in pip-less and root-owned venvs, e.g. an `/opt/hermes/.venv`
Docker default).

```bash
# 1. Install the provider AND the engine it imports into the Hermes venv.
uv pip install --python "$HERMES_VENV/bin/python" ./integrations/hermes-provider

# 2. Make Hermes discover it as a memory provider plugin.
#    The symlink must point at the DIRECTORY CONTAINING __init__.py — pointing
#    it at the repo root (as install.sh used to) makes the loader skip it.
ln -sfn "$(pwd)/integrations/hermes-provider/hermes_memory_provider" \
        "$HERMES_HOME/plugins/mnemosyne"

# 3. Point the agent at this provider.
hermes config set memory.provider mnemosyne

# 4. Prove it before trusting it.
hermes mnemosyne doctor --no-fix     # must exit 0 in the gateway venv
```

`scripts/install/…` and `./install.sh` perform the same steps; run them with
`--dry-run` first to see the resolved venv, DB path and symlink target.

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

```
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

Three different paths have been in play, so be explicit:

| Path | What it is |
|---|---|
| `$MNEMOSYNE_DB_PATH` | Explicit override. Wins when set. |
| `~/.hermes/mnemosyne/…` | The Hermes-side store (what the live provider used). |
| `~/.mnemosyne/mnemosyne.db` | Default of the standalone lite CLI (`src/mnemosyne`), **not** the provider's store. |

The provider resolves its own path from Hermes config/`MNEMOSYNE_DB_PATH`; the
lite CLI refuses to run against a missing path rather than creating one. Check
`hermes mnemosyne inspect` / doctor output to confirm which store is live.

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

## Sync policy

Upstream is the source of truth. On each `mnemosyne-memory` release:

```bash
scripts/vendor-provider-sync.sh /path/to/site-packages/hermes_memory_provider
python tests/test_vendored_provider.py
```

Triage per `PATCHES.md`, then re-vendor the changed files and update
`VENDORED_FROM.json` in the same commit. The engine pin
(`mnemosyne-memory[embeddings]>=3.15.1,<3.16`) may only be widened after
re-running the contract audit.
