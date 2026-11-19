"""Contract tests for the Self-Improvement Memory ledger (``scripts/sim.py``).

The ledger is dev tooling, not product code, but it is the record the next
improvement loop reads before it proposes. These tests pin the two properties
that make it worth reading: a prediction cannot be skipped or rewritten after
the fact, and a bad ledger fails loudly instead of being read as an empty one.

`scripts/` is not an importable package, so the module is loaded by path: the
tests then exercise the same entry point an operator runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parent.parent
SIM_PATH = ROOT / "scripts" / "sim.py"
LEDGER = ROOT / "research" / "sim.json"
ITER1 = ROOT / "research" / "runs" / "iter-1.json"
ITER2 = ROOT / "research" / "runs" / "iter-2.json"
ITER7 = ROOT / "research" / "runs" / "iter-7.json"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("mnemosyne_sim_ledger", SIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Any, module)


sim = _load()


def _base_ledger() -> dict[str, Any]:
    """A minimal valid ledger: one problem, one recorded attempt."""
    return {
        "format": "mnemosyne-sim/v1",
        "next_id": 2,
        "carried_from": {"date": "2026-11-19", "sources": ["research/research.md"], "note": ""},
        "aliases": {"typo": "P01"},
        "problems": [
            {
                "id": "P01",
                "name": "A root cause",
                "opened": {"date": "2026-11-16", "objective": "precisionmembench"},
                "attempts": [
                    {
                        "round": 1,
                        "accepted": True,
                        "backfilled": False,
                        "what": "change and rationale",
                        "files": ["scripts/sim.py"],
                        "patch": "P51",
                        "predicted": ["case-a", "case-b"],
                        "fixed": ["case-a"],
                        "missed": ["case-b"],
                        "collateral": ["case-c"],
                        "passed": 2,
                        "total": 3,
                        "predicted_at": "2026-11-16T09:00:00Z",
                        "evaluated_at": "2026-11-16T10:00:00Z",
                        "provenance": {
                            "objective": "precisionmembench",
                            "fixture_sha256": None,
                            "engine_pin": "mnemosyne-memory[embeddings]>=3.15.1,<3.16",
                            "engine_patches_sha256": None,
                            "hermes_version": None,
                            "git_commit": "8630c68",
                            "model": "claude-opus-5.5",
                        },
                    }
                ],
            }
        ],
    }


def _write(tmp_path: Path, ledger: dict[str, Any]) -> Path:
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    return path


def test_committed_ledger_is_valid() -> None:
    assert sim.validate(json.loads(LEDGER.read_text(encoding="utf-8"))) == []


def test_base_fixture_is_valid() -> None:
    assert sim.validate(_base_ledger()) == []


# --- one negative fixture per rule the plan names --------------------------


def test_attempt_without_a_prediction_is_rejected() -> None:
    ledger = _base_ledger()
    attempt = ledger["problems"][0]["attempts"][0]
    attempt["predicted"] = []
    attempt["fixed"] = []
    errors = sim.validate(ledger)
    assert any("prediction must be written before the run" in error for error in errors), errors


def test_alias_must_point_at_a_real_problem() -> None:
    ledger = _base_ledger()
    ledger["aliases"]["typo"] = "P99"
    errors = sim.validate(ledger)
    assert any("points at unknown problem" in error for error in errors), errors


def test_fixed_must_be_a_subset_of_predicted() -> None:
    ledger = _base_ledger()
    ledger["problems"][0]["attempts"][0]["fixed"] = ["case-a", "case-z"]
    errors = sim.validate(ledger)
    assert any("fixed is not a subset of predicted" in error for error in errors), errors


def test_missed_must_be_a_subset_of_predicted() -> None:
    ledger = _base_ledger()
    ledger["problems"][0]["attempts"][0]["missed"] = ["case-z"]
    errors = sim.validate(ledger)
    assert any("missed is not a subset of predicted" in error for error in errors), errors


def test_prediction_must_precede_evaluation() -> None:
    ledger = _base_ledger()
    attempt = ledger["problems"][0]["attempts"][0]
    attempt["predicted_at"] = "2026-11-17T10:00:00Z"
    attempt["evaluated_at"] = "2026-11-16T10:00:00Z"
    errors = sim.validate(ledger)
    assert any("predicted_at is later than evaluated_at" in error for error in errors), errors


def test_same_second_prediction_and_evaluation_is_allowed() -> None:
    ledger = _base_ledger()
    attempt = ledger["problems"][0]["attempts"][0]
    attempt["predicted_at"] = "2026-11-16T10:00:00Z"
    attempt["evaluated_at"] = "2026-11-16T10:00:00Z"
    assert sim.validate(ledger) == []


def test_backfilled_attempt_may_be_recorded_after_the_fact() -> None:
    ledger = _base_ledger()
    attempt = ledger["problems"][0]["attempts"][0]
    attempt["backfilled"] = True
    attempt["predicted_at"] = "2026-11-17T10:00:00Z"
    attempt["evaluated_at"] = "2026-11-16T10:00:00Z"
    assert sim.validate(ledger) == []


def test_duplicate_root_cause_names_are_rejected() -> None:
    ledger = _base_ledger()
    ledger["problems"].append(
        {
            **ledger["problems"][0],
            "id": "P02",
            "name": "A root  cause.",
            "attempts": [],
        }
    )
    ledger["next_id"] = 3
    errors = sim.validate(ledger)
    assert any("duplicates P01" in error for error in errors), errors


def test_unknown_objective_is_rejected() -> None:
    ledger = _base_ledger()
    ledger["problems"][0]["attempts"][0]["provenance"]["objective"] = "vibes"
    errors = sim.validate(ledger)
    assert any("provenance.objective" in error for error in errors), errors


def test_collateral_must_not_be_part_of_the_prediction() -> None:
    ledger = _base_ledger()
    ledger["problems"][0]["attempts"][0]["collateral"] = ["case-a"]
    errors = sim.validate(ledger)
    assert any("collateral must be cases the prediction did not cover" in error for error in errors)


def test_carried_from_sources_must_exist() -> None:
    ledger = _base_ledger()
    ledger["carried_from"]["sources"] = ["research/not-a-file.md"]
    errors = sim.validate(ledger)
    assert any("missing source file" in error for error in errors), errors


def test_validate_command_fails_loudly_on_a_broken_ledger(tmp_path: Path) -> None:
    ledger = _base_ledger()
    ledger["problems"][0]["attempts"][0]["predicted"] = []
    ledger["problems"][0]["attempts"][0]["fixed"] = []
    path = _write(tmp_path, ledger)
    assert sim.main(["--ledger", str(path), "validate", "--file", str(path)]) == 1


def test_unreadable_ledger_is_never_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    path.write_text("{not json", encoding="utf-8")
    assert sim.main(["--ledger", str(path), "validate", "--file", str(path)]) == 1


def test_record_refuses_a_broken_ledger_before_writing(tmp_path: Path) -> None:
    ledger = _base_ledger()
    ledger["problems"][0]["attempts"][0]["predicted"] = []
    path = _write(tmp_path, ledger)
    before = path.read_bytes()
    assert (
        sim.main(
            ["--ledger", str(path), "predict", "--problem", "P01", "--cases", "x", "--what", "y"]
        )
        == 1
    )
    assert path.read_bytes() == before


# --- the loop itself: predict -> evaluate -> record ------------------------


def test_open_merges_an_identical_root_cause(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    args = [
        "--ledger",
        str(path),
        "open",
        "--name",
        "A root cause",
        "--objective",
        "precisionmembench",
        "--alias",
        "typo",
    ]
    assert sim.main(args) == 0
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "a  ROOT cause.",
                "--objective",
                "precisionmembench",
                "--alias",
                "spelling",
            ]
        )
        == 0
    )
    ledger = json.loads(path.read_text(encoding="utf-8"))
    assert [problem["id"] for problem in ledger["problems"]] == ["P01"]
    assert ledger["aliases"] == {"typo": "P01", "spelling": "P01"}
    assert ledger["next_id"] == 2


def test_open_refuses_an_alias_bound_elsewhere(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "first",
                "--objective",
                "precisionmembench",
                "--alias",
                "shared",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "second",
                "--objective",
                "precisionmembench",
                "--alias",
                "shared",
            ]
        )
        == 1
    )
    ledger = json.loads(path.read_text(encoding="utf-8"))
    assert ledger["aliases"] == {"shared": "P01"}


def test_predict_then_record_needs_no_hand_edits(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "carried lesson",
                "--objective",
                "precisionmembench",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "predict",
                "--problem",
                "P01",
                "--cases",
                "fuzzy-two-edit-transposition-blocked",
                "--what",
                "expand the query once from structured labels",
                "--patch",
                "P50",
                "--model",
                "claude-opus-5.5",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "record",
                "--attempt",
                "P01/0",
                "--report",
                str(ITER2),
                "--baseline",
                str(ITER1),
                "--accept",
            ]
        )
        == 0
    )
    ledger = json.loads(path.read_text(encoding="utf-8"))
    attempt = ledger["problems"][0]["attempts"][0]
    assert attempt["fixed"] == ["fuzzy-two-edit-transposition-blocked"]
    assert attempt["missed"] == []
    # Predicted-not-to-regress is a different set: the expansion turned a real
    # word into another one, and the ledger keeps that.
    assert attempt["collateral"] == ["fuzzy-prefix-blocks-transitive-match"]
    assert (attempt["passed"], attempt["total"]) == (71, 77)
    assert attempt["accepted"] is True
    assert attempt["provenance"]["model"] == "claude-opus-5.5"
    assert attempt["provenance"]["fixture_sha256"] == {
        "beliefs.seed.json": "d6ae1c270faba0e8bf2bc0eb4527cd008d481c451a9cd557b5ab85f918fe33f3",
        "retrieval.cases.json": "6ab5a626356f4a6e947bee8e2e9a95af9ead7cfd58237e4aae6f02ae490db0ba",
    }
    assert sim.validate(ledger) == []


def test_record_refuses_an_attempt_without_a_prediction(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "no prediction",
                "--objective",
                "precisionmembench",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "record",
                "--attempt",
                "P01/0",
                "--report",
                str(ITER7),
                "--accept",
            ]
        )
        == 1
    )


def test_record_refuses_a_case_the_report_does_not_contain(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "typo case",
                "--objective",
                "precisionmembench",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "predict",
                "--problem",
                "P01",
                "--cases",
                "case-that-does-not-exist",
                "--what",
                "typo",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "record",
                "--attempt",
                "P01/0",
                "--report",
                str(ITER7),
                "--accept",
            ]
        )
        == 1
    )


def test_record_refuses_to_overwrite_a_recorded_attempt(tmp_path: Path) -> None:
    path = tmp_path / "sim.json"
    assert (
        sim.main(
            ["--ledger", str(path), "open", "--name", "twice", "--objective", "precisionmembench"]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "predict",
                "--problem",
                "P01",
                "--cases",
                "questions-pinned-active-only",
                "--what",
                "change",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "record",
                "--attempt",
                "P01/0",
                "--report",
                str(ITER7),
                "--accept",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "record",
                "--attempt",
                "P01/0",
                "--report",
                str(ITER7),
                "--accept",
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    ("problem", "cases"),
    [
        ("P01", ["paraphrase/b-redis-code"]),
        ("cost", ["paraphrase/b-redis-code"]),
        ("paraphrase cost", ["paraphrase/b-redis-code"]),
    ],
)
def test_problem_lookup_accepts_id_alias_and_name(
    tmp_path: Path, problem: str, cases: list[str]
) -> None:
    path = tmp_path / "sim.json"
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "open",
                "--name",
                "paraphrase cost",
                "--objective",
                "precisionmembench",
                "--alias",
                "cost",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "predict",
                "--problem",
                problem,
                "--cases",
                ",".join(cases),
                "--what",
                "round",
            ]
        )
        == 0
    )
    assert (
        sim.main(
            [
                "--ledger",
                str(path),
                "predict",
                "--problem",
                "cost",
                "--cases",
                ",".join(cases),
                "--what",
                "again",
            ]
        )
        == 0
    )


def test_render_shows_the_p51_trade_and_its_collateral(tmp_path: Path) -> None:
    rendered = sim.render(json.loads(LEDGER.read_text(encoding="utf-8")))
    problem = next(block for block in rendered.split("### ") if block.startswith("P05 "))
    assert "P51" in problem
    assert "Collateral (kept regressions" in problem
    assert "paraphrase/b-redis-code" in problem
    assert problem.count("paraphrase/") == 21
    # The loop's calibration number is printed, and backfilled predictions are
    # never mixed with predictions written before a run.
    assert "Prediction calibration" in rendered
    assert "| precisionmembench | backfilled |" in rendered
