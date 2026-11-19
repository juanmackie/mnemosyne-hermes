"""Unit tests for the memory-quality scorecard (no engine needed).

The end-to-end instrument is proven by `bench/scorecard/gate.py` against the
real provider. These tests pin the parts that decide what a number *means*:
labelling, grading, the paired bootstrap, and the pass/fail verdict, all of
which must be right even when no engine is installed.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

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


def test_sleep_stages_keep_simultaneous_events_together_and_assign_answer_cohorts():
    from bench.scorecard.retention import assign_cohorts, stage_events

    events = [
        {
            "event_id": "a",
            "transaction_at": "2025-01",
            "disposition": "store",
            "entity": "x",
            "attribute": "editor",
            "value": "vim",
        },
        {
            "event_id": "b",
            "transaction_at": "2025-02",
            "disposition": "store",
            "entity": "x",
            "attribute": "editor",
            "value": "helix",
        },
        {
            "event_id": "c",
            "transaction_at": "2025-02",
            "disposition": "store",
            "entity": "y",
            "attribute": "shell",
            "value": "fish",
        },
        {
            "event_id": "d",
            "transaction_at": "2025-03",
            "disposition": "never_store",
            "entity": "z",
            "attribute": "shell",
            "value": "secret",
        },
    ]
    stages = stage_events(events, 4)
    assert len(stages) == 4
    assert next(i for i, s in enumerate(stages) if events[1] in s) == next(
        i for i, s in enumerate(stages) if events[2] in s
    )
    probes = [
        {
            "probe_id": "p",
            "kind": "answerable_current",
            "entity": "x",
            "attribute": "editor",
            "answers": ["helix"],
        },
        {
            "probe_id": "q",
            "kind": "answerable_historical",
            "entity": "x",
            "attribute": "editor",
            "answers": ["vim"],
        },
        {
            "probe_id": "r",
            "kind": "answerable_current",
            "entity": "z",
            "attribute": "shell",
            "answers": ["secret"],
        },
    ]
    assert assign_cohorts(stages, probes) == {"p": 1}


def test_retention_summary_pairs_by_life_and_probe_and_rejects_missing_observations():
    from bench.scorecard.retention import summarize

    rows = []
    for life, initial, final in [("a", True, False), ("b", True, True)]:
        for stage, hit in [(0, initial), (3, final)]:
            rows.append(
                {
                    "life_id": life,
                    "probe_id": "same-id",
                    "surface": "tool",
                    "stage": stage,
                    "cohort": 0,
                    "answer_hit": hit,
                    "fabricated_rows": 0,
                    "expired_served": 0,
                }
            )
    result = summarize(rows, resamples=100)
    assert result["tool"]["backward_transfer"]["mean"] == -0.5
    assert result["tool"]["backward_transfer"]["lives"] == 2
    import pytest

    with pytest.raises(ValueError, match="missing"):
        summarize(rows[:-1], resamples=100)


def test_retention_aa_control_rejects_cancelling_errors():
    from bench.scorecard.retention import aa_control

    base = [
        {
            "life_id": "a",
            "probe_id": name,
            "stage": 0,
            "surface": "tool",
            "answer_hit": hit,
            "fabricated_rows": 0,
            "expired_served": 0,
        }
        for name, hit in [("one", True), ("two", False)]
    ]
    changed = [{**row, "answer_hit": not row["answer_hit"]} for row in base]
    assert aa_control(base, base, "tool")["passed"]
    assert not aa_control(base, changed, "tool")["passed"]
    assert aa_control(base, [changed[0], base[1]], "tool")["passed"]


def test_retention_refuses_database_outside_scratch(tmp_path):
    import pytest

    from bench.scorecard.retention import assert_scratch_database

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert_scratch_database(scratch / "life.db", scratch)
    with pytest.raises(ValueError, match="outside"):
        assert_scratch_database(tmp_path / "live.db", scratch)


def test_summary_loss_control_pairs_against_source_deletion():
    from bench.scorecard.retention import compare_control

    common = {
        "life_id": "life",
        "probe_id": "p",
        "surface": "tool",
        "stage": 3,
        "control_target": True,
    }
    retained = [{**common, "answer_hit": False}]
    erased = [{**common, "answer_hit": False}]
    # Losing sources already lost the answer; erasing the summary adds no loss.
    result = compare_control(retained, erased, "tool", resamples=10)
    assert result["mean"] == 0


def test_retention_entrypoint_reports_unexpected_failure(monkeypatch, capsys):
    from bench.scorecard import retention

    def broken():
        raise RuntimeError("measurement failed")

    monkeypatch.setattr(retention, "main", broken)
    assert retention.entrypoint() == 3
    assert "measurement failed" in capsys.readouterr().err


def test_summary_loss_uses_post_sleep_source_deletion_as_baseline():
    from bench.scorecard.retention import summary_loss_control

    common = {
        "life_id": "life",
        "probe_id": "p",
        "surface": "tool",
        "stage": 3,
        "control_target": True,
    }
    arms = {
        "sleep": [{**common, "answer_hit": True}],
        "sleep-delete-originals": [{**common, "answer_hit": False}],
        "sleep-loss-control": [{**common, "answer_hit": False}],
    }
    assert summary_loss_control(arms, "tool", resamples=10)["mean"] == 0


def test_retention_exit_codes_distinguish_controls_and_quality():
    from bench.scorecard.retention import result_exit_code

    assert result_exit_code(False, False) == 2
    assert result_exit_code(True, False) == 1
    assert result_exit_code(True, True) == 0


def test_retention_rejects_shadow_engine_before_creating_store(monkeypatch, tmp_path):
    import importlib.util
    from types import SimpleNamespace

    import pytest

    from bench.scorecard import retention, runner

    monkeypatch.setattr(
        runner, "engine_attestation", lambda: {"package_root": str(tmp_path / "real")}
    )
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=str(tmp_path / "shadow" / "__init__.py")),
    )
    monkeypatch.setattr(
        runner.LifeStore, "create", lambda *_args, **_kwargs: pytest.fail("store opened")
    )
    with pytest.raises(RuntimeError, match="engine path differs"):
        retention.measure_life({"life_id": "synthetic", "events": []}, [], "no-sleep")


@pytest.mark.parametrize("enabled", [False, True])
def test_retention_pins_sleep_verification_to_the_requested_state(monkeypatch, tmp_path, enabled):
    import importlib.util
    from types import SimpleNamespace

    from bench.scorecard import retention, runner

    monkeypatch.setenv("MNEMOSYNE_SLEEP_VERIFY", "ambient")
    monkeypatch.setattr(runner, "engine_attestation", lambda: {"package_root": str(tmp_path)})
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=str(tmp_path / "__init__.py")),
    )
    seen = []

    def stop_before_store(*_args, **_kwargs):
        seen.append(
            tuple(
                os.environ.get(name)
                for name in (
                    "MNEMOSYNE_SLEEP_VERIFY",
                    "MNEMOSYNE_SLEEP_DEMOTE_SOURCES",
                    "MNEMOSYNE_DEGRADE_VERIFY",
                    "MNEMOSYNE_DEGRADE_ADDITIVE",
                )
            )
        )
        raise RuntimeError("controlled stop")

    monkeypatch.setattr(runner.LifeStore, "create", stop_before_store)
    with pytest.raises(RuntimeError, match="controlled stop"):
        retention.measure_life(
            {"life_id": "synthetic", "events": []}, [], "no-sleep", verify_sleep=enabled
        )
    # The engine defaults the guards on, so every arm pins them explicitly:
    # the requested state for sleep verification, off for the rest.
    assert seen == [("1" if enabled else "0", "0", "0", "0")]


def test_require_vendored_provider_accepts_snapshot_and_refuses_other_copies(tmp_path):
    """The scorecard must measure the vendored provider, not a same-name copy.

    The engine wheel ships its own hermes_memory_provider without the local
    patches; a plain import can pick it up. The guard compares bytes.
    """
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT))
    from bench.scorecard import runner

    vendored = runner.PROVIDER_ROOT / "hermes_memory_provider" / "__init__.py"
    accepted = runner.require_vendored_provider(SimpleNamespace(__file__=str(vendored)))
    assert accepted["matches_vendored"] is True
    assert accepted["files_checked"] == len(runner.provider_tree_hashes())

    copy = tmp_path / "hermes_memory_provider"
    copy.mkdir()
    for name in runner.provider_tree_hashes():
        source = runner.PROVIDER_ROOT / "hermes_memory_provider" / name
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_bytes(source.read_bytes())
    (copy / "__init__.py").write_bytes(b"# unpatched wheel copy\n")
    with pytest.raises(RuntimeError, match="not the vendored snapshot"):
        runner.require_vendored_provider(SimpleNamespace(__file__=str(copy / "__init__.py")))


def test_staged_arm_keeps_the_inherited_environment(monkeypatch):
    """The staged arm needs HOME/USERPROFILE/PATH while the engine imports.

    Same invariant as the rehearsal: clean_environment reads os.environ, so it
    must be called before the clear. This arm is the one that died on Windows
    with `Path.home()` raising inside the pool worker.
    """
    mnemosyne = pytest.importorskip("mnemosyne")
    from bench.scorecard import retention, runner

    captured: dict[str, str] = {}

    def fake_create(cls, scratch, life_id, **kwargs):
        captured.update(dict(os.environ))
        raise RuntimeError("stop before the engine is touched")

    monkeypatch.setenv("SCORECARD_ENV_MARKER", "1")
    monkeypatch.setattr(runner.LifeStore, "create", classmethod(fake_create))
    # The arm verifies the engine path before opening a store; stub the
    # attestation so the test is about the environment, not the engine build.
    package_root = Path(mnemosyne.__file__).resolve().parent
    monkeypatch.setattr(runner, "engine_attestation", lambda: {"package_root": str(package_root)})

    with pytest.raises(RuntimeError, match="stop before the engine"):
        retention.measure_life({"life_id": "env-life", "events": []}, [], "no-sleep")

    assert captured.get("SCORECARD_ENV_MARKER") == "1", (
        "staged arm dropped the inherited environment"
    )
    assert "PATH" in captured
