#!/usr/bin/env bash
# Install or remove the canonical Hermes memory provider (vendored, engine-backed).
#
#   ./install.sh [--dry-run] [--yes] [--venv DIR | --python PATH] [--hermes-home DIR] [--db-path PATH]
#   ./install.sh --uninstall [--purge] [--yes] [--venv DIR | --python PATH] [--hermes-home DIR]
#
# What it does, in order:
#   1. resolve the Hermes venv (works for pip-less and root-owned venvs)
#   2. install the vendored provider + the pinned engine with uv
#   3. install $HERMES_HOME/plugins/mnemosyne as a symlink or verified copy
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
UNINSTALL=false
PURGE=false
COPY_MODE=false
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV="${HERMES_VENV:-}"
PY_OVERRIDE=""
DB_PATH="${MNEMOSYNE_DB_PATH:-}"

usage() {
  cat <<'EOF'
Install or remove the canonical Hermes memory provider (vendored, engine-backed).

  ./install.sh [--dry-run] [--yes] [--copy] [--venv DIR | --python PATH]
               [--hermes-home DIR] [--db-path PATH]
  ./install.sh --uninstall [--purge] [--yes] [--venv DIR | --python PATH] [--hermes-home DIR]

Install, in order:
  1. resolve the Hermes venv (works for pip-less and root-owned venvs)
  2. install the vendored provider + the pinned engine with uv
  3. install $HERMES_HOME/plugins/mnemosyne as a symlink or verified copy
  4. set memory.provider=mnemosyne via `hermes config set` (never a blind
     rewrite of config.yaml)
  5. verify: engine importable, provider importable, exactly one registration

Nothing here creates or opens the memory database. The resolved DB path is
printed up front so you can check it before anything is written.

Uninstall removes the plugin link/copy and provider package. Memory is kept
unless --purge is also given, which drops the engine package and the
$HERMES_HOME/mnemosyne data directory as well.

Options:
  --dry-run            print the plan; write nothing
  --yes                skip the confirmation prompt (required when not a TTY)
  --uninstall          remove the provider instead of installing it
  --purge              with --uninstall: also remove the engine and the data dir
  --copy               force a copy instead of a directory symlink. Useful on
                       Windows without Developer Mode; doctor verifies all package files
  --venv DIR           Hermes virtualenv (default: autodetect)
  --python PATH        the Hermes venv's python, when you know it exactly
  --hermes-home DIR    Hermes home (default: $HERMES_HOME or ~/.hermes)
  --db-path PATH       memory database path to report (default: env/MNEMOSYNE)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --dry-run)
    DRY_RUN=true
    shift
    ;;
  --yes | -y)
    ASSUME_YES=true
    shift
    ;;
  --uninstall)
    UNINSTALL=true
    shift
    ;;
  --purge)
    PURGE=true
    shift
    ;;
  --copy)
    COPY_MODE=true
    shift
    ;;
  --venv)
    VENV="$2"
    shift 2
    ;;
  --python)
    PY_OVERRIDE="$2"
    shift 2
    ;;
  --hermes-home)
    HERMES_HOME="$2"
    shift 2
    ;;
  --db-path)
    DB_PATH="$2"
    shift 2
    ;;
  --help | -h)
    usage
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    usage >&2
    exit 2
    ;;
  esac
done
export HERMES_HOME

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
note() { printf '  %s\n' "$*"; }

PLUGIN_LINK="$HERMES_HOME/plugins/mnemosyne"
CONFIG_FILE="$HERMES_HOME/config.yaml"

# Record every copied package file so doctor and installer can reject drift,
# including edits to helper modules or plugin metadata. Runtime bytecode caches
# and only the root marker are excluded; Hermes creates the caches after install.
write_provenance() {
  "$VENV_PY" - "$1" "$2" <<'PROVEOF'
import base64
import hashlib
import json
import os
import pathlib
import sys
import time

dest, src = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])


def digest(path):
    data = hashlib.sha256(path.read_bytes()).digest()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


