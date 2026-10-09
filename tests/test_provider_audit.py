"""Regression coverage for the provider's local audit store."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

# Always exercise the source under test, even when the Hermes venv has an
# installed provider with the same package name.
_PROVIDER_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "hermes-provider"
if str(_PROVIDER_ROOT) in sys.path:
    sys.path.remove(str(_PROVIDER_ROOT))
sys.path.insert(0, str(_PROVIDER_ROOT))

from hermes_memory_provider.audit import AuditError, AuditLog  # noqa: E402


def _engine_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE working_memory (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, session_id TEXT NOT NULL,
            scope TEXT, source TEXT, metadata_json TEXT
        );
        CREATE TABLE episodic_memory (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, session_id TEXT NOT NULL,
            scope TEXT, source TEXT, metadata_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_worker_thread_events_persist_and_are_queryable(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            audit.record(
                "remember", memory_id=f"m-{index}", scope="session", session_id="s-1", profile="p-1"
            )
        except BaseException as exc:  # pragma: no cover - record is contractually non-raising
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(i,)) for i in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    assert audit.count(session_id="s-1") == 12
    assert len(audit.query(session_id="s-1", limit=200)) == 12
    assert audit.diagnostics()["counters"]["write_successes"] == 12
    audit.close()


def test_queries_apply_memory_session_profile_and_operator_filters(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.record(
        "remember",
        memory_id="deleted-id",
        scope="session",
        session_id="session-a",
        profile="profile-a",
    )
    audit.record(
        "forget", memory_id="other-id", scope="session", session_id="session-b", profile="profile-b"
    )
    audit.record(
        "remember",
        memory_id="global-id",
        scope="global",
        session_id="session-b",
        profile="profile-b",
    )
    audit.record(
        "remember",
        memory_id="unknown-scope",
        scope="future-scope",
        session_id="session-a",
        profile="profile-a",
    )
    audit.close()

    reader = AuditLog(path, readonly=True)
    assert [
        row["memory_id"]
        for row in reader.query(memory_id="deleted-id", session_id="session-a", profile="profile-a")
    ] == ["deleted-id"]
    assert {row["memory_id"] for row in reader.query(session_id="session-a")} == {
        "deleted-id",
        "global-id",
    }
    assert [
        row["memory_id"] for row in reader.query(session_id="session-a", profile="profile-b")
    ] == ["global-id"]
    with pytest.raises(ValueError, match="all_sessions"):
        reader.query()
    assert reader.count(all_sessions=True) == 3
    reader.close()


def test_legacy_unknown_ownership_is_excluded_from_scoped_query(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.record("remember", memory_id="known", scope="session", session_id="s1")
    audit.record("remember", memory_id="unknown", scope=None, session_id=None)
    audit.close()

    reader = AuditLog(path, readonly=True)
    assert [row["memory_id"] for row in reader.query(session_id="s1")] == ["known"]
    assert reader.count(all_sessions=True) == 1
    reader.close()


def test_metadata_is_allowlisted_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.record(
        "remember",
        session_id="s1",
        scope="session",
        reason="private memory text",
        metadata={
            "kind": "fact",
            "existing": False,
            "content": "private memory text",
            "source_payload": "private transcript",
            "source_id": "x" * 500,
        },
    )
    audit.close()
    reader = AuditLog(path, readonly=True)
    row = reader.query(session_id="s1")[0]
    metadata = json.loads(row["metadata_json"])
    assert metadata == {"kind": "fact", "existing": False, "source_id": "x" * 128}
    assert row["reason"] is None
    assert "private" not in row["metadata_json"]
    reader.close()


def test_metadata_bound_never_truncates_json(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.record(
        "remember",
        session_id="s1",
        scope="session",
        metadata={key: "\x00" * 128 for key in ("kind", "source_type", "source_id", "author_id")},
    )
    audit.close()
    reader = AuditLog(path, readonly=True)
    encoded = reader.query(session_id="s1")[0]["metadata_json"]
    assert encoded is not None
    assert len(encoded) <= 1024
    assert isinstance(json.loads(encoded), dict)
    reader.close()


def test_legacy_metadata_is_sanitized_when_read(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO audit_log (timestamp, action, scope, session_id, reason, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            "remember",
            "session",
            "s1",
            "private memory text",
            '{"content":"private memory text","kind":"fact"}',
        ),
    )
    conn.commit()
    conn.close()

    reader = AuditLog(path, readonly=True)
    row = reader.query(session_id="s1")[0]
    assert row["reason"] is None
    assert json.loads(row["metadata_json"]) == {"kind": "fact"}
    reader.close()


def test_oversized_legacy_audit_fields_are_bounded_before_python_read(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO audit_log "
        "(timestamp, action, scope, session_id, source_tool, reason, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            "a" * 4000,
            "session",
            "s1",
            "tool" * 1000,
            "r" * 65,
            '{"content":"' + ("private" * 2000) + '"}',
        ),
    )
    conn.commit()
    conn.close()
    before = _digest(path)

    reader = AuditLog(path, readonly=True)
    row = reader.query(session_id="s1")[0]
    assert len(row["action"]) == 256
    assert len(row["source_tool"]) == 256
    assert row["reason"] is None
    assert row["metadata_json"] is None
    reader.close()
    assert _digest(path) == before


@pytest.mark.parametrize("limit", [0, -1, 201, True, 1.5])
def test_query_rejects_invalid_limits(tmp_path: Path, limit: object) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    reader = AuditLog(path, readonly=True)
    with pytest.raises(ValueError, match="limit"):
        reader.query(limit=limit, session_id="s1")  # type: ignore[arg-type]
    reader.close()


def test_failed_initialization_is_visible_and_does_not_create_missing_file(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.db"
    audit = AuditLog(missing)
    assert not missing.exists()
    with pytest.raises(AuditError):
        audit.query(session_id="s1")
    assert audit.diagnostics()["errors"]["init"] == "FileNotFoundError"
    assert audit.diagnostics()["counters"]["read_failures"] == 1
    audit.close()


def test_foreign_database_refusal_is_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "foreign.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (value TEXT)")
    conn.execute("INSERT INTO unrelated VALUES ('keep byte-identical')")
    conn.commit()
    conn.close()
    before = _digest(path)

    audit = AuditLog(path)
    assert audit.diagnostics()["state"] == "unavailable"
    assert _digest(path) == before
    audit.record("remember", session_id="s1")
    assert _digest(path) == before
    audit.close()


@pytest.mark.parametrize("kind", ["view", "lite"])
def test_engine_lookalike_refusal_is_byte_identical(tmp_path: Path, kind: str) -> None:
    path = tmp_path / f"{kind}.db"
    conn = sqlite3.connect(path)
    if kind == "view":
        conn.executescript(
            """
            CREATE TABLE backing (id TEXT, content TEXT, session_id TEXT,
                scope TEXT, source TEXT, metadata_json TEXT);
            CREATE VIEW working_memory AS SELECT * FROM backing;
            CREATE VIEW episodic_memory AS SELECT * FROM backing;
            """
        )
    else:
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, namespace TEXT NOT NULL,
                importance INTEGER, context TEXT, summary TEXT, keywords TEXT,
                created_at REAL, access_count INTEGER, last_accessed REAL
            );
            CREATE TABLE working_memory (
                id TEXT, content TEXT, session_id TEXT, scope TEXT, source TEXT, metadata_json TEXT
            );
            CREATE TABLE episodic_memory (
                id TEXT, content TEXT, session_id TEXT, scope TEXT, source TEXT, metadata_json TEXT
            );
            INSERT INTO memories VALUES ('m', 'do not adopt', 'default', 1, NULL, NULL,
                NULL, 0, 0, NULL);
            """
        )
    conn.commit()
    conn.close()
    before = _digest(path)

    audit = AuditLog(path)
    assert audit.diagnostics()["state"] == "unavailable"
    assert _digest(path) == before
    audit.close()


