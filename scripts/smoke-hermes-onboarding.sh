#!/usr/bin/env bash
# T5: clean-user Hermes onboarding smoke test.
#
# A fresh venv, a fresh $HERMES_HOME and a real Hermes, then the real
# ./install.sh --yes, then the four acceptance assertions:
#   1. hermes mnemosyne doctor --no-fix exits 0
#   2. hermes memory status shows mnemosyne installed/available/active
#   3. a sync_turn round-trip writes and reads back a fact
#   4. exactly one provider is registered
#
# Needs real symlinks (install.sh links the plugin). On Windows it SKIPS with
# exit 0; CI runs it on ubuntu-latest.
#
# Usage: scripts/smoke-hermes-onboarding.sh [--hermes-agent-version X] [--keep]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_AGENT_VERSION="${HERMES_AGENT_VERSION:-0.19.0}"
KEEP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
  --hermes-agent-version)
    HERMES_AGENT_VERSION="$2"
    shift 2
    ;;
  --keep)
    KEEP=true
    shift
    ;;
  -h | --help)
    sed -n '2,15p' "$0"
    exit 0
    ;;
  *)
    echo "Unknown option: $1" >&2
    exit 2
    ;;
  esac
done

case "$(uname -s)" in
MINGW* | MSYS* | CYGWIN*)
  echo "SKIP: this smoke test needs real symlinks; run it on Linux/macOS (CI)."
  exit 0
  ;;
esac

for tool in uv git; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "ERROR: $tool is required" >&2
    exit 1
  }
done

WORK="$(mktemp -d)"
cleanup() { [[ "$KEEP" == true ]] || rm -rf "$WORK"; }
trap cleanup EXIT

HERMES_HOME="$WORK/hermes-home"
VENV="$WORK/venv"
mkdir -p "$HERMES_HOME"
export HERMES_HOME
# Keep this isolated smoke away from any caller's configured database or keys.
unset MNEMOSYNE_DB_PATH MNEMOSYNE_DATA_DIR MNEMOSYNE_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY
# Keyless/keyword path: no embedding-model download, so the round-trip is fast
# and network-independent once the packages are installed.
export MNEMOSYNE_EMBEDDINGS_OFF=1
export HF_HUB_DISABLE_SYMLINKS_WARNING=1

echo "== [1/6] fresh venv at $VENV"
uv venv --python 3.11 "$VENV" >/dev/null
VENV_BIN="$VENV/bin"
VENV_PY="$VENV_BIN/python"
[[ -x "$VENV_PY" ]] || {
  VENV_BIN="$VENV/Scripts"
  VENV_PY="$VENV_BIN/python.exe"
}
export PATH="$VENV_BIN:$PATH"

echo "== [2/6] installing real Hermes $HERMES_AGENT_VERSION"
uv pip install --python "$VENV_PY" "hermes-agent==$HERMES_AGENT_VERSION"
hermes --version

echo "== [3/6] ./install.sh --yes"
"$REPO/install.sh" --yes --venv "$VENV" --hermes-home "$HERMES_HOME"

echo "== [3.5/6] seed bundled/user discovery collision"
"$VENV_PY" - "$REPO" <<'PY'
import pathlib
import sys
import plugins.memory as registry

repo = pathlib.Path(sys.argv[1])
provider = repo / "integrations" / "hermes-provider" / "hermes_memory_provider"
bundled = registry._MEMORY_PLUGINS_DIR / "mnemosyne"
if bundled.exists() or bundled.is_symlink():
    raise RuntimeError(f"unexpected pre-existing bundled provider: {bundled}")
bundled.symlink_to(provider, target_is_directory=True)
assert bundled.resolve() == provider.resolve()
PY

fail=0

echo "== [4/6] hermes mnemosyne doctor --no-fix"
doctor_out="$WORK/doctor.out"
if hermes mnemosyne doctor --no-fix >"$doctor_out" 2>&1; then
  echo "PASS: doctor exit 0"
  sed -n '1,16p' "$doctor_out"
else
  echo "FAIL: doctor exited $?"
  cat "$doctor_out"
  fail=1
fi

echo "== [5/6] hermes memory status"
status_out="$WORK/status.out"
hermes memory status >"$status_out" 2>&1 || true
for needle in mnemosyne installed available active; do
  if grep -q "$needle" "$status_out"; then
    echo "PASS: memory status contains '$needle'"
  else
    echo "FAIL: memory status missing '$needle'"
    cat "$status_out"
    fail=1
  fi
done

echo "== [6/6] sync_turn round-trip + single registration"
if "$VENV_PY" - "$REPO" "$WORK" <<'PY'
import importlib.util
import os
import pathlib
import sys
import threading
import time

repo, work = sys.argv[1], sys.argv[2]
home = pathlib.Path(work) / "hermes-home"
plugin = home / "plugins" / "mnemosyne"

# Load exactly the way the Hermes memory loader does, from the installed plugin.
provider_dir = os.path.realpath(str(plugin))
assert plugin.is_symlink(), f"installer did not create the expected user plugin symlink: {plugin}"
assert pathlib.Path(provider_dir) == pathlib.Path(repo) / "integrations" / "hermes-provider" / "hermes_memory_provider"

