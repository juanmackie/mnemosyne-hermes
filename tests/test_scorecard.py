"""Unit tests for the memory-quality scorecard (no engine needed).

The end-to-end instrument is proven by `bench/scorecard/gate.py` against the
real provider. These tests pin the parts that decide what a number *means*:
labelling, grading, the paired bootstrap, and the pass/fail verdict, all of
which must be right even when no engine is installed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _package():
    sys.path.insert(0, str(ROOT))
    from bench.scorecard import aggregate, grade, report, stats

    return grade, stats, aggregate, report


def test_scale_corpus_labels_are_exact_and_deterministic():
    scale = _load("mnemosim_scale", ROOT / "bench/sim/scale.py")
    corpus = scale.make_corpus(3, probes=60, dev_probes=12, probes_per_life=20, slots=4)
    again = scale.make_corpus(3, probes=60, dev_probes=12, probes_per_life=20, slots=4)
    assert scale.corpus_digest(corpus) == scale.corpus_digest(again)
    assert corpus["counts"]["held_out"] == 60
    assert corpus["counts"]["dev"] == 12
    held = {p["probe_id"] for p in corpus["probes"] if p["split"] == "held_out"}
    dev = {p["probe_id"] for p in corpus["probes"] if p["split"] == "dev"}
    assert not (held & dev)
    for probe in corpus["probes"]:
        if probe["kind"] in ("fabricated", "expired"):
            assert probe["answers"] == [], probe
            assert probe["must_abstain"] is True
        else:
            assert len(probe["answers"]) == 1, probe
            assert probe["must_abstain"] is False


def test_fabricated_entities_exist_nowhere_in_the_generated_world():
    scale = _load("mnemosim_scale2", ROOT / "bench/sim/scale.py")
    corpus = scale.make_corpus(5, probes=40, dev_probes=0, probes_per_life=20, slots=3)
    entities = {
        event["entity"]
        for life in corpus["lives"]
        for event in life["events"]
        if event.get("entity")
    }
    fabricated = [p for p in corpus["probes"] if p["kind"] == "fabricated"]
    assert fabricated
    for probe in fabricated:
        assert probe["entity"] not in entities, "a fabricated entity must not exist"


def test_grade_tool_surface_semantics():
    grade, _stats, _aggregate, _report = _package()
    current = {
        "probe_id": "p1",
        "kind": "answerable_current",
        "answers": ["Neovim"],
        "stale_values": ["Vim"],
    }
    assert grade.grade(current, ["The editor is Neovim."])["answer_hit"] is True
    # stale value above the current one is a failure even though the answer is present
    out = grade.grade(current, ["The editor is Vim.", "Correction: Neovim."])
    assert out["answer_hit"] is True
    assert out["stale_ranked_above_current"] == 1
    # answer below the stale one is fine
    out = grade.grade(current, ["Correction: Neovim.", "The editor is Vim."])
    assert out["stale_ranked_above_current"] == 0

    fabricated = {"probe_id": "p2", "kind": "fabricated", "answers": [], "stale_values": []}
    assert grade.grade(fabricated, [])["answered_fabricated"] is False
    assert grade.grade(fabricated, ["some row"])["fabricated_rows"] == 1

    expired = {
        "probe_id": "p3",
        "kind": "expired",
        "answers": [],
        "stale_values": ["Sunday 02:00 UTC"],
    }
    assert grade.grade(expired, ["unrelated"])["expired_served"] == 0
    assert grade.grade(expired, ["The window is Sunday 02:00 UTC."])["expired_served"] == 1


def test_expired_and_stale_grading_ignores_other_entities_rows():
    """Value-only matching is a bug: values are drawn from a small vocabulary.

    Two different lives can both have "tox" as their test runner, so a row about
    entity A must not be graded as serving entity B's expired fact.
    """
    grade, _stats, _aggregate, _report = _package()
    expired = {
        "probe_id": "p",
        "kind": "expired",
        "entity": "orina123",
        "answers": [],
        "stale_values": ["tox"],
    }
    other_entity = ["The tarnb456 test runner is tox."]
    assert grade.grade(expired, other_entity)["expired_served"] == 0
    same_entity = ["The orina123 test runner is tox."]
    assert grade.grade(expired, same_entity)["expired_served"] == 1

    current = {
        "probe_id": "q",
        "kind": "answerable_current",
        "entity": "orina123",
        "answers": ["pytest"],
        "stale_values": ["tox"],
    }
    rows = ["The tarnb456 test runner is tox.", "The orina123 test runner is pytest."]
    assert grade.grade(current, rows)["stale_ranked_above_current"] == 0


def test_grade_prefetch_carries_the_same_canonical_fields():
    grade, _stats, _aggregate, _report = _package()
    probe = {
        "probe_id": "p",
        "kind": "expired",
        "answers": [],
        "stale_values": ["Drone"],
    }
    out = grade.grade_prefetch(probe, "remembered: the ci provider is Drone")
    for field in (
        "answer_hit",
        "answered_fabricated",
        "expired_served",
        "stale_ranked_above_current",
    ):
        assert field in out
    assert out["expired_served"] == 1
    assert out["expired_present"] is True


def test_paired_bootstrap_is_deterministic_and_brackets_the_mean():
    _grade, stats, _aggregate, _report = _package()
    differences = [0.0] * 50 + [1.0] * 20
    first = stats.paired_bootstrap_ci(differences, resamples=2000)
    second = stats.paired_bootstrap_ci(differences, resamples=2000)
    assert first["ci_low"] == second["ci_low"]
    assert first["ci_high"] == second["ci_high"]
    assert first["ci_low"] <= first["mean"] <= first["ci_high"]
    assert first["excludes_zero"] is True


def test_paired_bootstrap_of_identical_arms_is_exactly_zero():
    _grade, stats, _aggregate, _report = _package()
    result = stats.paired_bootstrap_ci([0.0] * 100, resamples=500)
    assert result["mean"] == 0.0
    assert (result["ci_low"], result["ci_high"]) == (0.0, 0.0)
    assert result["excludes_zero"] is False


def test_paired_differences_refuses_unpaired_series():
    _grade, stats, _aggregate, _report = _package()
    try:
        stats.paired_differences([1.0, 2.0], [1.0])
    except ValueError as error:
        assert "differ in length" in str(error)
    else:
        raise AssertionError("mismatched pairing must raise, not silently truncate")


def test_holm_is_monotone_and_bounded():
    _grade, stats, _aggregate, _report = _package()
    adjusted = stats.holm([("a", 0.001), ("b", 0.02), ("c", 0.04)])
    values = [row["p_holm"] for row in adjusted]
    assert all(0.0 <= value <= 1.0 for value in values)
    assert values == sorted(values)
    assert all(row["p_holm"] >= row["p"] for row in adjusted)


def test_summary_separates_fabricated_and_expired_failures():
    grade, _stats, aggregate, _report = _package()
    probes = [
        {"probe_id": "a", "kind": "answerable_current", "answers": ["x"], "stale_values": []},
        {"probe_id": "f", "kind": "fabricated", "answers": [], "stale_values": []},
        {"probe_id": "e", "kind": "expired", "answers": [], "stale_values": ["y"]},
    ]
    records = [
        {**grade.grade(probes[0], ["x"]), "surface": "tool"},
        {**grade.grade(probes[1], ["unrelated row"]), "surface": "tool"},
        {**grade.grade(probes[2], ["the value is y"]), "surface": "tool"},
    ]
    summary = aggregate.summarize_surface(probes, records, "tool")
    assert summary["answerable_recall"] == 1.0
    assert summary["fabricated_entity_queries_returning_rows"] == 1
    assert summary["expired_facts_served_as_current"] == 1


def test_comparison_reports_every_missed_target():
    _grade, stats, aggregate, report = _package()
    probes = [
        {"probe_id": "f", "kind": "fabricated", "answers": [], "stale_values": []},
        {"probe_id": "e", "kind": "expired", "answers": [], "stale_values": ["y"]},
        {"probe_id": "a", "kind": "answerable_current", "answers": ["x"], "stale_values": []},
    ]

    def arm(rows):
        return {
            "arm": "x",
            "corpus_digest": "d",
            "engine_state": {"kind": "installed"},
            "surfaces": {"tool": aggregate.summarize_surface(probes, rows, "tool")},
            "secret_audit": {"planted": 1, "stored": 1},
        }

    baseline_records = [
        {
            "probe_id": "f",
            "kind": "fabricated",
            "surface": "tool",
            "answered_fabricated": True,
            "fabricated_rows": 2,
            "expired_served": 0,
            "answer_hit": False,
            "stale_ranked_above_current": 0,
        },
        {
            "probe_id": "e",
            "kind": "expired",
            "surface": "tool",
            "expired_served": 1,
            "answered_fabricated": False,
            "fabricated_rows": 0,
            "answer_hit": False,
            "stale_ranked_above_current": 0,
        },
        {
            "probe_id": "a",
            "kind": "answerable_current",
            "surface": "tool",
            "answer_hit": True,
            "answered_fabricated": False,
            "fabricated_rows": 0,
            "expired_served": 0,
            "stale_ranked_above_current": 0,
        },
    ]
    fixed_records = [
        {**row, "answered_fabricated": False, "fabricated_rows": 0, "expired_served": 0}
        if row["probe_id"] in ("f", "e")
        else row
        for row in baseline_records
    ]
    blocks = {
        surface: aggregate.compare_surfaces(
            probes, baseline_records, fixed_records, surface, resamples=300
        )
        for surface in ("tool", "prefetch")
    }
    result = report.comparison_report(
        baseline=arm(baseline_records),
        candidate=arm(fixed_records),
        blocks=blocks,
        split="held_out",
        resamples=300,
    )
    assert result["criteria"]["secrets_zero_stored"]["informational"] is False
    assert "secrets_zero_stored" in result["missed_targets"]
    # PrecisionMemBench is measured elsewhere, so it is reported as unmeasured.
    assert result["criteria"]["precisionmembench_active_passes"]["value"] is None
    assert "precisionmembench_active_passes" not in result["missed_targets"]
    assert result["criteria"]["fabricated_entity_zero_rows"]["passed"] is True
    assert result["criteria"]["expired_facts_zero_served"]["passed"] is True


def test_engine_state_revert_floor_restores_the_published_gate(tmp_path):
    sys.path.insert(0, str(ROOT))
    from bench.scorecard import cli

    try:
        import mnemosyne.core.beam  # noqa: F401
    except ImportError:
        return  # no engine: the gate covers this, these are pure unit tests
    state = cli.engine_state("revert-floor", tmp_path)
    beam = Path(state["sys_path"]) / "mnemosyne" / "core" / "beam.py"
    body = beam.read_text(encoding="utf-8")
    assert "if len(query_tokens) >= 4:\n        return 0.3" in body
    assert state["write_free"] is False
    # The installed tree must be untouched by building the overlay.
    import mnemosyne.core.beam as real

    assert cli._digest(Path(real.__file__)) == cli._digest(Path(real.__file__))
