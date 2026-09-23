#!/bin/bash
# Autoresearch runner: fast syntax pre-check, then the canonical search
# benchmark. Prints METRIC name=number lines (the experiment-loop contract).
set -euo pipefail

SELF="${0//\\//}"
SCRIPT_DIR="${SELF%/*}"
[ "$SCRIPT_DIR" = "$SELF" ] && SCRIPT_DIR="."
case "$SCRIPT_DIR" in
    /*) : ;;
    *) SCRIPT_DIR="$PWD/$SCRIPT_DIR" ;;
esac
ROOT="$SCRIPT_DIR"

# Same pinned-python resolution as .auto/measure.sh (minimal-PATH bash safe).
if [ -n "${MEASURE_PYTHON:-}" ]; then
    PYBIN="$MEASURE_PYTHON"
elif [ -x "$HOME/.local/bin/python3.exe" ]; then
    PYBIN="$HOME/.local/bin/python3.exe"
else
    PYBIN="$(command -v python3 || command -v python || true)"
fi
[ -n "$PYBIN" ] || { echo "no python on PATH" >&2; exit 1; }

# Fast pre-check (<1s): a syntax error must not cost a full benchmark run.
"$PYBIN" -m py_compile "$ROOT/src/lib/storage.py"

exec bash "$ROOT/.auto/measure.sh"
