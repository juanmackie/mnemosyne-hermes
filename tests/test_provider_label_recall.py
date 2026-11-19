"""Engine-free regression coverage for P49/P50/P51 structured-label recall.

The engine-backed PrecisionMemBench replay drives the end-to-end path; these
tests pin the small provider-side pieces that made its 43/43 result possible,
so a later re-vendor cannot silently drop them.
"""

from __future__ import annotations

import json

import pytest
from hermes_memory_provider import MnemosyneMemoryProvider as Provider


class FakeBeam:
    """Returns canned recall responses and records the queries it was given."""

    def __init__(self, responses: list[list[dict]]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def recall(self, query: str, **_kwargs: object) -> list[dict]:
        self.calls.append(query)
        return self.responses.pop(0) if self.responses else []


def _row(memory_id: str = "m1") -> dict:
    return {
        "id": memory_id,
        "content": "TypeScript with strict mode",
        "metadata": {
            "canonical_name": "language_typescript",
            "aliases": ["TypeScript", "TS"],
        },
    }


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("typscript", "typescript", True),
        ("typsecript", "typescript", True),
        ("mongod", "mongo", True),
        ("eror", "error", True),
        ("mango", "mongo", False),  # prefix guard: ma != mo
        ("s", "ts", False),  # P52: one-edit needs two chars on the shorter side
        ("a", "ab", False),  # P52
        ("ab", "abc", True),  # insertion of one char keeps the two-char floor
        ("typescript", "redis", False),
    ],
)
def test_within_one_edit(left: str, right: str, expected: bool) -> None:
    assert Provider._within_one_edit(left, right) is expected


@pytest.mark.parametrize(
    "query,label,expected",
    [
        ("how do I configure typscript strict mode?", "TypeScript", True),
        ("typsecript compilation config", "TypeScript", True),
        ("mongod", "Mongo", True),
        ("eror handling patterns", "error", True),
        ("how do I configure typscript strict mode?", "redis_cache", False),
        ("mongod", "dead letter", False),
    ],
)
def test_query_contains_label_fuzzy(query: str, label: str, expected: bool) -> None:
    assert Provider._query_contains_label(query, label, fuzzy=True) is expected


def test_label_index_is_bounded_and_single_token_only() -> None:
    provider = Provider()
    provider._index_search_labels({"canonical_name": "Redis", "aliases": ["auth redis", "A"]})
    assert provider._search_label_index == {"redis": "Redis"}


def test_zero_result_recall_retries_with_expanded_query() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    provider._index_search_labels(
        {"canonical_name": "language_typescript", "aliases": ["TypeScript"]}
    )
    beam = FakeBeam([[], [_row()]])
    provider._beam = beam

    response = json.loads(provider._handle_recall({"query": "typsecript config", "limit": 5}))

    assert response["count"] == 1
    assert beam.calls == ["typsecript config", "TypeScript config"]


def test_nonzero_result_recall_does_not_retry() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    provider._index_search_labels(
        {"canonical_name": "language_typescript", "aliases": ["TypeScript"]}
    )
    beam = FakeBeam([[_row()]])
    provider._beam = beam

    response = json.loads(provider._handle_recall({"query": "typsecript config", "limit": 5}))

    assert response["count"] == 1
    assert beam.calls == ["typsecript config"]


def test_abstains_when_no_labeled_row_matches_the_query() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    beam = FakeBeam([[_row()]])
    provider._beam = beam

    response = json.loads(
        provider._handle_recall({"query": "Vue component architecture", "limit": 5})
    )

    assert response["count"] == 0
    assert response["results"] == []


def test_abstains_only_when_every_row_is_labeled() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    row = _row()
    del row["metadata"]
    beam = FakeBeam([[row]])
    provider._beam = beam

    response = json.loads(provider._handle_recall({"query": "what's our stack?", "limit": 5}))

    assert response["count"] == 1


def test_unlabeled_store_never_retries() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    assert provider._search_label_index == {}
    beam = FakeBeam([[]])
    provider._beam = beam

    response = json.loads(provider._handle_recall({"query": "anything", "limit": 5}))

    assert response["count"] == 0
    assert beam.calls == ["anything"]


def test_abstaining_recall_retries_with_restored_query() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    provider._index_search_labels(
        {"canonical_name": "language_typescript", "aliases": ["TypeScript"]}
    )
    wrong = {
        "id": "wrong",
        "content": "Redis cache",
        "metadata": {"canonical_name": "redis_cache", "aliases": ["Redis"]},
    }
    beam = FakeBeam([[wrong], [_row()]])
    provider._beam = beam

    response = json.loads(provider._handle_recall({"query": "typsecript config", "limit": 5}))

    assert response["count"] == 1
    assert beam.calls == ["typsecript config", "TypeScript config"]


def test_abstaining_recall_without_expandable_tokens_returns_zero() -> None:
    provider = Provider()
    provider._shared_surface_read = False
    wrong = {
        "id": "wrong",
        "content": "Redis cache",
        "metadata": {"canonical_name": "redis_cache", "aliases": ["Redis"]},
    }
    beam = FakeBeam([[wrong]])
    provider._beam = beam

    response = json.loads(
        provider._handle_recall({"query": "Vue component architecture", "limit": 5})
    )

    assert response["count"] == 0
    assert beam.calls == ["Vue component architecture"]