# Exercise the installed Hermes discovery code with the same provider visible
# through both roots. Bundled providers win name collisions; the user symlink
# must not create duplicate discovery or registration.
import plugins.memory as memory_registry
bundled_dir = memory_registry._MEMORY_PLUGINS_DIR / "mnemosyne"
assert bundled_dir.is_symlink() and bundled_dir.resolve() == pathlib.Path(provider_dir)
matches = [row for row in memory_registry.discover_memory_providers() if row[0] == "mnemosyne"]
assert len(matches) == 1 and matches[0][1] and matches[0][2] is True, matches
assert memory_registry.find_provider_dir("mnemosyne") == bundled_dir
cli_commands = memory_registry.discover_plugin_cli_commands()
assert [cmd["name"] for cmd in cli_commands] == ["mnemosyne"], cli_commands
loaded = memory_registry.load_memory_provider("mnemosyne")
assert loaded is not None and loaded.name == "mnemosyne", loaded

spec = importlib.util.spec_from_file_location(
    "_smoke.mnemosyne", os.path.join(provider_dir, "__init__.py"),
    submodule_search_locations=[provider_dir])
mod = importlib.util.module_from_spec(spec)
sys.modules["_smoke.mnemosyne"] = mod
spec.loader.exec_module(mod)

box, cli = [], []


class Ctx:
    def register_memory_provider(self, p):
        box.append(p)

    def register_cli_command(self, **kw):
        cli.append(kw)

    def register_tool(self, *a, **k):
        pass

    def register_hook(self, *a, **k):
        pass


mod.register(Ctx())
assert len(box) == 1, f"expected exactly one provider, got {len(box)}"
assert [c.get("name") for c in cli] == ["mnemosyne"], cli
assert box[0].name == "mnemosyne"

provider = loaded
provider.initialize(session_id="smoke", hermes_home=str(home), agent_context="primary")
assert provider.is_available(), provider.unavailable_reason()

# Hermes 0.18.2/0.19.0 owns the asynchronous, serialized sync worker. Verify
# that a deliberately slow DB write does not hold the caller, then drain the
# worker and prove the write landed. Also measure direct sync_turn as a baseline
# on the same injected delay; callers must route it through MemoryManager.
from agent.memory_manager import MemoryManager

slow_seconds = 0.2
real_remember = provider._beam.remember

# Direct call establishes the blocking baseline for the same injected DB delay.
def delayed_remember(*args, **kwargs):
    time.sleep(slow_seconds)
    return real_remember(*args, **kwargs)

provider._beam.remember = delayed_remember
direct_start = time.perf_counter()
provider.sync_turn("Direct baseline writes host direct-kraken", "acknowledged", session_id="smoke")
direct_ms = (time.perf_counter() - direct_start) * 1000
assert direct_ms >= slow_seconds * 750, f"direct baseline did not include the DB delay: {direct_ms:.1f} ms"

# Production route: hold the DB operation open and prove sync_all returns while
# it is still in flight. The event makes the assertion independent of host speed.
started = threading.Event()
release = threading.Event()

def blocked_remember(*args, **kwargs):
    started.set()
    if not release.wait(timeout=5):
        raise TimeoutError("test write was not released")
    return real_remember(*args, **kwargs)

provider._beam.remember = blocked_remember
manager = MemoryManager()
manager.add_provider(provider)
assert len(manager.providers) == 1 and manager.get_provider("mnemosyne") is provider, manager.providers
try:
    # Contract check: Hermes 0.18/0.19 swallows a provider CheckpointError, so
    # the option cannot honestly promise to abort compression end-to-end.
    original_pre_compress = provider.on_pre_compress
    def fail_checkpoint(_messages):
        raise mod.CheckpointError("expected test failure")
    provider.on_pre_compress = fail_checkpoint
    try:
        assert manager.on_pre_compress([{"role": "user", "content": "x"}]) == ""
    finally:
        provider.on_pre_compress = original_pre_compress

    start = time.perf_counter()
    manager.sync_all(
        "The smoke onboarding target is host kraken-01",
        "acknowledged",
        session_id="smoke",
    )
    dispatch_ms = (time.perf_counter() - start) * 1000
    assert dispatch_ms < 100, f"sync dispatch blocked for {dispatch_ms:.1f} ms"
    assert started.wait(timeout=2), "background sync never reached the delayed DB write"
    assert not release.is_set(), "test DB operation unexpectedly completed before release"
    release.set()
    assert manager.flush_pending(timeout=15), "Hermes background sync did not drain"
    context = provider.prefetch("kraken-01") or ""
    assert "kraken-01" in context, f"round-trip failed; got: {context[:400]}"
    print(
        f"PASS: one Hermes-discovered provider despite bundled/user symlink collision; "
        f"delayed sync dispatch={dispatch_ms:.1f} ms, "
        f"direct baseline={direct_ms:.1f} ms; round-trip read back the fact"
    )
finally:
    release.set()
    provider._beam.remember = real_remember
    manager.shutdown_all()
PY
then
  :
else
  echo "FAIL: round-trip / registration assertion"
  fail=1
fi

echo
if [[ "$fail" -eq 0 ]]; then
  echo "SMOKE TEST PASSED"
else
  echo "SMOKE TEST FAILED"
  exit 1
fi
