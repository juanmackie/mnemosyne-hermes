"""Read-only provider inspection and history CLI contract tests."""

import contextlib
import hashlib
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

from hermes_memory_provider import cli  # noqa: E402


def _make_store(path, *, audit=True):
    conn = sqlite3.connect(path)
    for table in ("working_memory", "episodic_memory"):
        extra = ", summary_of TEXT" if table == "episodic_memory" else ""
        conn.execute(
            f"CREATE TABLE {table} (id TEXT PRIMARY KEY, content TEXT, source TEXT, "
            f"session_id TEXT, scope TEXT, metadata_json TEXT, timestamp TEXT{extra})"
        )
    conn.execute(
        "INSERT INTO working_memory VALUES (?, ?, ?, ?, ?, ?, ?) ",
        ("own", "own content", "conversation", "session-a", "session", "{}", "2026-10-09"),
    )
    conn.execute(
        "INSERT INTO working_memory VALUES (?, ?, ?, ?, ?, ?, ?) ",
        ("foreign", "foreign content", "conversation", "session-b", "session", "{}", "2026-10-09"),
    )
    conn.execute(
        "INSERT INTO working_memory VALUES (?, ?, ?, ?, ?, ?, ?) ",
        ("global", "global content", "conversation", "session-b", "global", "{}", "2026-10-09"),
    )
    if audit:
        conn.execute(
            "CREATE TABLE audit_log (event_id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, "
            "action TEXT NOT NULL, memory_id TEXT, bank TEXT, scope TEXT, profile TEXT, "
            "session_id TEXT, source_tool TEXT, tokens_used INTEGER, reason TEXT, metadata_json TEXT)"
        )
        conn.executemany(
            "INSERT INTO audit_log (event_id, timestamp, action, memory_id, scope, profile, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1.0, "remember", "own", "session", "profile-a", "session-a"),
                (2, 2.0, "forget", "deleted", "session", "profile-a", "session-a"),
                (3, 3.0, "remember", "foreign", "session", "profile-b", "session-b"),
                (4, 4.0, "remember", "global", "global", "profile-b", "session-b"),
            ],
        )
    conn.commit()
    conn.close()


def _capture(call):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        call()
    return json.loads(output.getvalue())


def test_inspect_by_id_reads_current_and_global_rows_without_mutation():
    with tempfile.TemporaryDirectory() as temp:
        db = pathlib.Path(temp) / "engine.db"
        _make_store(db)
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        with patch.object(cli, "resolve_effective_db_path", return_value=str(db)):
            own = _capture(
                lambda: cli._readonly_inspect(
                    Namespace(
                        memory_id="own",
                        query="",
                        session_id="session-a",
                        all_sessions=False,
                        limit=1,
                    )
                )
            )
            global_row = _capture(
                lambda: cli._readonly_inspect(
                    Namespace(
                        memory_id="global",
                        query="",
                        session_id="session-a",
                        all_sessions=False,
                        limit=1,
                    )
                )
            )
            foreign = _capture(
                lambda: cli._readonly_inspect(
                    Namespace(
                        memory_id="foreign",
                        query="",
                        session_id="session-a",
                        all_sessions=False,
                        limit=1,
                    )
                )
            )
        assert own["status"] == "found"
        assert global_row["memory"]["scope"] == "global"
        assert foreign["status"] == "not_found"
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_inspect_preserves_truncation_flags_from_bounded_snapshot():
    with tempfile.TemporaryDirectory() as temp:
        db = pathlib.Path(temp) / "engine.db"
        _make_store(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO working_memory VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("large", "x" * 5000, "conversation", "session-a", "session", "{}", "2026-10-09"),
        )
        conn.commit()
        conn.close()
        with patch.object(cli, "resolve_effective_db_path", return_value=str(db)):
            result = _capture(
                lambda: cli._readonly_inspect(
                    Namespace(
                        memory_id="large",
                        query="",
                        session_id="session-a",
                        all_sessions=False,
                        limit=1,
                    )
                )
            )
        assert len(result["memory"]["content"]) == 4000
        assert result["memory"]["content_truncated"] is True


