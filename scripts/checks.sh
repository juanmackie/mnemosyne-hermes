#!/bin/bash
# checks.sh — registry of repo gates that `pytest tests` does not run.
#
# The rule: if a check is a pytest test under tests/, `./test-all.sh` already
# runs it, and listing it here too would run it twice in CI. Only non-pytest
# gates (shell/version/secret checks, a test module pytest cannot collect, and
# the engine-contract wrapper that must honour MNEMOSYNE_REQUIRE_ENGINE) belong
# in this file. A gate that is in neither place is invisible.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

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

run bash scripts/check_version_drift.sh
run bash scripts/check_no_secrets.sh
# Strict when MNEMOSYNE_REQUIRE_ENGINE=1: a missing engine fails instead of
# skipping. The plain test file is also collected by `pytest tests`; this
# wrapper is what the engine-contract CI job depends on.
run "${PYTHON_BIN:-python3}" scripts/check_engine_contract.py

if [ "$failed" -gt 0 ]; then
    echo "FAILED: $failed gate(s)"
    exit 1
fi
echo "ALL PASS"
