"""Contract checks for the public PrecisionMemBench adapter replay."""

from bench.precisionmembench.run_eval import Adapter


class _RecallStore:
    def __init__(self, result_ids):
        self.result_ids = result_ids
        self.calls = 0

    def recall(self, _query, *, limit):
        assert limit == 20
        self.calls += 1
        return {"results": [{"id": result_id} for result_id in self.result_ids]}


def test_open_question_recall_routes_only_to_open_questions():
    question = {
        "_id": "question-1",
        "user_id": "test-user",
        "scope": ["domain:code"],
        "type": "open_question",
        "pinned": True,
        "superseded_by": None,
        "resolved_at": None,
    }
    adapter = Adapter(
        _RecallStore(["engine-question"]),
        {"question-1": question},
        {"engine-question": "question-1"},
    )

    context = adapter.build_context("test-user", ["domain:code"], "query")

    assert context["relevantBeliefs"] == []
    assert context["openQuestions"] == [question]


def test_search_never_returns_another_users_belief():
    foreign_belief = {
        "_id": "foreign-1",
        "user_id": "other-user",
        "scope": ["domain:code"],
        "type": "preference",
    }
    adapter = Adapter(
        _RecallStore(["engine-foreign"]),
        {"foreign-1": foreign_belief},
        {"engine-foreign": "foreign-1"},
    )

    context = adapter.build_context("test-user", ["domain:code"], "query")

    assert context["relevantBeliefs"] == []


def test_search_includes_beliefs_from_every_active_scope():
    code_belief = {
        "_id": "code-redis",
        "user_id": "test-user",
        "scope": ["domain:code"],
        "type": "preference",
    }
    writing_belief = {
        "_id": "writing-redis",
        "user_id": "test-user",
        "scope": ["domain:writing"],
        "type": "preference",
    }
    adapter = Adapter(
        _RecallStore(["engine-code", "engine-writing"]),
        {"code-redis": code_belief, "writing-redis": writing_belief},
        {"engine-code": "code-redis", "engine-writing": "writing-redis"},
    )

    context = adapter.build_context("test-user", ["domain:code", "domain:writing"], "Redis")

    assert {belief["_id"] for belief in context["relevantBeliefs"]} == {
        "code-redis",
        "writing-redis",
    }
    assert adapter.store.calls == 2


def test_search_and_relation_expansion_exclude_resolved_or_superseded_beliefs():
    active_relation = {
        "_id": "relation-1",
        "user_id": "test-user",
        "scope": ["domain:code"],
        "type": "relation",
        "participants": ["resolved-1", "superseded-1"],
    }
    resolved = {
        "_id": "resolved-1",
        "user_id": "test-user",
        "scope": ["domain:code"],
        "type": "preference",
        "resolved_at": "2026-10-01T00:00:00Z",
    }
    superseded = {
        "_id": "superseded-1",
        "user_id": "test-user",
        "scope": ["domain:code"],
        "type": "preference",
        "superseded_by": "replacement-1",
    }
    adapter = Adapter(
        _RecallStore(["engine-relation", "engine-resolved", "engine-superseded"]),
        {
            "relation-1": active_relation,
            "resolved-1": resolved,
            "superseded-1": superseded,
        },
        {
            "engine-relation": "relation-1",
            "engine-resolved": "resolved-1",
            "engine-superseded": "superseded-1",
        },
    )

    context = adapter.build_context("test-user", ["domain:code"], "query")

    assert {belief["_id"] for belief in context["relevantBeliefs"]} == {"relation-1"}
