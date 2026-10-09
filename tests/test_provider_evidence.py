"""Source references and read-only projections over engine evidence."""

import hashlib
import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

from hermes_memory_provider.evidence import (  # noqa: E402
    MAX_CONTENT_CHARS,
    MAX_INDEX_CHARS,
    MAX_METADATA_CHARS,
    EvidenceError,
    asserted_metadata,
    export_markdown,
    read_snapshot,
    source_reference,
    turn_metadata,
)


def _database(path):
    connection = sqlite3.connect(path)
    for table in ("working_memory", "episodic_memory"):
        connection.execute(
            f"CREATE TABLE {table} (id TEXT PRIMARY KEY, content TEXT, source TEXT, "
            "session_id TEXT, scope TEXT, metadata_json TEXT, timestamp TEXT, "
            "summary_of TEXT, importance REAL, created_at TEXT)"
        )
    connection.commit()
    return connection


def _remember(
    connection,
    memory_id,
    *,
    session="own",
    scope="session",
    table="working_memory",
    content="durable synthetic fact",
    metadata=None,
    lineage="",
):
    connection.execute(
        f"INSERT INTO {table} VALUES (?, ?, 'synthetic', ?, ?, ?, '2026-10-09', ?, 0.5, '2026-10-09')",
        (memory_id, content, session, scope, json.dumps(metadata or {}), lineage),
    )
    connection.commit()


def test_host_reference_requires_unique_exact_message_and_stable_identifiers():
    message = {"role": "user", "content": "remember this preference", "id": "message-7"}
    metadata = turn_metadata(
        message["content"], "user", [message], "host-session", {"id": "speaker"}
    )
    assert source_reference(metadata) == {
        "session_id": "host-session",
        "message_id": "message-7",
        "origin": "host",
        "origin_authenticated": False,
    }
    assert metadata["turn_author"] == {"id": "speaker"}
    for messages in (
        None,
        [{**message, "id": None}],
        [message, message],
        [{**message, "content": "other content"}],
        [{**message, "session_id": "other"}],
        [{**message, "message_id": "conflicting-id"}],
        [{**message, "id": "bad\nidentifier"}],
        [message, {"role": "assistant", "content": "different", "id": "message-7"}],
        [message, {"role": "user", "content": "different", "message_id": "message-7"}],
    ):
        assert "source_ref" not in turn_metadata(
            message["content"], "user", messages, "host-session"
        )
    assert "source_ref" not in turn_metadata(message["content"], "user", [message], "")


def test_caller_reference_cannot_claim_host_origin_and_metadata_is_not_mutated():
    metadata = {
        "tags": ["test"],
        "source_ref": {"session_id": "s", "message_id": "m", "origin": "host"},
    }
    asserted = asserted_metadata(metadata)
    assert source_reference(asserted)["origin"] == "caller"
    assert metadata["source_ref"]["origin"] == "host"
    assert source_reference(metadata)["origin_authenticated"] is False
    assert asserted_metadata(None) is None
    assert source_reference({"source_ref": {"session_id": "s"}}) is None


def test_snapshot_filters_foreign_rows_and_lineage_without_mutation(tmp_path):
    database = tmp_path / "engine.db"
    with _database(database) as connection:
        _remember(connection, "own")
        _remember(connection, "global", session="other", scope="global")
        _remember(connection, "foreign-secret-id", session="other")
        _remember(
            connection, "summary", table="episodic_memory", lineage="own,foreign-secret-id,missing"
        )
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    rows = read_snapshot(database, session_id="own")
    assert {row["id"] for row in rows} == {"own", "global", "summary"}
    summary = next(row for row in rows if row["id"] == "summary")
    assert summary["summary_of"] == ["own"]
    assert summary["lineage_unavailable"] is True
    assert "foreign-secret-id" not in json.dumps(rows)
    assert len(read_snapshot(database, all_sessions=True)) == 4
    assert read_snapshot(database, session_id="own", memory_id="foreign-secret-id") == []
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_snapshot_refuses_missing_foreign_and_corrupt_files_unchanged(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(EvidenceError):
        read_snapshot(missing)
    assert not missing.exists()
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE memories (content TEXT)")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not SQLite")
    for database in (foreign, corrupt):
        before = database.read_bytes()
        with pytest.raises(EvidenceError):
            read_snapshot(database)
        assert database.read_bytes() == before


def test_snapshot_includes_committed_wal_without_opening_writer(tmp_path):
    database = tmp_path / "wal.db"
    connection = _database(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        _remember(connection, "committed")
        connection.execute("UPDATE working_memory SET content='uncommitted' WHERE id='committed'")
        assert read_snapshot(database, session_id="own")[0]["content"] == "durable synthetic fact"
    finally:
        connection.rollback()
        connection.close()


def test_snapshot_accepts_engine_legacy_memories_table(tmp_path):
    database = tmp_path / "legacy-engine.db"
    with _database(database) as connection:
        connection.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, source TEXT, "
            "timestamp TEXT, session_id TEXT, importance REAL, metadata_json TEXT, created_at TEXT)"
        )
        _remember(connection, "visible")
    assert read_snapshot(database, session_id="own")[0]["id"] == "visible"