def test_history_filters_scope_profile_and_keeps_deleted_owner_event():
    with tempfile.TemporaryDirectory() as temp:
        db = pathlib.Path(temp) / "engine.db"
        _make_store(db)
        with patch.object(cli, "resolve_effective_db_path", return_value=str(db)):
            deleted = _capture(
                lambda: cli._readonly_history(
                    Namespace(
                        memory_id="deleted",
                        session_id="session-a",
                        all_sessions=False,
                        profile=None,
                        limit=50,
                    )
                )
            )
            foreign = _capture(
                lambda: cli._readonly_history(
                    Namespace(
                        memory_id="foreign",
                        session_id="session-a",
                        all_sessions=False,
                        profile=None,
                        limit=50,
                    )
                )
            )
            wrong_profile = _capture(
                lambda: cli._readonly_history(
                    Namespace(
                        memory_id="own",
                        session_id="session-a",
                        all_sessions=False,
                        profile="profile-b",
                        limit=50,
                    )
                )
            )
            global_event = _capture(
                lambda: cli._readonly_history(
                    Namespace(
                        memory_id="global",
                        session_id="session-a",
                        all_sessions=False,
                        profile=None,
                        limit=50,
                    )
                )
            )
            operator = _capture(
                lambda: cli._readonly_history(
                    Namespace(
                        memory_id="foreign",
                        session_id="session-a",
                        all_sessions=True,
                        profile=None,
                        limit=50,
                    )
                )
            )
        assert [event["action"] for event in deleted["events"]] == ["forget"]
        assert foreign["events"] == []
        assert wrong_profile["events"] == []
        assert [event["memory_id"] for event in global_event["events"]] == ["global"]
        assert operator["count"] == 1
        assert "partial" in deleted["coverage"]


