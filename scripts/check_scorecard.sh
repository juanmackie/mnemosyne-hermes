#!/bin/bash
# Scorecard instrument gate.
#
# Runs bench/scorecard/gate.py, which proves the measurement itself is sound:
# the corpus is deterministic and exactly labelled, the held-out split is
# disjoint, an A/A control shows no difference, and reverting the audited 0.15
# admission-floor engine patch is detected.
#
# Without the pinned engine the gate cannot measure anything, so it skips with
# exit 0 and a loud message -- unless MNEMOSYNE_REQUIRE_ENGINE=1, in which case a
# missing engine is a failure (the same contract as the engine-contract lane).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON_BIN:-python3}"
PROBES="${MNEMOSYNE_SCORECARD_GATE_PROBES:-240}"
WORKERS="${MNEMOSYNE_SCORECARD_GATE_WORKERS:-4}"

if ! "$PY" - <<'PYCHECK' >/dev/null 2>&1
import mnemosyne.core.beam  # noqa: F401
PYCHECK
then
    if [ "${MNEMOSYNE_REQUIRE_ENGINE:-0}" = "1" ]; then
        echo "FAIL: the pinned engine is not importable (MNEMOSYNE_REQUIRE_ENGINE=1)"
        echo "      install it: uv pip install mnemosyne-memory==3.15.1 && $PY scripts/apply_engine_patches.py"
        exit 1
    fi
    echo "SKIP: the pinned engine is not importable; scorecard gate needs it."
    echo "      (set MNEMOSYNE_REQUIRE_ENGINE=1 to make this a failure)"
    exit 0
fi

exec "$PY" bench/scorecard/gate.py --probes "$PROBES" --workers "$WORKERS"
