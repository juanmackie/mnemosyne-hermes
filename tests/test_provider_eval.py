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
    for case in FIXTURES["cases"]:
        oracle = " ".join(f"[EVAL_{label.upper()}]" for label in case["expected"])
        assert MODULE.grade(case, oracle, LABELS)["passed"], case["id"]


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
    assert evidence["passed"] == len(FIXTURES["cases"])