files = {}
for current, directories, names in os.walk(dest, followlinks=False):
    current_path = pathlib.Path(current)
    if any((current_path / name).is_symlink() for name in directories):
        raise SystemExit("provider copy contains a symlinked directory")
    directories[:] = [name for name in directories if name != "__pycache__"]
    for name in names:
        path = current_path / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"provider copy contains a non-regular file: {path}")
        relative = path.relative_to(dest).as_posix()
        if relative == "PROVENANCE.json":
            continue
        files[relative] = digest(path)

payload = {
    "format_version": 1,
    "copied_from": str(src.resolve()),
    "copied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "hash_algorithm": "sha256, base64url without padding (wheel RECORD format)",
    "files": files,
}
(dest / "PROVENANCE.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PROVEOF
}

verify_installed_copy() {
  "$VENV_PY" - "$PROVIDER_PKG/cli.py" "$1" "$PROVIDER_PKG" <<'VERIFYEOF'
import importlib.util
import pathlib
import sys

module_path, plugin_path, expected_source = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("_mnemosyne_copy_verifier", module_path)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load the provider copy verifier")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ok, detail = module._verified_copy(plugin_path, expected_source=expected_source)
if not ok:
    print(detail, file=sys.stderr)
    raise SystemExit(1)
VERIFYEOF
}

distribution_status() {
  "$VENV_PY" - "$1" <<'DISTEOF'
from importlib.metadata import PackageNotFoundError, distribution
import sys

try:
    distribution(sys.argv[1])
except PackageNotFoundError:
    raise SystemExit(2)
DISTEOF
}

uninstall_package() {
  local package="$1"
  local status=0
  distribution_status "$package" || status=$?
  case "$status" in
  0) ;;
  2)
    note "$package is not installed in $VENV; skipping"
    return 0
    ;;
  *) fail "could not inspect $package in $VENV (status $status)" ;;
  esac

  if command -v uv >/dev/null 2>&1; then
    uv pip uninstall --python "$VENV_PY" "$package" >/dev/null
  elif "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    "$VENV_PY" -m pip uninstall -y "$package" >/dev/null
  else
    fail "neither uv nor pip is usable for $VENV"
  fi

  distribution_status "$package" && fail "$package still appears installed in $VENV after uninstall"
  status=$?
  [[ "$status" == 2 ]] || fail "could not verify removal of $package from $VENV (status $status)"
  note "uninstalled $package from $VENV"
}

safe_purge_target() {
  "$VENV_PY" - "$1" <<'PURGEEOF'
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
anchor = Path(target.anchor)
if target == anchor or target.parent == anchor:
    raise SystemExit(1)
PURGEEOF
}

