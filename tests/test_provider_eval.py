"""Check synthetic outcome graders, including oracle/null/wrong answers."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prefetch_eval", ROOT / "bench/hermes_prefetch_eval.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = json.loads((ROOT / "bench/hermes_prefetch_cases.json").read_text(encoding="utf-8"))
LABELS = {row["label"] for row in FIXTURES["memories"]}


def test_oracle_passes_every_case():
    for case in FIXTURES["cases"] + FIXTURES.get("held_out_cases", []):
        oracle = " ".join(f"[EVAL_{label.upper()}]" for label in case["expected"])
        assert MODULE.grade(case, oracle, LABELS)["passed"], case["id"]


def test_held_out_cases_have_explicit_labels_and_versioned_source():
    cases = FIXTURES["held_out_cases"]
    assert FIXTURES["version"] >= 2
    assert {case["id"] for case in cases} == {
        "held-needed-fact",
        "held-distractor",
        "held-topic-switch",
        "held-correction-retirement",
        "held-no-match",
    }
    for case in cases:
        assert case["split"] == "held_out"
        assert set(case["expected"]) <= LABELS
        assert set(case["allowed"]) <= LABELS
        assert set(case["forbidden"]) <= LABELS


def test_held_out_grader_rejects_missing_and_retired_facts():
    case = next(
        case for case in FIXTURES["held_out_cases"]
        if case["id"] == "held-correction-retirement"
    )
    assert not MODULE.grade(case, "[EVAL_IDENTITY]", LABELS)["passed"]
    assert not MODULE.grade(
        case,
        "[EVAL_IDENTITY] [EVAL_CURRENT_START_TIME] [EVAL_RETIRED_START_TIME]",
        LABELS,
    )["passed"]
    assert MODULE.grade(
        case, "[EVAL_IDENTITY] [EVAL_CURRENT_START_TIME]", LABELS
    )["passed"]


def test_empty_wrong_sensitive_and_oversized_answers_fail():
    case = next(case for case in FIXTURES["cases"] if case["id"] == "testing")
    for wrong in (
        "",
        "[EVAL_FOREIGN]",
        "[EVAL_IDENTITY] [EVAL_RAW_TOOL]",
        "[EVAL_IDENTITY] [EVAL_PREFERENCE]" + "x" * 8000,
    ):
        assert not MODULE.grade(case, wrong, LABELS)["passed"]


def test_duplicate_and_estimated_token_diagnostics():
    case = FIXTURES["cases"][0]
    result = MODULE.grade(case, "[EVAL_IDENTITY] [EVAL_IDENTITY]", LABELS)
    assert result["duplicate_markers"] == 1
    assert not result["passed"]
    assert result["estimated_tokens"] == (result["chars"] + 3) // 4


def test_public_prefetch_engine_outcomes(tmp_path):
    if importlib.util.find_spec("mnemosyne") is None:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            pytest.fail("Strict provider evaluation requires the audited mnemosyne engine")
        pytest.skip("Engine is absent in the bare-venv lane")
    report = tmp_path / "prefetch.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bench/hermes_prefetch_eval.py"),
            "--repetitions",
            "2",
            "--require-pass",
            "--output",
            str(report),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["errors"] == 0
    assert evidence["passed"] == len(
        FIXTURES["cases"] + FIXTURES.get("held_out_cases", [])
    )
    assert evidence["evaluation_version"] == FIXTURES["version"]
    assert len(evidence["provider_source_hashes"]) >= 7
    assert len(evidence["provider_source_tree_sha256"]) == 64
    assert evidence["held_out_metrics"]["case_count"] == len(
        FIXTURES["held_out_cases"]
    )
    assert evidence["held_out_metrics"]["execution_errors"] == 0
    assert evidence["held_out_metrics"]["task_recall"] is not None
