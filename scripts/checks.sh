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

run bash scripts/check_version_drift.sh
run "${PYTHON_BIN:-python3}" tests/test_vendored_provider.py
run "${PYTHON_BIN:-python3}" -m pytest tests/test_install.py tests/test_update.py -q
run "${PYTHON_BIN:-python3}" tests/test_engine_patches.py
run "${PYTHON_BIN:-python3}" tests/test_engine_recall_perf.py
run "${PYTHON_BIN:-python3}" -m pytest tests/test_provider_consolidation.py -q
run "${PYTHON_BIN:-python3}" -m pytest tests/test_provider_prefetch.py tests/test_provider_lifecycle.py tests/test_provider_eval.py tests/test_install_powershell.py -q
run "${PYTHON_BIN:-python3}" -m pytest tests/test_provider_audit.py tests/test_provider_inspection.py tests/test_provider_evidence.py tests/test_lite_mcp.py tests/test_lite_describe_cards.py -q
run "${PYTHON_BIN:-python3}" -m pytest tests/test_provider_maintenance.py tests/test_test_runner.py -q
run "${PYTHON_BIN:-python3}" -m pytest tests/test_provider_profiles.py -q
run "${PYTHON_BIN:-python3}" scripts/check_engine_contract.py

if [ "$failed" -gt 0 ]; then
    echo "FAILED: $failed gate(s)"
    exit 1
fi
echo "ALL PASS"
