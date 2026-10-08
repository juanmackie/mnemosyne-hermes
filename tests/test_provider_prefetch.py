"""P23 regressions for Hermes prompt quality, budgeting and prefetch cache."""

import contextlib
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402


class RecallBeam:
    def __init__(self, db_path=":memory:", rows=None):
        self.conn = sqlite3.connect(db_path)
        self.db_path = db_path
        self.session_id = "session-a"
        self.author_id = None
        self.channel_id = None
        self.canonical_owner_id = "owner-a"
        self.rows = list(rows or [])
        self.recall_calls = 0

    def recall(self, **kwargs):
        self.recall_calls += 1
        return list(self.rows)


def _provider(beam):
    instance = provider_mod.MnemosyneMemoryProvider()
    instance._beam = beam
    instance._agent_context = "interactive"
    instance._skip_contexts = set()
    instance._prefetch_profile = "general"
    return instance


def _row(content="User prefers concise summaries", source="preference"):
    return {
        "content": content,
        "source": source,
        "score": 0.9,
        "keyword_score": 0.8,
        "importance": 0.9,
        "timestamp": "2026-10-08T00:00:00",
    }


@contextlib.contextmanager
def _real_provider(tmp_path):
    """Create an isolated provider/engine pair and close every SQLite handle."""
    try:
        from mnemosyne.core.beam import BeamMemory
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            pytest.fail("Strict prefetch regression lane requires the audited engine")
        pytest.skip("mnemosyne-memory engine is not installed")

    db_path = tmp_path / "prefetch.db"
    connections = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    environment = {
        "HERMES_HOME": str(tmp_path),
        "MNEMOSYNE_DATA_DIR": str(tmp_path / "engine-data"),
        "MNEMOSYNE_EMBEDDINGS_OFF": "1",
        "MNEMOSYNE_PREFETCH_CACHE_ENABLED": "1",
    }
    instance = provider_mod.MnemosyneMemoryProvider()
    instance._init_audit_log = lambda: None
    instance._read_config_key = lambda key: None
    try:
        with (
            patch.dict(os.environ, environment),
            patch.object(sqlite3, "connect", side_effect=tracked_connect),
        ):
            instance.initialize(
                session_id="prefetch-test-session",
                hermes_home=str(tmp_path),
                agent_context="primary",
                db_path=str(db_path),
                shared_surface_path=str(tmp_path / "shared.db"),
            )
            if instance._beam is None:
                pytest.fail(
                    f"provider could not initialize with installed engine: {instance._init_error!r}"
                )
            yield instance, BeamMemory, db_path
    finally:
        with contextlib.suppress(Exception):
            instance.shutdown()
        for connection in connections:
            with contextlib.suppress(Exception):
                connection.close()
        for module_name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
            module = sys.modules.get(module_name)
            local = getattr(module, "_thread_local", None)
            if local is not None:
                local.conn = None


