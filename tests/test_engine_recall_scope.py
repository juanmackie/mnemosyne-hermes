"""Precision regressions for lexical cross-topic and long-query candidates.

Run with the pinned engine installed:

    python tests/test_engine_recall_scope.py

All records, engine config and databases live under a temporary directory.
"""

import contextlib
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch

REQUIRE_ENGINE = os.environ.get("MNEMOSYNE_REQUIRE_ENGINE", "0") == "1"


def _require_engine():
    try:
        from mnemosyne.core.memory import Mnemosyne

        return Mnemosyne
    except ImportError:
        message = (
            "skip: mnemosyne-memory engine not importable "
            "(install ./integrations/hermes-provider to run this lane; "
            "MNEMOSYNE_REQUIRE_ENGINE=1 makes this a failure)"
        )
        if REQUIRE_ENGINE:
            raise AssertionError(message) from None
        print(message)
        return None


@contextlib.contextmanager
def _isolated_engine(tmp):
    keys = (
        "MNEMOSYNE_DATA_DIR",
        "MNEMOSYNE_EMBEDDINGS_OFF",
        "MNEMOSYNE_RECALL_SCORE_FLOOR",
        "HERMES_HOME",
    )
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(tmp, "engine-data")
    os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
    os.environ["HERMES_HOME"] = tmp
    os.environ.pop("MNEMOSYNE_RECALL_SCORE_FLOOR", None)

    connections = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append((conn, threading.get_ident()))
        return conn

    try:
        with patch.object(sqlite3, "connect", side_effect=tracked_connect):
            yield
    finally:
        for conn, owner_thread in connections:
            if owner_thread == threading.get_ident():
                with contextlib.suppress(Exception):
                    conn.close()
        for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
            module = sys.modules.get(name)
            local = getattr(module, "_thread_local", None)
            if local is not None:
                local.conn = None
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _recall_pair(Mnemosyne, tmp, memories, query, floor="0.75"):
    store = Mnemosyne(
        session_id="precision-scope-repro",
        db_path=pathlib.Path(tmp) / "recall.db",
    )
    for content, metadata in memories:
        store.remember(content, metadata=metadata, scope="session")

    os.environ.pop("MNEMOSYNE_RECALL_SCORE_FLOOR", None)
    baseline = store.recall(query, top_k=20)
    os.environ["MNEMOSYNE_RECALL_SCORE_FLOOR"] = floor
    gated = store.recall(query, top_k=20)
    return baseline, gated


def test_writing_query_drops_lexical_hit_from_code_domain():
    Mnemosyne = _require_engine()
    if Mnemosyne is None:
        return

    novel = "In the novel, chapter opening: Mara waits at the door before leaving for the coast."
    redis = "In Redis caching code, writing a value uses SET with an expiry."
    memories = [
        (novel, {"domain": "novel-writing"}),
        (redis, {"domain": "software-code"}),
    ]
    query = "How should I open the next chapter with Mara? writing"

    with tempfile.TemporaryDirectory() as tmp, _isolated_engine(tmp):
        baseline, gated = _recall_pair(Mnemosyne, tmp, memories, query)

    # The metadata labels characterize the benchmark fixture. The engine has
    # no domain-filter API; the score floor tests the explicit recall gate.
    assert {row["content"] for row in baseline} == {novel, redis}, baseline
    assert [row["content"] for row in gated] == [novel], gated


def test_long_query_suppresses_noise_and_keeps_multi_topic_results():
    Mnemosyne = _require_engine()
    if Mnemosyne is None:
        return

    kubernetes = (
        "Kubernetes deployment uses Helm with a values file and a rolling "
        "update for the landing page service."
    )
    redis = "Redis cache writes values with SET and expiry."
    novel = "Mara opens the next chapter at the coast."
    postgres = "PostgreSQL backup uses pg_dump and restore checks."
    prometheus = "Prometheus alerts notify operators when CPU saturation rises."
    travel = "For travel, prefer direct flights and quiet hotels."
    memories = [(text, {}) for text in (kubernetes, redis, novel, postgres, prometheus, travel)]
    rambling = (
        "ok so I have been thinking about this for a while and need the "
        "Kubernetes rolling deployment recipe with Helm for our landing page, "
        "and we also discussed Redis, Mara chapter, PostgreSQL backups, "
        "Prometheus alerts, travel preferences, access controls and billing decisions."
    )

    with tempfile.TemporaryDirectory() as tmp, _isolated_engine(tmp):
        baseline, gated = _recall_pair(Mnemosyne, tmp, memories, rambling)
        store = Mnemosyne(
            session_id="precision-scope-repro",
            db_path=pathlib.Path(tmp) / "recall.db",
        )
        multi_topic = store.recall(
            "Kubernetes deployment uses Helm; PostgreSQL backup uses pg_dump.",
            top_k=20,
        )

    assert {row["content"] for row in baseline} == {
        kubernetes,
        redis,
        novel,
        postgres,
        prometheus,
        travel,
    }, baseline
    assert [row["content"] for row in gated] == [kubernetes], gated
    assert {row["content"] for row in multi_topic} == {kubernetes, postgres}, multi_topic


def test_multi_term_query_keeps_single_precise_match():
    """A multi-term query must not discard its only matching row.

    The stepped gate asked a three-token query for half its terms, so
    "laptop specs - what machine do I have?" returned nothing even though the
    ThinkPad row was the only laptop in scope. Coverage now lowers the score
    instead of gating admission; the zero-overlap case still abstains.
    """
    Mnemosyne = _require_engine()
    if Mnemosyne is None:
        return

    laptop = "My laptop is a ThinkPad X1 Carbon Gen 12 with 64GB RAM running Ubuntu 24.04."
    kitchen = "The office kitchen is cleaned every Friday."
    memories = [(laptop, {"domain": "hardware"}), (kitchen, {"domain": "office"})]
    query = "laptop specs - what machine do I have?"

    with tempfile.TemporaryDirectory() as tmp, _isolated_engine(tmp):
        store = Mnemosyne(
            session_id="recall-lexical-floor",
            db_path=pathlib.Path(tmp) / "recall.db",
        )
        for content, metadata in memories:
            store.remember(content, metadata=metadata, scope="session")
        results = store.recall(query, top_k=5)

    assert results, "a multi-term query dropped its only matching row"
    assert results[0]["content"] == laptop, results


def test_zero_overlap_query_still_returns_nothing():
    """Lowering the admission floor must not turn nonsense into results."""
    Mnemosyne = _require_engine()
    if Mnemosyne is None:
        return

    with tempfile.TemporaryDirectory() as tmp, _isolated_engine(tmp):
        store = Mnemosyne(
            session_id="recall-lexical-floor",
            db_path=pathlib.Path(tmp) / "recall.db",
        )
        store.remember(
            "My laptop is a ThinkPad X1 Carbon Gen 12 with 64GB RAM.",
            scope="session",
        )
        results = store.recall("purple bicycle quantum oatmeal", top_k=5)

    assert results == [], results


if __name__ == "__main__":
    tests = [
        test_writing_query_drops_lexical_hit_from_code_domain,
        test_long_query_suppresses_noise_and_keeps_multi_topic_results,
        test_multi_term_query_keeps_single_precise_match,
        test_zero_overlap_query_still_returns_nothing,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
