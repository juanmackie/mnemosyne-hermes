#!/usr/bin/env bash
# Install into Hermes' Python environment so entry-point discovery can find us.
set -euo pipefail
REPO_URL="${MNEMOSYNE_REPO_URL:-https://github.com/juanmackie/mnemosyne-hermes}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PYTHON_BIN="${PYTHON_BIN:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url|--setup|--python)
      [[ $# -ge 2 && -n "$2" ]] || { echo "Missing value for $1" >&2; exit 2; }
      if [[ "$1" == --python ]]; then PYTHON_BIN="$2"; else REPO_URL="$2"; fi
      shift 2 ;;
    --help|-h)
      echo 'Usage: install.sh [--repo-url URL | --setup URL] [--python /path/to/hermes/venv/bin/python]'
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in "${HERMES_INSTALL_DIR:-$HERMES_HOME/hermes-agent}/venv/bin/python" \
      /usr/local/lib/hermes-agent/venv/bin/python "${VIRTUAL_ENV:-/nonexistent}/bin/python"; do
    if [[ -x "$candidate" ]]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo 'Select the Python environment used by Hermes with --python /path/to/venv/bin/python.' >&2
  echo 'For standalone CLI/MCP use: python3 -m venv .venv; then pass --python .venv/bin/python.' >&2
  exit 1
fi
# Resolve before changing directories; do not install into an unrelated Python.
PYTHON_BIN="$($PYTHON_BIN -c 'import sys; print(sys.executable)')"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"; import pip'

SOURCE=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
TMP=""
trap '[[ -z "$TMP" ]] || rm -rf "$TMP"' EXIT
if [[ ! -f "$SOURCE/integrations/hermes/pyproject.toml" ]]; then
  TMP="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$TMP/repo"
  SOURCE="$TMP/repo"
fi
# YAML is an install-time dependency (already present in Hermes), not a core
# memory dependency. Never edit arbitrary YAML with regex/string replacement.
"$PYTHON_BIN" -m pip install "$SOURCE" "$SOURCE/integrations/hermes" 'PyYAML>=6'
"$PYTHON_BIN" - "$HERMES_HOME" <<'PY'
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import yaml
from mnemosyne.cli import main
from mnemosyne_hermes import MnemosyneMemoryProvider, ProviderConfig

home = Path(sys.argv[1]).expanduser()
home.mkdir(parents=True, exist_ok=True)
path = home / "config.yaml"
config = yaml.safe_load(path.read_text()) if path.exists() else {}
if config is None:
    config = {}
if not isinstance(config, dict):
    raise ValueError("Hermes config must be a mapping")
memory = config.setdefault("memory", {})
if not isinstance(memory, dict):
    raise ValueError("memory config must be a mapping")
db_path = os.path.expanduser(os.environ.get("MNEMOSYNE_DB_PATH") or memory.get("db_path") or str(home / "mnemosyne/mnemosyne.db"))
namespace = os.environ.get("MNEMOSYNE_NAMESPACE") or memory.get("namespace", "agent:hermes")
provider = MnemosyneMemoryProvider(ProviderConfig(hermes_home=str(home), db_path=db_path, namespace=namespace))
provider._get_storage()  # fail before enabling a provider with an unusable DB
provider.shutdown()
# Preserve explicit paths for the entry-point provider, not just config.yaml.
provider.save_config({"db_path": db_path, "namespace": namespace}, hermes_home=str(home))
memory.update(provider="mnemosyne", db_path=db_path)
if path.exists():
    # Never overwrite a prior backup during a repeated install.
    fd, backup = tempfile.mkstemp(prefix="config.yaml.backup-", dir=home)
    os.close(fd)
    shutil.copyfile(path, backup)
    print(f"Config backup: {backup}")
with tempfile.NamedTemporaryFile(mode="w", dir=home, delete=False, encoding="utf-8") as handle:
    temporary = handle.name
    try:
        yaml.safe_dump(config, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        os.unlink(temporary)
        raise
os.replace(temporary, path)
main(["--version"])
PY
echo "Installed into $PYTHON_BIN; Hermes configuration: $HERMES_HOME/config.yaml"
echo 'Restart Hermes to discover the mnemosyne provider. CLI: use this environment’s bin directory.'
