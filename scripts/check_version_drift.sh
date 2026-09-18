#!/usr/bin/env bash
# T10: one version source. Fails on drift between the lite pyproject, the
# package __version__, the README, and the engine pin carried in install.sh,
# the provider pyproject and VENDORED_FROM.json.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail=0
err() { echo "VERSION DRIFT: $*" >&2; fail=1; }

PYPROJECT_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
LITE_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/mnemosyne_lite/__init__.py | head -1)"
README_VERSION="$(sed -n 's/^\*\*Current Version\*\*: *\(.*\)$/\1/p' README.md | head -1)"

[[ -n "$PYPROJECT_VERSION" ]] || err "no version found in pyproject.toml"
[[ "$PYPROJECT_VERSION" == "$LITE_VERSION" ]] || err "pyproject.toml ($PYPROJECT_VERSION) != mnemosyne_lite ($LITE_VERSION)"
[[ "$PYPROJECT_VERSION" == "$README_VERSION" ]] || err "pyproject.toml ($PYPROJECT_VERSION) != README Current Version ($README_VERSION)"
grep -q "Current status (v$PYPROJECT_VERSION)" README.md \
    || err "README current-status marker is not v$PYPROJECT_VERSION"

ENGINE_PIN='mnemosyne-memory[embeddings]>=3.15.1,<3.16'
grep -qF "$ENGINE_PIN" install.sh || err "install.sh engine pin drifted"
grep -qF "$ENGINE_PIN" integrations/hermes-provider/pyproject.toml || err "provider pyproject engine pin drifted"
grep -qF "$ENGINE_PIN" integrations/hermes-provider/VENDORED_FROM.json || err "VENDORED_FROM.json engine pin drifted"

if [[ "$fail" -ne 0 ]]; then
    exit 1
fi
echo "version sources agree: v$PYPROJECT_VERSION (engine pin $ENGINE_PIN)"
