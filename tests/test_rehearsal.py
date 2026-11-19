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


def test_controls_target_another_item_and_are_marked():
    probes = [
        {"probe_id": "a", "target": "a", "query": "x", "split": "fit"},
        {"probe_id": "b", "target": "b", "query": "y", "split": "held"},
    ]
    controls = rh.make_controls(probes)
    assert [c["target"] for c in controls] == ["b", "a"]
    assert all(c["control"] for c in controls)
    assert rh.make_controls(probes[:1]) == []


def test_fixes_only_for_single_token_attributes():
    misses = [
        {"target": "a", "attribute": "4096", "canonical_name": "Redis"},
        {"target": "b", "attribute": "QX-7741", "canonical_name": "Vault"},
    ]
    fixes = rh.propose_fixes(misses, "batch-1")
    assert [f["target"] for f in fixes] == ["a"]
    assert fixes[0]["metadata"]["trust_tier"] == "DERIVED"
    assert fixes[0]["source"] == "rehearsal:batch-1"


def test_decide_keeps_only_when_all_three_conditions_hold():
    fixes = [{"fix_id": "f"}]
    before = {"probes": 5, "hits": 2, "wrong_top1": 1}
    better = {"probes": 5, "hits": 3, "wrong_top1": 1}
    assert rh.decide(before, better, 3, 3, fixes) is True
    assert rh.decide(before, before, 3, 3, fixes) is False  # no hit gain
    assert rh.decide(before, {"probes": 5, "hits": 3, "wrong_top1": 2}, 3, 3, fixes) is False
    assert rh.decide(before, better, 3, 4, fixes) is False  # fabricated rose
    assert rh.decide(before, better, 3, 3, []) is False  # no fixes, never kept
