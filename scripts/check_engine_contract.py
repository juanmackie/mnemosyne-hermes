#!/usr/bin/env python3
"""Repo-gate wrapper for the engine-backed provider contract lane.

Local (bare venv): the engine is absent, so the lane prints
``engine absent, skipped`` and exits 0 — the contract file itself also prints
its per-test skip lines. Under ``MNEMOSYNE_REQUIRE_ENGINE=1`` a missing engine
is a hard failure instead of a skip.

    python3 scripts/check_engine_contract.py
    MNEMOSYNE_REQUIRE_ENGINE=1 python3 scripts/check_engine_contract.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests" / "test_provider_engine_contract.py"
REQUIRE = os.environ.get("MNEMOSYNE_REQUIRE_ENGINE", "0") == "1"


def _engine_present() -> bool:
    try:
        import mnemosyne.core.beam  # noqa: F401
    except ImportError:
        return False
    return True


def main() -> int:
    if not CONTRACT.exists():
        print(f"ERROR: contract lane missing: {CONTRACT}", file=sys.stderr)
        return 1
    if not _engine_present():
        if REQUIRE:
            print(
                "FAIL: MNEMOSYNE_REQUIRE_ENGINE=1 but the mnemosyne-memory "
                "engine is not importable. Install it with: "
                "uv pip install ./integrations/hermes-provider",
                file=sys.stderr,
            )
            return 1
        print("engine absent, skipped (install ./integrations/hermes-provider to run)")
        return 0
    proc = subprocess.run([sys.executable, str(CONTRACT)])
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
