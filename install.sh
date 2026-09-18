#!/usr/bin/env bash
# Install the canonical Hermes memory provider (vendored, engineer-backed).
#
#   ./install.sh [--dry-run] [--yes] [--venv DIR | --python PATH] [--hermes-home DIR] [--db-path PATH]
#
# What it does, in order:
#   1. resolve the Hermes venv (works for pip-less and root-owned venvs)
#   2. install the vendored provider + the pinned engine with uv
#   3. point $HERMES_HOME/plugins/mnemosyne at the vendored package directory
#   4. set memory.provider=mnemosyne via `hermes config set` (never a blind
#      rewrite of config.yaml)
#   5. verify: engine importable, provider importable, exactly one registration
#
# Nothing here creates or opens the memory database. The resolved DB path is
# printed up front so you can check it before anything is written.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVIDER_SRC="$ROOT/integrations/hermes-provider"
PROVIDER_PKG="$PROVIDER_SRC/hermes_memory_provider"
ENGINE_PIN='mnemosyne-memory[embeddings]>=3.15.1,<3.16'

DRY_RUN=false
ASSUME_YES=false
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV="${HERMES_VENV:-}"
PY_OVERRIDE=""
DB_PATH="${MNEMOSYNE_DB_PATH:-}"

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --dry-run            print the plan; write nothing
  --yes                skip the confirmation prompt (required when not a TTY)
  --venv DIR           Hermes virtualenv (default: autodetect)
  --python PATH        the Hermes venv's python, when you know it exactly
  --hermes-home DIR    Hermes home (default: $HERMES_HOME or ~/.hermes)
  --db-path PATH       memory database path to report (default: env/MNEMOSYNE)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)     DRY_RUN=true; shift ;;
        --yes|-y)      ASSUME_YES=true; shift ;;
        --venv)        VENV="$2"; shift 2 ;;
        --python)      PY_OVERRIDE="$2"; shift 2 ;;
        --hermes-home) HERMES_HOME="$2"; shift 2 ;;
        --db-path)     DB_PATH="$2"; shift 2 ;;
        --help|-h)     usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

venv_python() {
    if [[ -x "$1/bin/python" ]]; then printf '%s' "$1/bin/python"
    elif [[ -x "$1/Scripts/python.exe" ]]; then printf '%s' "$1/Scripts/python.exe"
    else printf '%s' ""
    fi
}

# --- 1. resolve the Hermes venv ---------------------------------------------
# --python wins: that is how a Hermes install knows its own interpreter, and it
# is unambiguous when several venvs exist on the machine.
if [[ -n "$PY_OVERRIDE" ]]; then
    [[ -x "$PY_OVERRIDE" ]] || fail "--python must point at an executable python: $PY_OVERRIDE"
    VENV_PY="$PY_OVERRIDE"
    VENV="$(cd "$(dirname "$PY_OVERRIDE")/.." && pwd)"
else
    if [[ -z "$VENV" ]]; then
        for candidate in \
            "${HERMES_VENV:-}" \
            "/opt/hermes/.venv" \
            "$HERMES_HOME/venv" \
            "${HERMES_INSTALL_DIR:-$HERMES_HOME/hermes-agent}/venv" \
            "/usr/local/lib/hermes-agent/venv" \
            "$(dirname "$(dirname "$(command -v hermes 2>/dev/null || true)")" 2>/dev/null || true)"
        do
            [[ -n "$candidate" && -n "$(venv_python "$candidate")" ]] || continue
            VENV="$candidate"
            break
        done
    fi
fi
[[ -n "$VENV" ]] || fail "could not find the Hermes venv; pass --venv DIR or --python PATH"
if [[ -z "${VENV_PY:-}" ]]; then
    VENV_PY="$(venv_python "$VENV")"
fi
[[ -n "$VENV_PY" ]] || fail "$VENV has no python (looked for bin/python and Scripts/python.exe)"
[[ -d "$PROVIDER_PKG" ]] || fail "vendored provider missing at $PROVIDER_PKG"

# --- 2. work out the resolved DB path BEFORE writing anything ---------------
if [[ -z "$DB_PATH" ]]; then
    DB_PATH="$("$VENV_PY" -c '
from mnemosyne.core.beam import _default_db_path
print(_default_db_path())' 2>/dev/null || true)"
fi
[[ -n "$DB_PATH" ]] || DB_PATH="$HERMES_HOME/mnemosyne/data/mnemosyne.db"

PLUGIN_LINK="$HERMES_HOME/plugins/mnemosyne"
CONFIG_FILE="$HERMES_HOME/config.yaml"

cat <<EOF
Hermes provider install plan
  repo             : $ROOT
  provider source  : $PROVIDER_PKG
  venv             : $VENV
  plugin symlink   : $PLUGIN_LINK -> $PROVIDER_PKG
  config           : $CONFIG_FILE   (memory.provider=mnemosyne)
  memory DB path   : $DB_PATH
                     (nothing is created or opened by this script)
  engine pin       : $ENGINE_PIN