venv_python() {
  if [[ -x "$1/bin/python" ]]; then
    printf '%s' "$1/bin/python"
  elif [[ -x "$1/Scripts/python.exe" ]]; then
    printf '%s' "$1/Scripts/python.exe"
  else
    printf '%s' ""
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
      "$(dirname "$(dirname "$(command -v hermes 2>/dev/null || true)")" 2>/dev/null || true)"; do
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

# --- uninstall: the inverse of install, memory kept unless --purge -------
if [[ "$UNINSTALL" == true ]]; then
  [[ -n "$DB_PATH" ]] || DB_PATH="$HERMES_HOME/mnemosyne/data/mnemosyne.db"
  DATA_ROOT="$HERMES_HOME/mnemosyne"
  if [[ -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]] && ! verify_installed_copy "$PLUGIN_LINK"; then
    fail "refusing to remove $PLUGIN_LINK: it is not an intact copy created by this installer"
  fi
  cat <<EOF
Hermes provider uninstall plan
  repo             : $ROOT
  venv             : $VENV
  plugin link      : $PLUGIN_LINK   (removed)
  provider package : mnemosyne-hermes-provider   (uninstalled)
  memory DB path   : $DB_PATH
EOF
  if [[ "$PURGE" == true ]]; then
    echo "  --purge          : ALSO removes the engine package and $DATA_ROOT"
  else
    echo "  memory data      : kept (pass --purge to remove the engine and $DATA_ROOT)"
  fi

  if [[ "$DRY_RUN" == true ]]; then
    echo
    echo "--dry-run: no changes made."
    exit 0
  fi
  if [[ "$PURGE" == true ]] && ! safe_purge_target "$DATA_ROOT"; then
    fail "refusing to remove '$DATA_ROOT'; remove it yourself"
  fi
  if [[ "$ASSUME_YES" != true ]]; then
    if [[ -t 0 ]]; then
      read -r -p "Proceed? [y/N]: " reply
      [[ "$reply" =~ ^[Yy] ]] || {
        echo "Cancelled; nothing changed."
        exit 1
      }
    else
      fail "refusing to uninstall without confirmation (pass --yes, or --dry-run to inspect)"
    fi
  fi

  # A real directory passed the complete-copy/source check above, so it is
  # safe to remove only after the explicit confirmation step.
  if [[ -L "$PLUGIN_LINK" ]]; then
    rm -f "$PLUGIN_LINK"
    note "removed plugin link $PLUGIN_LINK"
  elif [[ -e "$PLUGIN_LINK" ]]; then
    verify_installed_copy "$PLUGIN_LINK" || fail "plugin copy changed after preflight; refusing to remove it"
    rm -rf "$PLUGIN_LINK"
    note "removed verified plugin copy $PLUGIN_LINK (installed by --copy)"
  else
    note "no plugin entry at $PLUGIN_LINK"
  fi

  uninstall_package mnemosyne-hermes-provider

  if [[ "$PURGE" == true ]]; then
    uninstall_package mnemosyne-memory
    safe_purge_target "$DATA_ROOT" || fail "refusing to remove '$DATA_ROOT'; remove it yourself"
    if [[ -d "$DATA_ROOT" ]]; then
      rm -rf "$DATA_ROOT"
      note "removed data directory $DATA_ROOT"
    else
      note "no data directory at $DATA_ROOT"
    fi
  fi

  if [[ -f "$CONFIG_FILE" ]] && grep -qE 'provider:[[:space:]]*mnemosyne' "$CONFIG_FILE"; then
    note "NOTE: $CONFIG_FILE still selects memory.provider=mnemosyne."
    note "      Point it elsewhere (or clear it) before restarting Hermes."
  fi

  echo
  echo "Uninstalled. Restart the gateway so it stops running the provider module."
  exit 0
fi

[[ -d "$PROVIDER_PKG" ]] || fail "vendored provider missing at $PROVIDER_PKG"

# --- 2. work out the resolved DB path BEFORE writing anything ---------------
if [[ -z "$DB_PATH" ]]; then
  DB_PATH="$("$VENV_PY" -c '
from mnemosyne.core.beam import _default_db_path
print(_default_db_path())' 2>/dev/null || true)"
fi
[[ -n "$DB_PATH" ]] || DB_PATH="$HERMES_HOME/mnemosyne/data/mnemosyne.db"

if [[ "$COPY_MODE" == true ]]; then
  PLUGIN_INSTALL_MODE="copy (forced)"
else
  PLUGIN_INSTALL_MODE="symlink with copy fallback"
fi

cat <<EOF
Hermes provider install plan
  repo             : $ROOT
  provider source  : $PROVIDER_PKG
  venv             : $VENV
  plugin install   : $PLUGIN_LINK ($PLUGIN_INSTALL_MODE)
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
    [[ "$reply" =~ ^[Yy] ]] || {
      echo "Cancelled; nothing changed."
      exit 1
    }
  else
    fail "refusing to install without confirmation (pass --yes, or --dry-run to inspect)"
  fi
fi

# Never replace a real directory based only on a user-editable marker file.
# Validate its complete inventory and hashes before removing it later.
if [[ -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]] && ! verify_installed_copy "$PLUGIN_LINK"; then
  fail "$PLUGIN_LINK exists but is not an intact installer-created copy; refusing to overwrite it"
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
echo "== Installing the plugin directory"
mkdir -p "$(dirname "$PLUGIN_LINK")"
if [[ -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]] && ! verify_installed_copy "$PLUGIN_LINK"; then
  fail "$PLUGIN_LINK changed after preflight; refusing to overwrite it"
fi
rm -rf "$PLUGIN_LINK"
if [[ "$COPY_MODE" != true ]]; then
  ln -s "$PROVIDER_PKG" "$PLUGIN_LINK" 2>/dev/null || true
fi
PLUGIN_REAL="$("$VENV_PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PLUGIN_LINK" 2>/dev/null || true)"
PROVIDER_REAL="$("$VENV_PY" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PROVIDER_PKG" 2>/dev/null || true)"
if [[ "$COPY_MODE" == true ]] || [[ -z "$PLUGIN_REAL" || "$PLUGIN_REAL" != "$PROVIDER_REAL" ]]; then
  # Asked for, or the link did not take: Windows cannot create a directory
  # symlink without Developer Mode, and some filesystems refuse them. Copy
  # instead and record the digests, which is what doctor verifies.
  rm -rf "$PLUGIN_LINK"
  cp -R "$PROVIDER_PKG" "$PLUGIN_LINK"
  write_provenance "$PLUGIN_LINK" "$PROVIDER_PKG"
  note "installed a COPY at $PLUGIN_LINK (no symlink support here)"
  note "  re-run this script after changing provider code: a copy does not track it"
