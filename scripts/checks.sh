#!/bin/bash
# checks.sh — registry of all repo gates. Every gate must be listed here
# (mirrors ripwire's regression.sh contract: a gate not in the runner is invisible).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Python-only repo; no cargo path needed

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

# rustfmt removed with rust source
run bash scripts/check_notes.sh
run bash scripts/check_version_drift.sh

if [ "$failed" -gt 0 ]; then
    echo "FAILED: $failed gate(s)"
    exit 1
fi
echo "ALL PASS"
