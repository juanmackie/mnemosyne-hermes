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
# The Self-Improvement Memory ledger (research/sim.json): schema shape, unique
# ids, aliases that point at real problems, predictions written before their
# evaluation, and fixed/missed as subsets of the prediction. A ledger the gate
# cannot parse fails; it is never read as empty. The loop's own mechanics
# (predict -> evaluate -> record) are pytest cases in tests/test_sim_ledger.py.
run "${PYTHON_BIN:-python3}" scripts/sim.py validate
# Strict when MNEMOSYNE_REQUIRE_ENGINE=1: a missing engine fails instead of
# skipping. The plain test file is also collected by `pytest tests`; this
# wrapper is what the engine-contract CI job depends on.
run "${PYTHON_BIN:-python3}" scripts/check_engine_contract.py
# Memory-quality scorecard: proves the instrument (deterministic corpus, A/A
# control, admission-floor revert detection) on a small held-out split. It is a
# gate on the *instrument*, not on product quality; quality is reported weekly
# by the upstream-drift workflow. Skips (loudly) without the engine, like the
# other engine-backed lanes.
run bash scripts/check_scorecard.sh

if [ "$failed" -gt 0 ]; then
    echo "FAILED: $failed gate(s)"
    exit 1
fi
echo "ALL PASS"