EOF

if [[ "$DRY_RUN" == true ]]; then
    echo
    echo "--dry-run: no changes made."
    exit 0
fi

if [[ "$ASSUME_YES" != true ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Proceed? [y/N]: " reply
        [[ "$reply" =~ ^[Yy] ]] || { echo "Cancelled; nothing changed."; exit 1; }
    else
        fail "refusing to install without confirmation (pass --yes, or --dry-run to inspect)"
    fi
fi

if [[ -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]]; then
    fail "$PLUGIN_LINK exists and is not a symlink; remove it first"
fi

# --- 3. install provider + engine -------------------------------------------
if command -v uv >/dev/null 2>&1; then
    INSTALL=(uv pip install --python "$VENV_PY")
elif "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    # Kept as a fallback; uv is preferred because it also works in the
    # pip-less/root-owned venvs used by the Docker installs.
    INSTALL=("$VENV_PY" -m pip install)
else
    fail "neither uv nor pip is usable for $VENV; install uv (https://docs.astral.sh/uv/)"
fi

echo "== Installing the vendored provider and the pinned engine"
"${INSTALL[@]}" "$PROVIDER_SRC"
"${INSTALL[@]}" "$ENGINE_PIN"

# --- 3b. engine must stay the engine ----------------------------------------
# The engine owns the `mnemosyne` package. Two things can still break it here:
# this repo's lite distribution was once named `mnemosyne` too (it is
# `mnemosyne-lite`/`mnemosyne_lite` now, so a fresh install cannot collide), and
# any other `mnemosyne` distribution would shadow the engine's package. Check
# rather than assume.
if ! "$VENV_PY" -c 'import mnemosyne.core.beam' >/dev/null 2>&1; then
    fail "the engine is not importable in $VENV after install (need $ENGINE_PIN).
       Check for a shadowing 'mnemosyne' package in this venv:
         $VENV_PY -m pip list | grep -i mnemosyne"
fi
ENGINE_FILE="$("$VENV_PY" -c 'import mnemosyne, os; print(os.path.realpath(mnemosyne.__file__))')"
case "$ENGINE_FILE" in
    "$ROOT"/src/*) fail "the engine import resolved to this repo ($ENGINE_FILE) instead of the
       installed engine; uninstall whatever put src/ ahead of site-packages in $VENV" ;;
esac

# --- 4. plugin discovery ----------------------------------------------------
echo "== Linking the plugin directory"
mkdir -p "$(dirname "$PLUGIN_LINK")"
rm -f "$PLUGIN_LINK"
ln -sfn "$PROVIDER_PKG" "$PLUGIN_LINK"
[[ -f "$PLUGIN_LINK/__init__.py" ]] || fail "the symlink target has no __init__.py; the Hermes memory
       provider loader would skip it (it requires __init__.py with register_memory_provider)"

# --- 5. config --------------------------------------------------------------
echo "== Selecting the provider"
if command -v hermes >/dev/null 2>&1; then
    hermes config set memory.provider mnemosyne
elif [[ ! -f "$CONFIG_FILE" ]]; then
    mkdir -p "$(dirname "$CONFIG_FILE")"
    printf 'memory:\n  provider: mnemosyne\n' > "$CONFIG_FILE"
    note "created $CONFIG_FILE with memory.provider=mnemosyne"
else
    note "NOTE: 'hermes' is not on PATH and $CONFIG_FILE already exists — this script"
    note "      does not rewrite config files. Add under the existing memory: block:"
    note "          provider: mnemosyne"
fi

# --- 6. verify --------------------------------------------------------------
echo "== Verifying"
"$VENV_PY" - "$PLUGIN_LINK" <<'PY'
import importlib.util, os, sys
provider_dir = os.path.realpath(sys.argv[1])
box = []
spec = importlib.util.spec_from_file_location(
    "_hermes_user_memory.mnemosyne", os.path.join(provider_dir, "__init__.py"),
    submodule_search_locations=[provider_dir])
mod = importlib.util.module_from_spec(spec)
sys.modules["_hermes_user_memory.mnemosyne"] = mod
spec.loader.exec_module(mod)

class Ctx:
    def register_memory_provider(self, p): box.append(p)
    def register_cli_command(self, **kw): pass
    def register_tool(self, *a, **k): pass
    def register_hook(self, *a, **k): pass

mod.register(Ctx())
assert len(box) == 1, f"expected exactly one provider, got {len(box)}"
p = box[0]
assert p.name == "mnemosyne", p.name
if not p.is_available():
    print(f"unavailable: {p.unavailable_reason()}", file=sys.stderr)
    sys.exit(1)
print(f"provider registered: {p.name} (available)")
PY

cat <<EOF

Installed.

Next:
  hermes mnemosyne doctor --no-fix     # must exit 0
  hermes mnemosyne stats
  # restart the gateway: provider code is cached per process, so a running
  # gateway keeps executing the old module until it is restarted.
Database (created on first write, not by this script): $DB_PATH
EOF
