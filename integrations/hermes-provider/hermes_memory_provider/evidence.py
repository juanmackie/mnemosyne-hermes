"""Local read-only evidence views over the pinned engine's existing rows."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .audit import AuditError, AuditLog

# LOCAL PATCH: P28 source references and Markdown are projections, never a
# second store. This module uses no engine constructors or persistent pragmas.
MAX_ROWS = 200
MAX_CONTENT_CHARS = 4000
MAX_INDEX_CHARS = 8000
MAX_METADATA_CHARS = 65536
MAX_LINEAGE_CHARS = 65536
_REFERENCE_KEYS = ("session_id", "message_id")


class EvidenceError(RuntimeError):
    """The requested engine evidence cannot be read safely."""


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def source_reference(metadata: Any) -> dict[str, str | bool] | None:
    """Return a well-formed reference, without claiming semantic verification."""
    if not isinstance(metadata, dict):
        return None
    reference = metadata.get("source_ref")
    if not isinstance(reference, dict):
        return None
    identifiers = {key: _identifier(reference.get(key)) for key in _REFERENCE_KEYS}
    if not all(identifiers.values()):
        return None
    origin = "host" if reference.get("origin") == "host" else "caller"
    # Stored JSON cannot authenticate a host label from an import or legacy row.
    result = {**identifiers, "origin": origin, "origin_authenticated": False}
    # LOCAL PATCH: P32 old references have an unknown namespace. Preserve them
    # as legacy claims; never silently label them as platform or internal IDs.
    namespace = reference.get("message_id_namespace")
    if namespace in ("platform", "internal"):
        result["message_id_namespace"] = namespace
    return result


def asserted_metadata(metadata: Any) -> Any:
    """Keep caller metadata compatible, but do not let a tool assert host origin."""
    if not isinstance(metadata, dict):
        return metadata
    result = dict(metadata)
    if isinstance(result.get("source_ref"), dict):
        result["source_ref"] = {**result["source_ref"], "origin": "caller"}
    return result


def turn_metadata(content: str, role: str, messages: Any, session_id: str,
                  turn_author: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach only an unambiguous exact host message ID; never infer turn IDs."""
    metadata: dict[str, Any] = {}
    if turn_author:
        metadata["turn_author"] = dict(turn_author)
    if not _identifier(session_id) or not isinstance(messages, list):
        return metadata

    # LOCAL PATCH: P32 scope both exact-content matching and duplicate checks
    # to the active session. Hermes normally omits per-message session IDs;
    # when present, they must agree with the session supplied to sync_turn.
    def belongs_to_session(message: dict[str, Any]) -> bool:
        message_session = message.get("session_id", session_id)
        if message_session is None:
            message_session = session_id
        return message_session == session_id

    matches = [message for message in messages if isinstance(message, dict)
               and belongs_to_session(message)
               and str(message.get("role", "")).lower() == role
               and message.get("content") == content]
    if len(matches) != 1:
        return metadata
    message = matches[0]
    # LOCAL PATCH: P32 prefer explicit platform IDs, checking duplicates only
    # in that namespace. Internal row IDs can legitimately differ or collide.
    key = "message_id" if message.get("message_id") is not None else "id"
    def host_id(value: Any) -> str | None:
        # Hermes row IDs and some platform message IDs are nonnegative integers.
        if type(value) is int and value >= 0:
            value = str(value)
        return _identifier(value)

    message_id = host_id(message.get(key))
    if not message_id:
        return metadata
    if any(other is not message and isinstance(other, dict)
           and belongs_to_session(other)
           and message_id == host_id(other.get(key))
           for other in messages):
        return metadata
    metadata["source_ref"] = {
        "session_id": session_id, "message_id": message_id, "origin": "host",
        "message_id_namespace": "platform" if key == "message_id" else "internal",
    }
    return metadata


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # Table names here are fixed constants, not external input.
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _open_snapshot(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if str(db_path) == ":memory:" or not path.is_file():
        raise EvidenceError("An existing engine database is required")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        try:
            AuditLog._check_engine_schema(connection)
        except AuditError as exc:
            raise EvidenceError("Database is not a supported engine store") from exc
        return connection
    except Exception:
        connection.close()
        raise


def _read_rows(connection: sqlite3.Connection, *, session_id: str, all_sessions: bool,
               memory_id: str | None, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table, store in (("working_memory", "working"), ("episodic_memory", "episodic")):
        columns = _columns(connection, table)
        fields = ["id", "source", "session_id", "scope",
                  f"substr(content, 1, {MAX_CONTENT_CHARS + 1}) AS content",
                  f"substr(metadata_json, 1, {MAX_METADATA_CHARS + 1}) AS metadata_json"]
        fields.extend(name for name in ("timestamp", "created_at", "importance")
                      if name in columns)
        if "summary_of" in columns:
            fields.append(f"substr(summary_of, 1, {MAX_LINEAGE_CHARS + 1}) AS summary_of")
        where = ["scope IN ('session', 'global')"]
        params: list[Any] = []
        if not all_sessions:
            where.append("((session_id = ? AND scope = 'session') OR scope = 'global')")
            params.append(session_id)
        if memory_id is not None:
            where.append("id = ?")
            params.append(memory_id)
        condition = " WHERE " + " AND ".join(where) if where else ""
        order = "timestamp DESC, id" if "timestamp" in columns else "id"
        for row in connection.execute(
            f"SELECT {', '.join(fields)} FROM {table}{condition} ORDER BY {order} LIMIT ?",
            (*params, limit),
        ):
            record = dict(row)
            if len(record.get("content") or "") > MAX_CONTENT_CHARS:
                record["content"] = record["content"][:MAX_CONTENT_CHARS]
                record["content_truncated"] = True
            raw_metadata = record.pop("metadata_json")
            try:
                if len(raw_metadata or "") > MAX_METADATA_CHARS:
                    record["metadata_truncated"] = True
                    raise ValueError("metadata exceeds projection bound")
                metadata = json.loads(raw_metadata or "{}")
                if not isinstance(metadata, dict):
                    raise ValueError("not an object")
                record["metadata"] = metadata
            except (TypeError, ValueError):
                record["metadata"] = {}
                record["metadata_error"] = "invalid_json_object"
            record["memory_store"] = store
            # Lineage is an identifier list, not authority to open originals.
            raw_lineage = str(record.pop("summary_of", "") or "")
            lineage_truncated = len(raw_lineage) > MAX_LINEAGE_CHARS
            if lineage_truncated:
                # Do not expose a partial identifier cut at the character bound.
                raw_lineage = raw_lineage[:MAX_LINEAGE_CHARS].rsplit(",", 1)[0] if "," in raw_lineage[:MAX_LINEAGE_CHARS] else ""
            all_identifiers = list(dict.fromkeys(raw_lineage.split(",")))
            identifiers = all_identifiers[:MAX_ROWS]
            if lineage_truncated or len(all_identifiers) > MAX_ROWS:
                record["lineage_truncated"] = True
            visible_lineage = []
            for identifier in identifiers:
                if not _identifier(identifier):
                    continue
                clause = "id = ? AND scope IN ('session', 'global')"
                binds = [identifier]
                if not all_sessions:
                    clause += " AND ((session_id = ? AND scope = 'session') OR scope = 'global')"
                    binds.append(session_id)
                if connection.execute(
                    f"SELECT 1 FROM working_memory WHERE {clause}", binds,
                ).fetchone():
                    visible_lineage.append(identifier)
            record["summary_of"] = visible_lineage
            if record.get("lineage_truncated") or (raw_lineage and len(visible_lineage) != len([x for x in identifiers if x])):
                record["lineage_unavailable"] = True
            rows.append(record)
        if memory_id is not None and rows:
            # Match the engine get(): working takes precedence over episodic.
            return rows[:limit]
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), str(row["id"])), reverse=True)
    return rows[:limit]