def test_real_engine_cache_observes_memory_writes_visibility_and_source_revisions(tmp_path):
    with _real_provider(tmp_path) as (instance, _, db_path):
        beam = instance._beam
        query = "cache coherence crimson indigo memory"
        beam.remember(
            "[EVAL_CACHE_BASE] crimson memory anchor red map",
            source="preference",
            importance=0.95,
            scope="session",
        )
        first = instance.prefetch(query, session_id="prefetch-test-session")
        second = instance.prefetch(query, session_id="prefetch-test-session")
        assert first == second and "EVAL_CACHE_BASE" in second
        assert instance.get_prefetch_diagnostics()["cache_hits"] == 1, (
            instance.get_prefetch_diagnostics()
        )

        misses = instance.get_prefetch_diagnostics()["cache_misses"]
        beam.remember(
            "[EVAL_CACHE_LOCAL_WRITE] crimson memory association",
            source="preference",
            importance=0.95,
            scope="session",
        )
        instance.prefetch(query, session_id="prefetch-test-session")
        assert instance.get_prefetch_diagnostics()["cache_misses"] == misses + 1

        # A separate thread commits a real store update through its own SQLite
        # connection. The provider connection's PRAGMA data_version must notice.
        write_errors = []

        def external_write():
            writer = None
            try:
                writer = sqlite3.connect(db_path)
                memory_id = writer.execute("SELECT id FROM working_memory LIMIT 1").fetchone()[0]
                writer.execute(
                    "UPDATE working_memory SET importance = importance + 0.01 WHERE id = ?",
                    (memory_id,),
                )
                writer.commit()
            except Exception as exc:  # surfaced in the main test thread
                write_errors.append(exc)
            finally:
                if writer is not None:
                    writer.close()

        worker = threading.Thread(target=external_write)
        worker.start()
        worker.join(timeout=20)
        assert not worker.is_alive(), "external engine writer did not finish"
        assert not write_errors, write_errors
        instance.prefetch(query, session_id="prefetch-test-session")
        assert instance.get_prefetch_diagnostics()["cache_misses"] >= 2

        # The cache key follows runtime visibility and profile settings even
        # when the query text remains identical.
        instance.prefetch(query, session_id="prefetch-test-session")
        misses = instance.get_prefetch_diagnostics()["cache_misses"]
        instance.on_session_switch("prefetch-test-session-rotated")
        instance.prefetch(query, session_id="prefetch-test-session")
        assert instance.get_prefetch_diagnostics()["cache_misses"] == misses + 1

        instance._agent_identity = "prefetch-owner-b"
        instance.prefetch(query, session_id="prefetch-test-session")
        assert instance.get_prefetch_diagnostics()["cache_misses"] == misses + 2

        alternate_profile = provider_mod.PrefetchProfile(
            name="p23-engine-cache-profile-alt", min_score=0.01
        )
        provider_mod.register_profile(alternate_profile)
        profile = provider_mod.PrefetchProfile(
            name="p23-engine-cache-profile", sources=("bank", "fixture")
        )
        provider_mod.register_profile(profile)
        try:
            instance._prefetch_profile = alternate_profile.name
            instance.prefetch(query, session_id="prefetch-test-session")
            assert instance.get_prefetch_diagnostics()["cache_misses"] == misses + 3

            calls = []

            def custom_source(query, *, session_id):
                calls.append((query, session_id))
                return "## Fixture Context\n  fixture source result"

            instance.register_prefetch_source("fixture", custom_source)
            instance._prefetch_profile = profile.name
            instance.prefetch(query, session_id="prefetch-test-session")
            instance.prefetch(query, session_id="prefetch-test-session")
            assert len(calls) == 2, "non-versioned external sources must bypass cache"

            custom_source.cache_revision = "v1"
            instance.prefetch(query, session_id="prefetch-test-session")
            instance.prefetch(query, session_id="prefetch-test-session")
            assert len(calls) == 3, (
                "a stable custom source revision should allow an exact-query hit"
            )
            custom_source.cache_revision = "v2"
            instance.prefetch(query, session_id="prefetch-test-session")
            assert len(calls) == 4, "changing a custom source revision must invalidate the hit"
        finally:
            provider_mod._BUILTIN_PROFILES.pop(profile.name, None)
            provider_mod._BUILTIN_PROFILES.pop(alternate_profile.name, None)


def test_system_prompt_matches_native_memory_and_configured_tools():
    instance = _provider(RecallBeam())
    instance._configured_tool_names = lambda: {"mnemosyne_remember", "mnemosyne_recall"}
    instance._with_persona_block = lambda value: value

    prompt = instance.system_prompt_block()

    assert "MEMORY.md and USER.md" in prompt
    assert "Configured Mnemosyne tools: mnemosyne_recall, mnemosyne_remember" in prompt
    assert "deprecated" not in prompt
    assert "mnemosyne_shared_*" not in prompt
    instance._beam.conn.close()


def test_complete_prefetch_output_obeys_budget_and_marks_truncation():
    blocks = [
        "## Mnemosyne Context\n  [IDENTITY] " + "identity fact " * 30,
        "## Mnemosyne Context\n  [preference] " + "optional fact " * 50,
    ]

    rendered, truncated = provider_mod._budget_prefetch_blocks(blocks, 256)

    assert len(rendered) <= 256
    assert "[IDENTITY]" in rendered
    assert "[truncated]" in rendered
    assert truncated


def test_long_identity_line_does_not_consume_all_space_for_other_anchors():
    blocks = [
        "## Mnemosyne Context\n  [IDENTITY] " + "long anchor " * 1800,
        "## Mnemosyne Context\n  [IDENTITY] [EVAL_SECOND_ANCHOR] Casey is a designer.",
    ]

    rendered, truncated = provider_mod._budget_prefetch_blocks(blocks, 8000)

    assert len(rendered) <= 8000
    assert "[IDENTITY]" in rendered
    assert "[truncated]" in rendered
    assert "[EVAL_SECOND_ANCHOR]" in rendered
    assert truncated


