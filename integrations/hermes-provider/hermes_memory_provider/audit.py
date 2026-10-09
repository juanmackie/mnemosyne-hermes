"""Best-effort audit events for the Hermes Mnemosyne provider.

# LOCAL PATCH: P26 — use short-lived per-operation connections, classified
# engine-store checks, bounded waits and explicit failures for history reads.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_SECONDS = 0.1
_BUSY_TIMEOUT_MS = 100
_MAX_LIMIT = 200
_MAX_METADATA_STRING = 128
_MAX_EVENT_TEXT = 256
_MAX_REASON_TEXT = 65
_MAX_METADATA_JSON = 1024
_METADATA_KEYS = ("kind", "existing", "source_type", "source_id", "author_id", "memory_store")
_EVENT_COLUMNS = (
    "event_id", "timestamp", "action", "memory_id", "bank", "scope",
    "profile", "session_id", "source_tool", "tokens_used", "reason",
    "metadata_json",
)
_LITE_MEMORIES_COLUMNS = {
    "id", "content", "namespace", "importance", "context", "summary",
    "keywords", "created_at", "access_count", "last_accessed",
}
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    memory_id TEXT,
    bank TEXT,
    scope TEXT,
    profile TEXT,
    session_id TEXT,
    source_tool TEXT,
    tokens_used INTEGER,
    reason TEXT,
    metadata_json TEXT
)
"""
_INSERT = """
INSERT INTO audit_log
    (timestamp, action, memory_id, bank, scope, profile, session_id,
     source_tool, tokens_used, reason, metadata_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class AuditError(RuntimeError):
    """The audit store could not provide a trustworthy read result."""


# LOCAL PATCH: P26 each operation owns and closes its connection on its calling
# thread; the lifecycle lock coordinates operations with provider shutdown.
class AuditLog:
    """Append-only audit log with per-operation SQLite connections.

    Writes are best-effort so audit trouble never breaks memory mutations.
    Reads raise :class:`AuditError` because an unavailable history must not
    look like a valid empty result. ``readonly=True`` supports CLI inspection
    without creating or migrating an audit table.
    """

    def __init__(self, db_path: Path, *, readonly: bool = False):
        self._db_path = Path(db_path)
        self._readonly = readonly
        self._lock = threading.RLock()
        self._closed = False
        self._initialized = False
        self._state = "initializing"
        self._errors: Dict[str, Optional[str]] = {"init": None, "write": None, "read": None}
        self._counters = {"write_attempts": 0, "write_successes": 0, "write_failures": 0,
                          "read_attempts": 0, "read_failures": 0}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        # mode=rw prevents sqlite3 from creating a missing or mistyped file.
        mode = "ro" if self._readonly else "rw"
        uri = self._db_path.resolve().as_uri() + f"?mode={mode}"
        conn = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _object_type(conn: sqlite3.Connection, name: str) -> Optional[str]:
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            (name,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _check_engine_schema(conn: sqlite3.Connection) -> None:
        # This is a read-only classification step. No DDL or persistent pragma
        # is issued until both engine tables and their identity/scope columns exist.
        memories_columns = AuditLog._table_columns(conn, "memories")
        if memories_columns in (_LITE_MEMORIES_COLUMNS,
                                _LITE_MEMORIES_COLUMNS | {"content_lower"}):
            raise AuditError("database is a mnemosyne-lite store")
        for table in ("working_memory", "episodic_memory"):
            columns = AuditLog._table_columns(conn, table)
            required = {"id", "content", "session_id", "scope", "source", "metadata_json"}
            if AuditLog._object_type(conn, table) != "table" or not required.issubset(columns):
                raise AuditError("database is not a classified Mnemosyne engine store")

    def _initialize(self) -> None:
        with self._lock:
            if not self._db_path.is_file():
                self._state = "unavailable"
                self._errors["init"] = "FileNotFoundError"
                return
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = self._connect()
                conn.execute("BEGIN" if self._readonly else "BEGIN IMMEDIATE")
                self._check_engine_schema(conn)
                columns = self._table_columns(conn, "audit_log")
                required = set(_EVENT_COLUMNS) - {"tokens_used"}
                if columns and not required.issubset(columns):
                    raise AuditError("audit history schema is incomplete")
                if self._readonly:
                    if not columns:
                        raise AuditError("audit history is unavailable")
                    if not required.issubset(columns):
                        raise AuditError("audit history schema is incomplete")
                else:
                    conn.execute(_CREATE_TABLE)
                    columns = self._table_columns(conn, "audit_log")
                    if "tokens_used" not in columns:
                        conn.execute("ALTER TABLE audit_log ADD COLUMN tokens_used INTEGER")
                    conn.commit()
                self._initialized = True
                self._state = "ready"
            except Exception as exc:
                self._state = "unavailable"
                self._errors["init"] = type(exc).__name__
                logger.debug("audit store initialization failed (%s)", type(exc).__name__)
            finally:
                if conn is not None:
                    conn.close()

    @staticmethod
    def _bounded_text(value: Optional[str], maximum: int = 256) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        return value[:maximum]

    @staticmethod
    def _safe_reason(value: Optional[str]) -> Optional[str]:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
            return value
        return None

    @staticmethod
    def _safe_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(metadata, dict):
            return None
        safe: Dict[str, Any] = {}
        for key in _METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, bool):
                candidate = value
            elif isinstance(value, (str, int, float)):
                candidate = str(value)[:_MAX_METADATA_STRING]
            else:
                continue
            trial = {**safe, key: candidate}
            if len(json.dumps(trial, separators=(",", ":"))) <= 1024:
                safe[key] = candidate
        return json.dumps(safe, separators=(",", ":")) if safe else None

    def record(
        self,
        action: str,
        *,
        memory_id: Optional[str] = None,
        bank: Optional[str] = None,
        scope: Optional[str] = None,
        profile: Optional[str] = None,
        session_id: Optional[str] = None,
        source_tool: Optional[str] = None,
        tokens_used: Optional[int] = None,
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Attempt to append an event; failures are counted and never raised."""
        with self._lock:
            self._counters["write_attempts"] += 1
            if self._closed or not self._initialized or self._readonly:
                self._counters["write_failures"] += 1
                self._errors["write"] = "AuditClosedError" if self._closed else "AuditUnavailableError"
                if not self._closed:
                    self._state = "degraded" if self._initialized else "unavailable"
                return
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                self._check_engine_schema(conn)
                conn.execute(
                    _INSERT,
                    (time.time(), self._bounded_text(action, 64),
                     self._bounded_text(memory_id, 256), self._bounded_text(bank, 64),
                     self._bounded_text(scope, 64), self._bounded_text(profile, 128),
                     self._bounded_text(session_id, 256), self._bounded_text(source_tool, 128),
                     tokens_used if isinstance(tokens_used, int) else None,
                     self._safe_reason(reason), self._safe_metadata(metadata)),
                )
                conn.commit()
                self._counters["write_successes"] += 1
            except Exception as exc:
                self._counters["write_failures"] += 1
                self._errors["write"] = type(exc).__name__
                self._state = "degraded"
                logger.debug("audit event write failed (%s)", type(exc).__name__)
            finally:
                if conn is not None:
                    conn.close()

    def _validate_scope(self, session_id: Optional[str], all_sessions: bool) -> None:
        if not all_sessions and not (isinstance(session_id, str) and session_id):
            raise ValueError("session_id is required unless all_sessions=True")

    def _select(self, *, count: bool, limit: int, memory_id: Optional[str],
                session_id: Optional[str], profile: Optional[str],
                all_sessions: bool) -> Any:
        self._validate_scope(session_id, all_sessions)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("limit must be an integer from 1 to 200")
        with self._lock:
            self._counters["read_attempts"] += 1
            if self._closed or not self._initialized:
                self._read_failed("AuditClosedError" if self._closed else "AuditUnavailableError")
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = self._connect()
                conn.execute("BEGIN")
                self._check_engine_schema(conn)
                columns = self._table_columns(conn, "audit_log")
                if not columns:
                    raise AuditError("audit history is unavailable")
                select_columns = [column for column in _EVENT_COLUMNS if column in columns]
                where = []
                params: list[Any] = []
                # Legacy events without trustworthy ownership metadata are never
                # returned by a scoped query, including events for deleted IDs.
                if not all_sessions:
                    where.append(
                        "session_id IS NOT NULL AND session_id != '' AND "
                        "((session_id = ? AND scope = 'session') OR scope = 'global')"
                    )
                    params.append(session_id)
                else:
                    where.append(
                        "session_id IS NOT NULL AND session_id != '' "
                        "AND scope IN ('session', 'global')"
                    )
                if memory_id is not None:
                    where.append("memory_id = ?")
                    params.append(memory_id)
                if profile is not None:
                    where.append("profile = ? AND profile IS NOT NULL AND profile != ''")
                    params.append(profile)
                where_sql = " WHERE " + " AND ".join(where) if where else ""
                if count:
                    row = conn.execute("SELECT COUNT(*) FROM audit_log" + where_sql, params).fetchone()
                    return int(row[0])
                # Older audit tables may predate tokens_used; keep the public row shape stable.
                projection = []
                for column in select_columns:
                    if column == "metadata_json":
                        projection.append(
                            f"substr(metadata_json, 1, {_MAX_METADATA_JSON + 1}) AS metadata_json"
                        )
                    elif column == "reason":
                        projection.append(
                            f"substr(reason, 1, {_MAX_REASON_TEXT}) AS reason"
                        )
                    elif column in {
                        "action", "memory_id", "bank", "scope", "profile",
                        "session_id", "source_tool",
                    }:
                        projection.append(
                            f"substr({column}, 1, {_MAX_EVENT_TEXT}) AS {column}"
                        )
                    else:
                        projection.append(column)
                selected = ", ".join(projection)
                rows = conn.execute(
                    f"SELECT {selected} FROM audit_log{where_sql} ORDER BY event_id DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
                return [dict(zip(select_columns, row)) for row in rows]
            except ValueError:
                raise
            except AuditError as exc:
                self._read_failed(type(exc).__name__)
            except Exception as exc:
                self._read_failed(type(exc).__name__)
            finally:
                if conn is not None:
                    conn.close()

    def _read_failed(self, error_class: str) -> None:
        self._counters["read_failures"] += 1
        self._errors["read"] = error_class
        self._state = "degraded" if not self._closed else "closed"
        raise AuditError(f"audit history read failed ({error_class})")

    def query(self, limit: int = 50, *, memory_id: Optional[str] = None,
              session_id: Optional[str] = None, profile: Optional[str] = None,
              all_sessions: bool = False) -> list[Dict[str, Any]]:
        """Return recent events within a session or explicit operator scope."""
        rows = self._select(count=False, limit=limit, memory_id=memory_id,
                            session_id=session_id, profile=profile,
                            all_sessions=all_sessions)
        for row in rows:
            row.setdefault("tokens_used", None)
            row["reason"] = self._safe_reason(row.get("reason"))
            metadata_json = row.get("metadata_json")
            if metadata_json:
                if len(metadata_json) > _MAX_METADATA_JSON:
                    row["metadata_json"] = None
                else:
                    try:
                        row["metadata_json"] = self._safe_metadata(json.loads(metadata_json))
                    except (TypeError, ValueError):
                        row["metadata_json"] = None
        return rows

    def count(self, *, memory_id: Optional[str] = None,
              session_id: Optional[str] = None, profile: Optional[str] = None,
              all_sessions: bool = False) -> int:
        """Count events in an explicitly selected session or operator scope."""
        return self._select(count=True, limit=1, memory_id=memory_id,
                             session_id=session_id, profile=profile,
                             all_sessions=all_sessions)

    def diagnostics(self) -> Dict[str, Any]:
        """Return counters and sanitized error classes without event contents."""
        with self._lock:
            return {"state": self._state, "readonly": self._readonly,
                    "counters": dict(self._counters), "errors": dict(self._errors)}

    def close(self) -> None:
        """Drain any in-flight operation and reject subsequent writes."""
        with self._lock:
            self._closed = True
            self._state = "closed"


def read_audit_history(
    db_path: Path,
    memory_id: Optional[str] = None,
    session_id: Optional[str] = None,
    all_sessions: bool = False,
    profile: Optional[str] = None,
    limit: int = 50,
) -> list[Dict[str, Any]]:
    """Read bounded audit history without creating or migrating audit schema."""
    audit = AuditLog(db_path, readonly=True)
    try:
        return audit.query(
            limit=limit, memory_id=memory_id, session_id=session_id,
            profile=profile, all_sessions=all_sessions,
        )
    finally:
        audit.close()
