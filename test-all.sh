#!/usr/bin/env bash
# Python test runner: the vendored-provider contract gates, then the unit suite.
# The retired adapter's API-key-dependent LLM tests are gone; current tests are
# local/keyless. Any future LLM-marked lane must use the Hermes proxy first.
#
# Usage:
#   ./test-all.sh [--skip-llm] [--require-engine]
#
# --require-engine exports MNEMOSYNE_REQUIRE_ENGINE=1 so the engine-backed
# contract lane fails (never skips) when the engine is missing.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
SKIP_LLM=false

while [[ $# -gt 0 ]]; do
  case "$1" in
  --skip-llm)
    SKIP_LLM=true
    shift
    ;;
  --require-engine)
    export MNEMOSYNE_REQUIRE_ENGINE=1
    shift
    ;;
  --help | -h)
    sed -n '2,10p' "$0"
    exit 0
    ;;
  *)
    printf 'Unknown option: %s\n' "$1" >&2
    exit 2
    ;;
  esac
done

cd "$ROOT"
# Build PYTHONPATH with the configured interpreter's native path separator.
# In Git Bash, the shell is POSIX-like while a Windows Python expects a
# semicolon-separated path list. Passing ROOT as an argument also lets MSYS
# convert it for that Python process; exporting the resulting value avoids
# Git Bash rewriting it as one path when Python is launched later.
export PYTHONPATH
PYTHONPATH="$("$PYTHON_BIN" -c 'import os, sys; from pathlib import Path; parts = [str(Path(sys.argv[1]) / "src")]; inherited = os.environ.get("PYTHONPATH", ""); parts.extend(inherited.split(os.pathsep) if inherited else []); print(os.pathsep.join(parts))' "$ROOT")"
export PYTHONPATH
if [[ ! -f pyproject.toml ]]; then
  printf 'Error: pyproject.toml not found at %s\n' "$ROOT" >&2
  exit 1
fi

run_contract_tests() {
  printf '\n== Provider contract gates (vendored drift, loader, db path, engine contract) ==\n'
  "$PYTHON_BIN" tests/test_vendored_provider.py
  "$PYTHON_BIN" tests/test_engine_patches.py
  "$PYTHON_BIN" tests/test_provider_loader.py
  "$PYTHON_BIN" tests/test_provider_db_path.py
  "$PYTHON_BIN" tests/test_provider_engine_contract.py
}

run_non_llm_tests() {
  printf '\n== Python unit tests ==\n'
  # Coverage when pytest-cov is present (CI installs it); the suite is the
  # same either way, so this is one run, not two.
  local -a pytest_args=(tests -q)
  if [[ "$SKIP_LLM" == true ]]; then
    pytest_args+=(-m "not llm")
  fi
  if "$PYTHON_BIN" -c 'import pytest_cov' >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pytest "${pytest_args[@]}" \
      --cov=mnemosyne_lite --cov-report=term-missing
  else
    "$PYTHON_BIN" -m pytest "${pytest_args[@]}"
  fi
}

run_contract_tests
run_non_llm_tests

if [[ "$SKIP_LLM" == true ]]; then
  printf '\nLLM-marked test filter applied (--skip-llm).\n'
else
  printf '\nLLM-marked tests were not filtered; any LLM lane must use the Hermes proxy first.\n'
fi
printf 'Python test suite completed.\n'
