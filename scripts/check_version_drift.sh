#!/usr/bin/env bash
# T10: one version source. The lite distribution has exactly one version
# literal — `mnemosyne_lite.__version__` — which pyproject.toml reads
# dynamically. This gate fails if a second literal appears, if that wiring
# breaks, or if the README drifts from it. It also pins the engine pin carried
# in install.sh, the provider pyproject and VENDORED_FROM.json.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail=0
err() {
  echo "VERSION DRIFT: $*" >&2
  fail=1
}

LITE_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/mnemosyne_lite/__init__.py | head -1)"
README_VERSION="$(sed -n 's/^\*\*Current Version\*\*: *\(.*\)$/\1/p' README.md | head -1)"

[[ -n "$LITE_VERSION" ]] || err "no __version__ in src/mnemosyne_lite/__init__.py"
if sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | grep -q .; then
  err "pyproject.toml carries a literal version again; keep it dynamic"
fi
grep -qE '^dynamic = \["version"\]' pyproject.toml ||
  err "pyproject.toml does not declare dynamic version"
grep -qF 'attr = "mnemosyne_lite.__version__"' pyproject.toml ||
  err "pyproject.toml dynamic version does not read mnemosyne_lite.__version__"
[[ -n "$README_VERSION" ]] || err "README.md has no '**Current Version**:' line"
[[ "$LITE_VERSION" == "$README_VERSION" ]] ||
  err "mnemosyne_lite ($LITE_VERSION) != README Current Version ($README_VERSION)"

ENGINE_PIN='mnemosyne-memory[embeddings]>=3.15.1,<3.16'
grep -qF "$ENGINE_PIN" install.sh || err "install.sh engine pin drifted"
grep -qF "$ENGINE_PIN" integrations/hermes-provider/pyproject.toml || err "provider pyproject engine pin drifted"
grep -qF "$ENGINE_PIN" integrations/hermes-provider/VENDORED_FROM.json || err "VENDORED_FROM.json engine pin drifted"

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "version sources agree: v$LITE_VERSION (engine pin $ENGINE_PIN)"
