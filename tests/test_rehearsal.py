"""Pure-logic tests for the offline rehearsal (Phase 4). No engine needed."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.rehearsal import rehearse as rh  # noqa: E402


def test_split_is_fixed_and_uses_both_halves():
    ids = [f"belief-{n}" for n in range(40)]
    first = [rh.split_of(i) for i in ids]
    assert first == [rh.split_of(i) for i in ids]
    assert {"fit", "held"} == set(first)


def test_probes_are_label_plus_first_key_token_and_skip_unlabelled():
    items = [
        {"id": "a", "content": "The Redis limit is 4096 keys.", "canonical_name": "Redis"},
        {"id": "b", "content": "no label here 12", "canonical_name": ""},
        {"id": "c", "content": "only lowercase words", "canonical_name": "Words"},
    ]
    probes = rh.make_probes(items)
    assert [p["probe_id"] for p in probes] == ["a"]
    assert probes[0]["query"] == "Redis 4096"
    assert probes[0]["target"] == "a"


def test_probe_attribute_prefers_digit_token_over_sentence_opener():
    items = [{"id": "a", "content": "The team uses Redis at 4096 keys.", "canonical_name": "Redis"}]
    assert rh.make_probes(items)[0]["query"] == "Redis 4096"


def test_explicit_attribute_overrides_content_tokens():
    items = [
        {
            "id": "a",
            "content": "pgdb retention is 33 days.",
            "canonical_name": "PostgreSQL",
            "attribute": "retention",
            "alias": "pgdb",
        }
    ]
    probe = rh.make_probes(items)[0]
    assert probe["query"] == "PostgreSQL retention"
    assert probe["alias"] == "pgdb"


def test_controls_target_another_item_and_are_marked():
    probes = [
        {"probe_id": "a", "target": "a", "query": "x", "split": "fit"},
        {"probe_id": "b", "target": "b", "query": "y", "split": "held"},
    ]
    controls = rh.make_controls(probes)
    assert [c["target"] for c in controls] == ["b", "a"]
    assert all(c["control"] for c in controls)
    assert rh.make_controls(probes[:1]) == []


def test_fix_rule_applies_to_held_items_of_the_same_label_group():
    items = [
        {"id": "p1", "content": "pgdb retention is 33 days.", "canonical_name": "PostgreSQL"},
        {"id": "p2", "content": "pgdb quota is 90 days.", "canonical_name": "PostgreSQL"},
        {"id": "k1", "content": "kfk retention is 44 days.", "canonical_name": "Kafka"},
    ]
    misses = [{"target": "p1", "alias": "pgdb", "canonical_name": "PostgreSQL"}]
    fixes = rh.propose_fixes(misses, items, "batch-1")
    # Both PostgreSQL items carry the alias; the Kafka item does not match.
    assert sorted(f["target"] for f in fixes) == ["p1", "p2"]
    assert all(f["metadata"]["trust_tier"] == "DERIVED" for f in fixes)
    assert all(f["metadata"]["beliefId"] == f["target"] for f in fixes)
    assert all(f["source"] == "rehearsal:batch-1" for f in fixes)


def test_fix_rule_ignores_misses_without_a_usable_alias():
    items = [{"id": "p1", "content": "pgdb retention", "canonical_name": "PostgreSQL"}]
    assert rh.propose_fixes([{"target": "p1", "canonical_name": "PostgreSQL"}], items, "b") == []
    assert (
        rh.propose_fixes(
            [{"target": "p1", "alias": "two words", "canonical_name": "PostgreSQL"}], items, "b"
        )
        == []
    )


def test_decide_keeps_only_when_all_three_conditions_hold():
    fixes = [{"fix_id": "f"}]
    before = {"probes": 5, "hits": 2, "wrong_top1": 1, "over_return": 4}
    better = {"probes": 5, "hits": 3, "wrong_top1": 1, "over_return": 4}
    assert rh.decide(before, better, 3, 3, fixes) is True
    assert rh.decide(before, before, 3, 3, fixes) is False  # no hit gain
    more_distractors = {"probes": 5, "hits": 3, "wrong_top1": 1, "over_return": 5}
    assert rh.decide(before, more_distractors, 3, 3, fixes) is False  # over-return rose
    assert rh.decide(before, better, 3, 4, fixes) is False  # fabricated rose
    assert rh.decide(before, better, 3, 3, []) is False  # no fixes, never kept


def test_armed_evaluation_keeps_the_inherited_environment():
    """`clean_environment` reads os.environ, so it has to run before the clear.

    Evaluating it after `os.environ.clear()` kept only its own defaults and
    dropped HOME/USERPROFILE/PATH, which the engine needs the moment it imports
    (`Path.home()` raises on Windows without USERPROFILE).
    """
    import contextlib
    import os
    import tempfile

    from bench.scorecard import runner

    marker = "REHEARSAL_ENV_MARKER"
    captured: dict[str, str] = {}

    def fake_create(cls, scratch, life_id, **kwargs):
        captured.update(dict(os.environ))
        raise RuntimeError("stop before the engine is touched")

    original_create = runner.LifeStore.create
    previous = os.environ.get(marker)
    os.environ[marker] = "1"
    runner.LifeStore.create = classmethod(fake_create)
    try:
        with tempfile.TemporaryDirectory() as tmp, contextlib.suppress(RuntimeError):
            rh.evaluate([], [], [], scratch_root=Path(tmp) / "scratch", tag="env")
    finally:
        runner.LifeStore.create = original_create
        if previous is None:
            os.environ.pop(marker, None)
        else:
            os.environ[marker] = previous

    assert captured.get(marker) == "1", "rehearsal dropped the inherited environment"
    assert "PATH" in captured