def test_inspection_errors_fail_loud_and_do_not_create_database():
    with tempfile.TemporaryDirectory() as temp:
        missing = pathlib.Path(temp) / "missing.db"
        with (
            patch.object(cli, "resolve_effective_db_path", return_value=str(missing)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                cli._readonly_inspect(
                    Namespace(
                        memory_id="id",
                        query="",
                        session_id="s",
                        all_sessions=False,
                        limit=1,
                    )
                )
            except SystemExit as exc:
                assert exc.code == 1
            else:
                raise AssertionError("missing database must fail inspection")
        assert not missing.exists()

        no_audit = pathlib.Path(temp) / "no-audit.db"
        _make_store(no_audit, audit=False)
        with (
            patch.object(cli, "resolve_effective_db_path", return_value=str(no_audit)),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                cli._readonly_history(
                    Namespace(
                        memory_id="id",
                        session_id="s",
                        all_sessions=False,
                        profile=None,
                        limit=50,
                    )
                )
            except SystemExit as exc:
                assert exc.code == 1
            else:
                raise AssertionError("missing audit table must fail history reads")


def test_inspect_rejects_query_and_id_together():
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            cli._readonly_inspect(
                Namespace(
                    memory_id="id",
                    query="query",
                    session_id="s",
                    all_sessions=False,
                    limit=1,
                )
            )
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("query and --id must be mutually exclusive")


def test_provider_audit_retarget_and_shutdown_close_writes():
    from hermes_memory_provider import MnemosyneMemoryProvider
    from hermes_memory_provider.audit import read_audit_history

    with tempfile.TemporaryDirectory() as temp:
        first_db = pathlib.Path(temp) / "first.db"
        second_db = pathlib.Path(temp) / "second.db"
        missing_db = pathlib.Path(temp) / "missing.db"
        _make_store(first_db)
        _make_store(second_db)
        provider = MnemosyneMemoryProvider()
        provider._beam = SimpleNamespace(db_path=str(first_db))
        provider._init_audit_log()
        first_audit = provider._audit
        provider._audit_event(
            "first_target",
            memory_id="first",
            scope="session",
            session_id="session-a",
        )

        provider._beam = SimpleNamespace(db_path=str(second_db))
        provider._init_audit_log()
        second_audit = provider._audit
        assert first_audit.diagnostics()["state"] == "closed"
        provider._audit_event(
            "second_target",
            memory_id="second",
            scope="session",
            session_id="session-a",
        )

        provider._beam = SimpleNamespace(db_path=str(missing_db))
        provider._init_audit_log()
        failed_target = provider._audit
        assert second_audit.diagnostics()["state"] == "closed"
        assert provider.get_audit_diagnostics()["state"] == "unavailable"
        provider._audit_event(
            "failed_retarget",
            memory_id="missing",
            scope="session",
            session_id="session-a",
        )
        assert not missing_db.exists()

        provider.shutdown()
        assert provider._audit is failed_target
        assert provider.get_audit_diagnostics()["state"] == "closed"
        failures_before = provider.get_audit_diagnostics()["counters"]["write_failures"]
        provider._audit_event(
            "after_shutdown",
            memory_id="missing",
            scope="session",
            session_id="session-a",
        )
        assert provider.get_audit_diagnostics()["counters"]["write_failures"] == failures_before + 1

        first_events = read_audit_history(first_db, memory_id="first", session_id="session-a")
        second_events = read_audit_history(
            second_db,
            memory_id="second",
            session_id="session-a",
        )
        assert [event["action"] for event in first_events] == ["first_target"]
        assert [event["action"] for event in second_events] == ["second_target"]


def test_sync_turn_attaches_only_exact_host_message_references():
    from hermes_memory_provider import MnemosyneMemoryProvider

    class CaptureBeam:
        def __init__(self):
            self.items = []

        def remember(self, **kwargs):
            self.items.append(kwargs)
            return f"memory-{len(self.items)}"

    provider = MnemosyneMemoryProvider()
    provider._beam = CaptureBeam()
    provider._agent_context = "primary"
    provider._skip_contexts = set()
    provider._sync_roles = {"user", "assistant", "tool"}
    provider._current_session_id = "host-session-42"
    provider._auto_sleep_enabled = False
    user_text = "A stable user statement for source tracing."
    assistant_text = "A stable assistant response for source tracing."
    tool_text = "A stable tool result for source tracing."
    messages = [
        {
            "role": "user",
            "content": user_text,
            "id": "message-user",
            "session_id": "host-session-42",
        },
        {
            "role": "assistant",
            "content": assistant_text,
            "id": "message-assistant",
            "session_id": "host-session-42",
        },
        {
            "role": "tool",
            "content": tool_text,
            "id": "message-tool",
            "session_id": "host-session-42",
        },
    ]

    provider.sync_turn(user_text, assistant_text, messages=messages)

    assert len(provider._beam.items) == 3
    for stored, message_id in zip(
        provider._beam.items,
        ("message-user", "message-assistant", "message-tool"),
        strict=True,
    ):
        reference = stored["metadata"]["source_ref"]
        assert reference == {
            "session_id": "host-session-42",
            "message_id": message_id,
            "origin": "host",
        }

    provider._beam.items.clear()
    duplicate_id_messages = [
        messages[0],
        messages[1],
        {
            "role": "tool",
            "content": tool_text,
            "id": "duplicate-id",
            "session_id": "host-session-42",
        },
        {
            "role": "assistant",
            "content": "A different message with a reused ID.",
            "id": "duplicate-id",
        },
    ]
    provider.sync_turn(user_text, assistant_text, messages=duplicate_id_messages)
    stored_tool = next(
        item for item in provider._beam.items if item["source"] == "conversation_tool"
    )
    assert stored_tool["content"] == f"[TOOL] {tool_text}"
    assert "source_ref" not in stored_tool["metadata"]
