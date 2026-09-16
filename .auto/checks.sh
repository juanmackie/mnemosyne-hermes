#!/bin/bash
set -euo pipefail
set -o pipefail

# Correctness gate: contract tests + non-LLM unit tests. Keep output minimal.
# NOTE: builtins only (+ python) — must run under a minimal-PATH bash.
SELF="${0//\\//}"
cd "${SELF%/*}/.."
PYBIN="$(command -v python || command -v python3 || true)"
[ -n "$PYBIN" ] || { echo "no python/python3 on PATH"; exit 1; }

tail_lines() { # $1 = count; reads stdin, prints last $1 lines (builtins only)
    local n="$1" line
    local -a buf=()
    while IFS= read -r line; do
        buf+=("$line")
        [ "${#buf[@]}" -gt "$n" ] && buf=("${buf[@]:1}")
    done
    printf '%s\n' "${buf[@]}"
}

"$PYBIN" -m unittest discover -s integrations/hermes-memory-provider/tests -t . 2>&1 | tail_lines 3
"$PYBIN" -m pytest tests -m 'not integration' -q 2>&1 | tail_lines 2
