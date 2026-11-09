#!/usr/bin/env python3
"""Small synthetic source-check evaluation; --run-model explicitly spends host tokens.

No live memory stores or private transcripts are read. Scripted judges in tests
validate the scorer only; they are not evidence of model quality.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

from hermes_memory_provider.evidence import EvidenceError  # noqa: E402
from hermes_memory_provider.source_check import check_memory  # noqa: E402

CASES = Path(__file__).with_name("source_check_cases.json")


def evaluate(
    cases: list[dict[str, Any]], *, complete: Callable[..., Any] | None = None
) -> dict[str, Any]:
    results = []
    for case in cases:
        memory = {
            "id": case["id"],
            "memory_store": "working",
            "content": case["memory"],
            "metadata": {},
        }
        try:
            judgment = check_memory(memory, case["transcript"], complete=complete)
            replacement = judgment["replacement"]
            normalized = (replacement or "").casefold()
            correction_ok = (
                judgment["verdict"] == "contradicted"
                and all(term.casefold() in normalized for term in case.get("replacement_all", []))
                and all(
                    term.casefold() not in normalized for term in case.get("replacement_none", [])
                )
            )
            passed = judgment["verdict"] == case["expected_verdict"]
            if case["expected_verdict"] == "contradicted":
                passed = passed and correction_ok
            results.append(
                {
                    "id": case["id"],
                    "expected": case["expected_verdict"],
                    "passed": passed,
                    "proposal": replacement is not None,
                    "judgment": judgment,
                }
            )
        except EvidenceError as exc:
            results.append(
                {
                    "id": case["id"],
                    "expected": case["expected_verdict"],
                    "passed": False,
                    "proposal": False,
                    "error": str(exc),
                }
            )
    contradictions = sum(row["expected"] == "contradicted" for row in results)
    corrections_passed = sum(row["expected"] == "contradicted" and row["passed"] for row in results)
    correct_memories = sum(row["expected"] == "supported" for row in results)
    insufficient_cases = sum(row["expected"] == "insufficient" for row in results)
    false_proposals = sum(
        row["expected"] in ("supported", "insufficient") and row["proposal"] for row in results
    )
    non_correction_cases = correct_memories + insufficient_cases

    def rate(numerator: int, denominator: int) -> float | None:
        return None if denominator == 0 else round(numerator / denominator, 4)

    return {
        "format": "mnemosyne-source-check-eval/v1",
        "coverage": "small authored synthetic corpus; not a production quality estimate",
        "cases": len(results),
        "passed": sum(row["passed"] for row in results),
        "errors": sum("error" in row for row in results),
        "contradictions": contradictions,
        "corrections_passed": corrections_passed,
        "correction_pass_rate": rate(corrections_passed, contradictions),
        "correct_memories": correct_memories,
        "correct_memory_false_proposals": sum(
            row["expected"] == "supported" and row["proposal"] for row in results
        ),
        "insufficient_cases": insufficient_cases,
        "insufficient_false_proposals": sum(
            row["expected"] == "insufficient" and row["proposal"] for row in results
        ),
        "false_proposal_rate": rate(false_proposals, non_correction_cases),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-model",
        action="store_true",
        required=True,
        help="Explicitly make up to eight calls using the existing Hermes model adapter",
    )
    parser.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    report = evaluate(cases)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