def read_snapshot(db_path: str | Path, *, session_id: str = "hermes_default",
                  all_sessions: bool = False, memory_id: str | None = None,
                  limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """Read own/global rows (or explicit local-operator all-session selection)."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS + 1:
        raise EvidenceError(f"limit must be between 1 and {MAX_ROWS + 1}")
    if not all_sessions and not _identifier(session_id):
        raise EvidenceError("A session identifier is required")
    if memory_id is not None and not _identifier(memory_id):
        raise EvidenceError("A valid memory identifier is required")
    try:
        connection = _open_snapshot(db_path)
        try:
            return _read_rows(connection, session_id=session_id, all_sessions=all_sessions,
                              memory_id=memory_id, limit=limit)
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise EvidenceError(f"Engine evidence unavailable ({type(exc).__name__})") from exc


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _markdown(value: Any, limit: int) -> str:
    text = _text(value, limit)
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, "\\" + character)
    if len(text) > limit:
        text = text[:limit - 1]
        # A cut between an escape and its character must not leave a bare slash.
        if (len(text) - len(text.rstrip("\\"))) % 2 == 1:
            text = text[:-1]
        return text + "…"
    return text


def export_markdown(db_path: str | Path, output_path: str | Path, *,
                    session_id: str = "hermes_default", all_sessions: bool = False,
                    limit: int = MAX_ROWS) -> dict[str, Any]:
    """Publish a bounded single-file evidence projection without altering its DB."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise EvidenceError(f"limit must be between 1 and {MAX_ROWS}")
    database = Path(db_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    protected = {database, *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    if destination in protected or destination.exists():
        raise EvidenceError("Markdown output must be a new file separate from the database")
    if not destination.parent.is_dir():
        raise EvidenceError("Markdown output directory must already exist")
    rows = read_snapshot(db_path, session_id=session_id, all_sessions=all_sessions, limit=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    selection = "all sessions (local operator)" if all_sessions else f"session {session_id} and global"
    header = ["# Mnemosyne evidence projection", "", "Read-only export; references are not verified citations.",
              f"Selection: {_markdown(selection, 512)}.",
              f"Shown {len(rows)} rows; {'additional rows omitted' if has_more else 'selection complete'}.", ""]
    index = ["## Index", ""]
    index_chars = sum(len(line) + 1 for line in index)
    for number, row in enumerate(rows, 1):
        line = f"- [{number}](#memory-{number}): {_markdown(row['id'], 256)}"
        if index_chars + len(line) + 1 > MAX_INDEX_CHARS - 64:
            index.append("- … additional index entries omitted")
            break
        index.append(line)
        index_chars += len(line) + 1
    sections = []
    for number, row in enumerate(rows, 1):
        sections.extend(["", f'<a id="memory-{number}"></a>', f"## Memory {number}", "",
                         f"ID: {_markdown(row['id'], 256)} · {_markdown(row['memory_store'], 16)} · scope {_markdown(row['scope'], 32)}",
                         "", _markdown(row["content"], MAX_CONTENT_CHARS), ""])
        if row.get("content_truncated"):
            sections.append("… additional content omitted")
        reference = source_reference(row.get("metadata"))
        if reference:
            sections.append(f"Source reference (stored {reference['origin']} claim, lookup unavailable): "
                            f"{_markdown(reference['session_id'], 256)} / {_markdown(reference['message_id'], 256)} "
                            f"(namespace: {reference.get('message_id_namespace', 'unknown')})")
        else:
            sections.append("Source reference: unavailable.")
        if row.get("metadata_error"):
            sections.append("Metadata: oversized or invalid JSON object; evidence unavailable.")
        if row["summary_of"]:
            sections.append("Eligible original IDs: " + ", ".join(
                _markdown(identifier, 256) for identifier in row["summary_of"][:20]
            ))
            if len(row["summary_of"]) > 20:
                sections.append("… additional eligible original IDs omitted")
        if row.get("lineage_unavailable"):
            sections.append("Some original evidence is unavailable in this selection.")
    document = "\n".join(header + index + sections) + "\n"
    # Write completely before publishing. Hard-link creation refuses a racing
    # existing output atomically, unlike replace(), which could overwrite it.
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=destination.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    except OSError as exc:
        raise EvidenceError(f"Markdown export failed ({type(exc).__name__})") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return {"output_path": str(destination), "shown": len(rows), "truncated": has_more,
            "selection": "all_sessions" if all_sessions else "session_and_global"}
