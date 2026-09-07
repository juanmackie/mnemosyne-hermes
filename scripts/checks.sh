#!/bin/bash
# checks.sh — registry of all repo gates. Every gate must be listed here
# (mirrors ripwire's regression.sh contract: a gate not in the runner is invisible).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.cargo/bin:$PATH"

failed=0
run() {
    echo "=== $* ==="
    if "$@"; then
        echo "PASS: $*"
    else
        echo "FAIL: $*"
        failed=$((failed + 1))
    fi
}

run cargo fmt --check
run bash scripts/check_notes.sh

if [ "$failed" -gt 0 ]; then
    echo "FAILED: $failed gate(s)"
    exit 1
fi
echo "ALL PASS"
