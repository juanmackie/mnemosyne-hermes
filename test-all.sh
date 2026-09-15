#!/usr/bin/env bash
# Python test runner for Mnemosyne and the Hermes adapter.
#
# Usage:
#   ./test-all.sh              # contract and non-LLM tests; run LLM tests if configured
#   ./test-all.sh --skip-llm   # contract and non-LLM tests only
#   ./test-all.sh --llm-only   # integration/LLM-marked tests only

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
SKIP_LLM=false
LLM_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --skip-llm) SKIP_LLM=true ;;
        --llm-only) LLM_ONLY=true ;;
        --help)
            sed -n '2,8p' "$0"
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

cd "$ROOT"
if [[ ! -f pyproject.toml ]]; then
    printf 'Error: pyproject.toml not found at %s\n' "$ROOT" >&2
    exit 1
fi

run_contract_tests() {
    printf '\n== Hermes adapter contract tests ==\n'
    "$PYTHON_BIN" -m unittest discover \
        -s integrations/hermes-memory-provider/tests -t . -v
}

run_non_llm_tests() {
    printf '\n== Python unit tests (integration/LLM tests deselected) ==\n'
    "$PYTHON_BIN" -m pytest tests -m 'not integration' -q
}

run_llm_tests() {
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        printf '\n== LLM tests skipped: ANTHROPIC_API_KEY is not configured ==\n'
        return 0
    fi
    printf '\n== Python integration/LLM tests ==\n'
    "$PYTHON_BIN" -m pytest tests -m integration -q
}

if [[ "$LLM_ONLY" == true ]]; then
    run_llm_tests
else
    run_contract_tests
    run_non_llm_tests
    if [[ "$SKIP_LLM" == false ]]; then
        run_llm_tests
    fi
fi

printf '\nPython test suite completed.\n'