else
  note "linked $PLUGIN_LINK -> $PROVIDER_PKG"
fi
[[ -f "$PLUGIN_LINK/__init__.py" ]] || fail "$PLUGIN_LINK has no __init__.py; the Hermes memory
       provider loader would skip it (it requires __init__.py with register_memory_provider)"

# --- 5. config --------------------------------------------------------------
echo "== Selecting the provider"
if command -v hermes >/dev/null 2>&1; then
  hermes config set memory.provider mnemosyne
elif [[ ! -f "$CONFIG_FILE" ]]; then
  mkdir -p "$(dirname "$CONFIG_FILE")"
  printf 'memory:\n  provider: mnemosyne\n' >"$CONFIG_FILE"
  note "created $CONFIG_FILE with memory.provider=mnemosyne"
else
  note "NOTE: 'hermes' is not on PATH and $CONFIG_FILE already exists — this script"
  note "      does not rewrite config files. Add under the existing memory: block:"
  note "          provider: mnemosyne"
fi

# --- 6. verify --------------------------------------------------------------
echo "== Verifying"
"$VENV_PY" - "$PLUGIN_LINK" <<'PY'
import importlib, importlib.util, os, sys
provider_dir = os.path.realpath(sys.argv[1])
box = []
cli = []
spec = importlib.util.spec_from_file_location(
    "_hermes_user_memory.mnemosyne", os.path.join(provider_dir, "__init__.py"),
    submodule_search_locations=[provider_dir])
mod = importlib.util.module_from_spec(spec)
sys.modules["_hermes_user_memory.mnemosyne"] = mod
spec.loader.exec_module(mod)

# T7: the retired provider (integrations/hermes) had no register_cli and a
# keyword-only recall; Hermes' CLI discovery needs both handler names.
cli_path = os.path.join(provider_dir, "cli.py")
cli_src = open(cli_path, encoding="utf-8", errors="replace").read() if os.path.exists(cli_path) else ""
for fn in ("register_cli", "mnemosyne_command"):
    assert f"def {fn}" in cli_src, f"cli.py is missing {fn} (Hermes CLI handler contract)"

class Ctx:
    def register_memory_provider(self, p): box.append(p)
    def register_cli_command(self, **kw): cli.append(kw)
    def register_tool(self, *a, **k): pass
    def register_hook(self, *a, **k): pass

mod.register(Ctx())
assert len(box) == 1, f"expected exactly one provider, got {len(box)}"
names = [c.get("name") for c in cli]
assert names == ["mnemosyne"], f"expected exactly one 'mnemosyne' CLI command, got {names}"
assert callable(cli[0].get("setup_fn")) and callable(cli[0].get("handler_fn")), \
    "mnemosyne CLI command must declare setup_fn and handler_fn"
p = box[0]
assert p.name == "mnemosyne", p.name
if not p.is_available():
    print(f"unavailable: {p.unavailable_reason()}", file=sys.stderr)
    sys.exit(1)
print(f"provider registered: {p.name} (available); CLI handler contract OK")
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