def test_tool_and_delegation_transcripts_are_raw_and_not_silently_injected():
    tool = _row("[TOOL] command output contains details", "conversation_tool")
    delegation = _row("[DELEGATION] child task result", "conversation_delegation")

    assert provider_mod._prefetch_is_raw(tool)
    assert provider_mod._prefetch_is_raw(delegation)
    assert provider_mod._prefetch_source_quality(tool) == 0
    assert provider_mod._prefetch_source_quality(delegation) == 0

    with patch.dict("os.environ", {"MNEMOSYNE_PREFETCH_INCLUDE_RAW_TOOL_CAPTURE": "1"}):
        assert provider_mod._prefetch_source_quality(tool) > 0


def test_tool_transcript_is_skipped_by_the_automatic_bank_prefetch():
    beam = RecallBeam(rows=[_row("[TOOL] command output", "conversation_tool")])
    instance = _provider(beam)

    with patch.dict(
        "os.environ",
        {
            "MNEMOSYNE_PREFETCH_CACHE_ENABLED": "0",
            "MNEMOSYNE_PREFETCH_INCLUDE_RAW_TOOL_CAPTURE": "0",
        },
    ):
        assert instance.prefetch("command output details", session_id="session-a") == ""

    assert instance.get_prefetch_diagnostics()["bank"]["skipped_source"] == 1
    beam.conn.close()


def test_exact_query_prewarm_hits_cache_and_reports_pii_safe_metrics():
    beam = RecallBeam(rows=[_row()])
    instance = _provider(beam)
    query = "What summaries do I prefer?"

    with patch.dict("os.environ", {"MNEMOSYNE_PREFETCH_CACHE_ENABLED": "1"}):
        instance.queue_prefetch(query, session_id="session-a")
        warmed = instance.prefetch(query, session_id="session-a")

    assert warmed and "User prefers concise summaries" in warmed
    assert beam.recall_calls == 1
    stats = instance.get_prefetch_diagnostics()
    assert stats["queued"] == 1
    assert stats["cache_hits"] == 1
    assert stats["cache_misses"] == 1
    assert stats["last_chars"] == len(warmed)
    assert "query" not in stats and "content" not in stats
    beam.conn.close()


def test_cache_invalidates_for_local_and_external_database_writes():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(pathlib.Path(tmp) / "memory.db")
        beam = RecallBeam(db_path=db_path, rows=[_row()])
        instance = _provider(beam)
        query = "What summaries do I prefer?"

        with patch.dict("os.environ", {"MNEMOSYNE_PREFETCH_CACHE_ENABLED": "1"}):
            instance.prefetch(query, session_id="session-a")
            instance.prefetch(query, session_id="session-a")
            assert beam.recall_calls == 1

            beam.conn.execute("CREATE TABLE cache_generation (value TEXT)")
            beam.conn.execute("INSERT INTO cache_generation VALUES ('local write')")
            beam.conn.commit()
            instance.prefetch(query, session_id="session-a")
            assert beam.recall_calls == 2

            external = sqlite3.connect(db_path)
            external.execute("INSERT INTO cache_generation VALUES ('external write')")
            external.commit()
            external.close()
            instance.prefetch(query, session_id="session-a")
            assert beam.recall_calls == 3
        beam.conn.close()


def test_queue_prefetch_is_opt_in_until_hit_rate_is_measured():
    beam = RecallBeam(rows=[_row()])
    instance = _provider(beam)

    with patch.dict("os.environ", {"MNEMOSYNE_PREFETCH_CACHE_ENABLED": "0"}):
        instance.queue_prefetch("a repeated query", session_id="session-a")

    assert beam.recall_calls == 0
    assert instance.get_prefetch_diagnostics()["cache_enabled"] is False
    beam.conn.close()


def test_prefetch_diagnostics_distinguish_store_error_from_no_match():
    beam = RecallBeam()
    beam.conn.execute(
        "CREATE TABLE working_memory (content TEXT, importance REAL, timestamp TEXT, source TEXT, session_id TEXT)"
    )
    beam.recall = lambda **kwargs: (_ for _ in ()).throw(
        sqlite3.DatabaseError("private store path")
    )
    instance = _provider(beam)
    instance._prefetch_model_slots = lambda query, profile: ""

    assert instance.prefetch("a query", session_id="session-a") == ""
    diagnostics = instance.get_prefetch_diagnostics()
    assert diagnostics["last_error"] == "bank: DatabaseError"
    assert diagnostics["errors_by_source"] == {"bank": 1}
    assert "private store path" not in str(diagnostics)
    beam.conn.close()
