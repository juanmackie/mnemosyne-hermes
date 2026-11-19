#!/usr/bin/env python3
"""Autoresearch evaluator: one PMB run -> {"pass": bool, "score": number}.

Usage (from repo root):
    PYTHONPATH="$PWD:$PWD/integrations/hermes-provider" \
      python research/evaluate.py \
      --output research/runs/iter-N.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    from bench.precisionmembench.run_eval import run

    report = run()
    summary = report["summary"]
    active = summary["activePasses"]
    over = summary["overreturnFailures"]
    missing = summary["missingExpectedBeliefs"]

    score = active * 100 - over - 10 * missing
    passed = active == 43 and missing <= 1 and over <= 5
    result = {
        "pass": passed,
        "score": score,
        "activePasses": active,
        "overreturnFailures": over,
        "missingExpectedBeliefs": missing,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"contract": result, "report": report}, sort_keys=True, indent=1),
            encoding="utf-8",
        )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
