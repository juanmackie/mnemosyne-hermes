#!/bin/bash
set -euo pipefail

# Correctness gate: contract tests + non-LLM unit tests. Keep output minimal.
cd "$(dirname "$0")/.."
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . 2>&1 | tail -3
python -m pytest tests -m 'not integration' -q 2>&1 | tail -2
