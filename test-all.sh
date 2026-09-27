#!/usr/bin/env bash
# Python test runner: the vendored-provider contract gates, then the unit suite.
#
# Usage:
#   ./test-all.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"

case "${1:-}" in
    '') ;;
    --help|-h) sed -n '2,5p' "$0"; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
esac

cd "$ROOT"
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"
if [[ ! -f pyproject.toml ]]; then
    printf 'Error: pyproject.toml not found at %s\n' "$ROOT" >&2
    exit 1
fi

run_contract_tests() {
    printf '\n== Provider contract gates (vendored drift, loader, db path) ==\n'
    "$PYTHON_BIN" tests/test_vendored_provider.py
    "$PYTHON_BIN" tests/test_provider_loader.py
    "$PYTHON_BIN" tests/test_provider_db_path.py
}

run_non_llm_tests() {
    printf '\n== Python unit tests ==\n'
    "$PYTHON_BIN" -m pytest tests -q
}

run_contract_tests
run_non_llm_tests

printf '\nPython test suite completed.\n'
