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
        --hermes-agent-version) HERMES_AGENT_VERSION="$2"; shift 2 ;;
        --keep) KEEP=true; shift ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        echo "SKIP: this smoke test needs real symlinks; run it on Linux/macOS (CI)."
        exit 0 ;;
esac

for tool in uv git; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is required" >&2; exit 1; }
done

WORK="$(mktemp -d)"
cleanup() { [[ "$KEEP" == true ]] || rm -rf "$WORK"; }
trap cleanup EXIT

HERMES_HOME="$WORK/hermes-home"
VENV="$WORK/venv"
mkdir -p "$HERMES_HOME"
export HERMES_HOME
# Keyless/keyword path: no embedding-model download, so the round-trip is fast
# and network-independent once the packages are installed.
export MNEMOSYNE_EMBEDDINGS_OFF=1
export HF_HUB_DISABLE_SYMLINKS_WARNING=1

echo "== [1/6] fresh venv at $VENV"
uv venv --python 3.11 "$VENV" >/dev/null
VENV_BIN="$VENV/bin"
VENV_PY="$VENV_BIN/python"
[[ -x "$VENV_PY" ]] || { VENV_BIN="$VENV/Scripts"; VENV_PY="$VENV_BIN/python.exe"; }
export PATH="$VENV_BIN:$PATH"

echo "== [2/6] installing real Hermes $HERMES_AGENT_VERSION"
uv pip install --python "$VENV_PY" "hermes-agent==$HERMES_AGENT_VERSION"
hermes --version

echo "== [3/6] ./install.sh --yes"
"$REPO/install.sh" --yes --venv "$VENV" --hermes-home "$HERMES_HOME"

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

repo, work = sys.argv[1], sys.argv[2]
home = pathlib.Path(work) / "hermes-home"
plugin = home / "plugins" / "mnemosyne"

# Load exactly the way the Hermes memory loader does, from the installed plugin.
provider_dir = os.path.realpath(str(plugin))
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

provider = box[0]
provider.initialize(session_id="smoke", hermes_home=str(home), agent_context="primary")
assert provider.is_available(), provider.unavailable_reason()
fact = "The smoke onboarding target is host kraken-01"
provider.sync_turn(fact, "acknowledged", session_id="smoke")
context = provider.prefetch("kraken-01") or ""
assert "kraken-01" in context, f"round-trip failed; got: {context[:400]}"
print("PASS: one provider registered; sync_turn round-trip read the fact back")
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