def test_snapshot_prefers_working_id_and_marks_invalid_metadata(tmp_path):
    database = tmp_path / "engine.db"
    with _database(database) as connection:
        _remember(connection, "same")
        _remember(connection, "same", table="episodic_memory", content="episodic")
        connection.execute("UPDATE working_memory SET metadata_json='bad json'")
    row = read_snapshot(database, session_id="own", memory_id="same", limit=1)[0]
    assert row["memory_store"] == "working"
    assert row["metadata_error"] == "invalid_json_object"


def test_markdown_exports_scoped_ids_and_asserted_refs_with_explicit_gaps(tmp_path):
    database = tmp_path / "engine.db"
    with _database(database) as connection:
        _remember(
            connection,
            "own",
            content="<script>alert('synthetic')</script> [[unsafe]]",
            metadata={
                "source_ref": {"session_id": "host", "message_id": "exact", "origin": "host"}
            },
        )
        _remember(connection, "global", session="other", scope="global")
        _remember(connection, "foreign-secret-id", session="other", content="foreign secret")
    before = database.read_bytes()
    output = tmp_path / "evidence.md"
    result = export_markdown(database, output, session_id="own")
    text = output.read_text(encoding="utf-8")
    assert result["shown"] == 2
    assert result["truncated"] is False
    assert "Source reference (stored host claim, lookup unavailable): host / exact" in text
    assert "Source reference: unavailable" in text
    assert "<script>" not in text and "[[unsafe]]" not in text
    assert "foreign secret" not in text and "foreign-secret-id" not in text
    assert database.read_bytes() == before
    with pytest.raises(EvidenceError):
        export_markdown(database, output, session_id="own")
    assert output.read_text(encoding="utf-8") == text


def test_markdown_bounds_index_and_content_and_refuses_database_destinations(tmp_path):
    database = tmp_path / "engine.db"
    with _database(database) as connection:
        for number in range(201):
            _remember(connection, f"{number:03d}-" + "a" * 220, content="large" * 1000)
    output = tmp_path / "bounded.md"
    result = export_markdown(database, output, session_id="own")
    text = output.read_text(encoding="utf-8")
    index = text.split("## Index\n", 1)[1].split('<a id="memory-1">', 1)[0]
    assert len(index) <= MAX_INDEX_CHARS
    assert "additional index entries omitted" in index
    assert result["shown"] == 200 and result["truncated"] is True
    assert "large" * 1000 not in text
    assert "…" in text
    before = database.read_bytes()
    for destination in (
        database,
        pathlib.Path(str(database) + "-wal"),
        pathlib.Path(str(database) + "-shm"),
        pathlib.Path(str(database) + "-journal"),
    ):
        with pytest.raises(EvidenceError):
            export_markdown(database, destination, session_id="own")
    assert database.read_bytes() == before


def test_snapshot_excludes_unknown_scope_from_rows_and_lineage(tmp_path):
    database = tmp_path / "engine.db"
    with _database(database) as connection:
        _remember(connection, "malformed", scope="unknown")
        _remember(connection, "summary", lineage="malformed")
    for selection in ({"session_id": "own"}, {"all_sessions": True}):
        rows = read_snapshot(database, **selection)
        assert [row["id"] for row in rows] == ["summary"]
        assert rows[0]["summary_of"] == []
        assert rows[0]["lineage_unavailable"] is True


def test_snapshot_bounds_large_legacy_fields_and_marks_lineage_caps(tmp_path):
    database = tmp_path / "engine.db"
    with _database(database) as connection:
        for number in range(201):
            _remember(connection, f"original-{number}")
        _remember(
            connection,
            "summary",
            content="x" * 100000,
            metadata={"large": "x" * (MAX_METADATA_CHARS * 2)},
            lineage=",".join(f"original-{number}" for number in range(201)),
        )
        _remember(connection, "huge-lineage", lineage="original-0," + "x" * 100000)
    row = read_snapshot(database, session_id="own", memory_id="summary")[0]
    assert len(row["content"]) == MAX_CONTENT_CHARS
    assert row["content_truncated"] is True
    assert row["metadata"] == {} and row["metadata_truncated"] is True
    assert len(row["summary_of"]) == 200
    assert row["lineage_truncated"] is True and row["lineage_unavailable"] is True
    huge = read_snapshot(database, session_id="own", memory_id="huge-lineage")[0]
    assert huge["summary_of"] == ["original-0"]
    assert huge["lineage_truncated"] is True and huge["lineage_unavailable"] is True


@pytest.mark.parametrize("limit", [0, -1, 202, True, "1"])
def test_snapshot_rejects_unbounded_or_invalid_limits(tmp_path, limit):
    with pytest.raises(EvidenceError):
        read_snapshot(tmp_path / "missing", limit=limit)
