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


def test_strict_label_abstain_opt_out_preserves_paraphrase_results(monkeypatch) -> None:
    """P54: MNEMOSYNE_LABEL_STRICT_ABSTAIN=0 preserves engine results on labeled paraphrases."""
    monkeypatch.setenv("MNEMOSYNE_LABEL_STRICT_ABSTAIN", "0")
    provider = Provider()
    provider._shared_surface_read = False
    beam = FakeBeam([[_row()]])
    provider._beam = beam

    response = json.loads(
        provider._handle_recall(
            {"query": "do we prefer strong static typing for our frontend?", "limit": 5}
        )
    )

    assert response["count"] == 1
    assert response["results"][0]["id"] == "m1"


def test_hydrate_search_label_index_recovers_labels_from_sqlite() -> None:
    """P57: P50 typo-recovery index hydrates from existing SQLite rows on initialize."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE working_memory (id TEXT, session_id TEXT, scope TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE episodic_memory (id TEXT, session_id TEXT, scope TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO working_memory VALUES (?, ?, ?, ?)",
        (
            "m1",
            "session-a",
            "session",
            json.dumps({"canonical_name": "language_typescript", "aliases": ["TypeScript"]}),
        ),
    )
    conn.execute(
        "INSERT INTO working_memory VALUES (?, ?, ?, ?)",
        (
            "m-other",
            "session-other",
            "session",
            json.dumps({"canonical_name": "secret_other", "aliases": ["OtherPrivate"]}),
        ),
    )
    conn.execute(
        "INSERT INTO episodic_memory VALUES (?, ?, ?, ?)",
        (
            "e1",
            "session-other",
            "global",
            json.dumps({"canonical_name": "cache_redis", "aliases": ["Redis"]}),
        ),
    )
    conn.commit()
    try:
        provider = Provider()
        provider._session_id = "session-a"
        provider._beam = type("StubBeam", (), {"conn": conn, "session_id": "session-a"})()
        provider._hydrate_search_label_index()
        assert provider._search_label_index["typescript"] == "TypeScript"
        assert provider._search_label_index["redis"] == "Redis"
        assert "otherprivate" not in provider._search_label_index
    finally:
        conn.close()


def test_recall_batches_metadata_lookup_once_across_labels_and_provenance() -> None:
    """P57: _handle_recall resolves metadata once (no double beam.get() per row)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE working_memory (id TEXT, metadata_json TEXT)")
    conn.execute("CREATE TABLE episodic_memory (id TEXT, metadata_json TEXT)")
    meta = {
        "canonical_name": "language_typescript",
        "aliases": ["TypeScript"],
        "provenance": {
            "model": "test-model",
            "engine_version": "3.15.1",
            "recorded_at": "2026-11-26T00:00:00Z",
        },
    }
    conn.execute("INSERT INTO working_memory VALUES (?, ?)", ("m1", json.dumps(meta)))
    conn.commit()

    class CountingBeam:
        def __init__(self) -> None:
            self.conn = conn
            self.get_calls = 0

        def recall(self, _query: str, **_kwargs: object) -> list[dict]:
            return [{"id": "m1", "content": "TypeScript with strict mode"}]

        def get(self, _memory_id: str) -> dict | None:
            self.get_calls += 1
            return {"id": "m1", "metadata": json.dumps(meta)}

    try:
        beam = CountingBeam()
        provider = Provider()
        provider._shared_surface_read = False
        provider._beam = beam
        response = json.loads(provider._handle_recall({"query": "TypeScript config", "limit": 5}))
        assert response["count"] == 1
        assert "test-model" in response["results"][0]["learned_under"]
        assert beam.get_calls == 0, "real SQLite batch query should avoid N+1 beam.get() calls"
    finally:
        conn.close()