def test_write_reclassifies_path_inside_transaction_after_replacement(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)

    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE unrelated (value TEXT)")
    conn.execute("INSERT INTO unrelated VALUES ('preserve')")
    conn.commit()
    conn.close()
    path.unlink()
    shutil.copyfile(foreign, path)
    before = _digest(path)

    audit.record("remember", session_id="s1", scope="session")
    assert _digest(path) == before
    assert audit.diagnostics()["counters"]["write_failures"] == 1
    assert audit.diagnostics()["errors"]["write"] == "AuditError"
    audit.close()


def test_readonly_reader_does_not_create_audit_table(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    before = _digest(path)
    reader = AuditLog(path, readonly=True)
    assert reader.diagnostics()["state"] == "unavailable"
    assert _digest(path) == before
    with pytest.raises(AuditError):
        reader.query(session_id="s1")
    assert _digest(path) == before
    reader.close()


def test_injected_write_failure_is_counted_without_exposing_error_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)

    def fail_connect() -> sqlite3.Connection:
        raise sqlite3.OperationalError("private db path and payload")

    monkeypatch.setattr(audit, "_connect", fail_connect)
    audit.record("remember", session_id="s1", scope="session")
    diagnostics = audit.diagnostics()
    assert diagnostics["counters"]["write_failures"] == 1
    assert diagnostics["errors"]["write"] == "OperationalError"
    assert diagnostics["state"] == "degraded"
    assert "private" not in str(diagnostics)
    audit.close()


def test_read_failure_raises_and_updates_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    audit.record("remember", session_id="s1", scope="session")

    def fail_connect() -> sqlite3.Connection:
        raise sqlite3.OperationalError("sensitive detail")

    monkeypatch.setattr(audit, "_connect", fail_connect)
    with pytest.raises(AuditError, match="OperationalError"):
        audit.query(session_id="s1")
    with pytest.raises(AuditError, match="OperationalError"):
        audit.count(session_id="s1")
    diag = audit.diagnostics()
    assert diag["counters"]["read_failures"] == 2
    assert diag["errors"]["read"] == "OperationalError"
    assert "sensitive" not in str(diag)
    audit.close()


def test_sqlite_lock_wait_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    blocker = sqlite3.connect(path, timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    audit.record("remember", session_id="s1", scope="session")
    elapsed = time.monotonic() - started
    blocker.rollback()
    blocker.close()
    assert elapsed < 1.0
    assert audit.diagnostics()["counters"]["write_failures"] == 1
    audit.close()


def test_close_drains_active_write_and_refuses_later_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "engine.db"
    _engine_db(path)
    audit = AuditLog(path)
    original_connect = audit._connect
    entered = threading.Event()
    release = threading.Event()

    def delayed_connect() -> sqlite3.Connection:
        entered.set()
        assert release.wait(2)
        return original_connect()

    monkeypatch.setattr(audit, "_connect", delayed_connect)
    writer = threading.Thread(
        target=lambda: audit.record("remember", session_id="s1", scope="session")
    )
    writer.start()
    assert entered.wait(1)
    closer = threading.Thread(target=audit.close)
    closer.start()
    release.set()
    writer.join()
    closer.join()

    audit.record("remember", session_id="s1", scope="session")
    assert audit.diagnostics()["state"] == "closed"
    assert audit.diagnostics()["counters"]["write_successes"] == 1
    assert audit.diagnostics()["counters"]["write_failures"] == 1
