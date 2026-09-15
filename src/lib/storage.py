"""
Python-native memory storage backend.

Replaces the Rust CLI subprocess approach with direct SQLite access.
Provides async-compatible storage for Mnemosyne memories.

Uses sqlite3 from stdlib — no external dependencies needed.

Thread-safe: each operation opens its own connection (SQLite constraint).
For concurrent access, use WAL mode and external locking.
"""
import sqlite3
import os
import threading
import hashlib
import time
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class MemoryRecord:
    id: str
    content: str
    namespace: str
    importance: int
    context: Optional[str] = None
    summary: Optional[str] = None
    keywords: Optional[List[str]] = None
    created_at: Optional[float] = None
    access_count: int = 0
    last_accessed: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class PythonMemoryStorage:
    """
    Python-native memory storage using SQLite.

    Drop-in replacement for the Rust CLI backend.
    Provides the same interface as MnemosyneClient but with
    direct database access — no subprocess overhead.

    Thread safety: Each operation opens its own connection.
    For concurrent writes, enable WAL mode (default).
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            namespace TEXT NOT NULL,
            importance INTEGER DEFAULT 5 CHECK(importance >= 0 AND importance <= 10),
            context TEXT,
            summary TEXT,
            keywords TEXT,
            created_at REAL DEFAULT 0,
            access_count INTEGER DEFAULT 0,
            last_accessed REAL
        )
    """

    INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace)",
        "CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance)",
        "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_ns_created ON memories(namespace, created_at)",
        # Matches the recall query shape (namespace filter + ORDER BY importance
        # DESC, created_at DESC) *and* carries content, so the scan can filter
        # on the LIKE and walk rows in output order without touching the table.
        # Without content here, every scanned row cost a table fetch (~0.4ms of
        # the ~0.8ms tail for selective queries); with it, only the returned
        # rows are fetched. Costs one content-sized index (see README notes).
        "CREATE INDEX IF NOT EXISTS idx_memories_recall ON memories(namespace, importance, created_at, content)",
    ]

    # Superseded by idx_memories_recall (same leading columns, plus content).
    # Dropped rather than reused because IF NOT EXISTS matches on the name
    # only, so an existing index would keep the old column list.
    DROPPED_INDEXES = ["DROP INDEX IF EXISTS idx_memories_ns_rank"]

    def __init__(self, db_path: str):
        """
        Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file

        Raises:
            OSError: If database directory cannot be created
            sqlite3.Error: If database cannot be initialized
        """
        if not db_path:
            raise ValueError("db_path is required")
        self.db_path = db_path
        self._ensure_db_dir()
        self._local = threading.local()
        self._init_schema()

    def _ensure_db_dir(self):
        """Ensure the database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _new_conn(self) -> sqlite3.Connection:
        """Open a fresh connection with WAL mode and safe settings."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL skips an fsync per commit (only at checkpoint). Still
        # crash-safe for this store, and commits were the bulk of warm recall
        # cost once connection setup was cached.
        conn.execute("PRAGMA synchronous=FULL")
        # M1: authoritative commits (FULL durability). Normal skipped fsync; FULL guarantees acknowledged writes survive crash/restart. WAL stays active.
        # Auto-checkpoint runs a PASSIVE checkpoint *inside* whichever commit
        # crosses the page threshold -- and recall commits (the access-count
        # bump), so a read could stall for 12-481ms (measured p99 11.3ms, max
        # 481ms over 300 recalls). Checking in on the write path instead keeps
        # the WAL bounded without ever paying that cost on a read.
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _conn(self) -> sqlite3.Connection:
        """Return this thread's cached connection, opening it on first use.

        Opening a connection costs a WAL pragma round-trip, and the old code
        did it (twice) on every recall -- that dominated warm recall latency.
        SQLite connections are not shareable across threads, so cache one per
        thread. Use close() to release it.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Flush buffered access counts and close this thread's connection."""
        self._flush_accesses()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # Checkpoint when the WAL outgrows this. Called from the write paths only.
    WAL_CHECKPOINT_BYTES = 4 * 1024 * 1024

    def _maybe_checkpoint(self) -> None:
        """Fold a large WAL back into the database once it outgrows a bound.

        With wal_autocheckpoint off, nothing would checkpoint until close(),
        and an ingest of a few thousand memories was measured leaving a 71MB
        WAL next to a 700KB database. Checkpointing is called from the write
        paths and from a flush, never from the read work itself, so a recall
        only ever pays it once per several thousand calls. TRUNCATE is
        best-effort: it returns busy without raising if a reader is active,
        and the next write retries.
        """
        try:
            wal = self.db_path + "-wal"
            if os.path.exists(wal) and os.path.getsize(wal) >= self.WAL_CHECKPOINT_BYTES:
                self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.Error):
            logger.warning("WAL checkpoint skipped", exc_info=True)

    # Apply buffered access counts once this many distinct memories are pending,
    # or once this many hits are pending (a workload that recalls the same few
    # memories forever would never reach the distinct cap).
    ACCESS_FLUSH_DISTINCT = 256
    ACCESS_FLUSH_HITS = 1024

    def _pending(self) -> Dict[str, int]:
        """This thread's not-yet-written access counts, as {memory id: hits}."""
        pending = getattr(self._local, "pending", None)
        if pending is None:
            pending = self._local.pending = {}
        return pending

    def _flush_accesses(self) -> None:
        """Write out buffered recall access counts. Best effort by design.

        Called from the write paths, from readers of access_count, and from
        close(). On error the batch is dropped: a stale popularity count is
        not worth retrying, and retrying would let the buffer grow unbounded.
        """
        pending = self._pending()
        if not pending:
            return
        batch, self._local.pending = pending, {}
        self._local.pending_hits = 0
        try:
            now = time.time()
            self._conn().executemany(
                "UPDATE memories SET access_count = access_count + ?, last_accessed = ? WHERE id = ?",
                [(hits, now, mem_id) for mem_id, hits in batch.items()]
            )
            self._conn().commit()
        except sqlite3.Error:
            logger.warning("Failed to apply buffered access counts", exc_info=True)
        self._maybe_checkpoint()

    def _init_schema(self):
        """Initialize database schema if not exists."""
        init_conn = self._new_conn()
        try:
            init_conn.execute(self.SCHEMA)
            for drop_sql in self.DROPPED_INDEXES:
                init_conn.execute(drop_sql)
            for idx_sql in self.INDEXES:
                init_conn.execute(idx_sql)
            init_conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Schema initialization failed: {e}")
            raise
        finally:
            init_conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a database row to a memory dict."""
        return {
            "id": row[0],
            "content": row[1],
            "namespace": row[2],
            "importance": row[3],
            "context": row[4],
            "summary": row[5],
            "keywords": row[6],
            "created_at": row[7],
            "access_count": row[8],
            "last_accessed": row[9],
        }

    def remember(
        self,
        content: str,
        namespace: str,
        importance: int,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Store a memory.

        Args:
            content: Memory content (required, max 100K chars)
            namespace: Namespace (required)
            importance: Importance score 0-10
            context: Optional context information

        Returns:
            dict: Memory metadata (id, success)

        Raises:
            ValueError: If content or namespace is empty
            sqlite3.Error: If database write fails
        """
        if not content or not content.strip():
            raise ValueError("content cannot be empty")
        if not namespace or not namespace.strip():
            raise ValueError("namespace cannot be empty")
        if not (0 <= importance <= 10):
            raise ValueError(f"importance must be 0-10, got {importance}")

        mem_id = hashlib.sha256(
            f"{content}:{namespace}:{time.time()}".encode()
        ).hexdigest()[:16]

        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO memories
                   (id, content, namespace, importance, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (mem_id, content[:100000], namespace, importance, context, time.time())
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Collision — regenerate ID and retry once
            mem_id = hashlib.sha256(
                f"{content}:{namespace}:{time.time()}:retry".encode()
            ).hexdigest()[:16]
            conn.execute(
                """INSERT INTO memories
                   (id, content, namespace, importance, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (mem_id, content[:100000], namespace, importance, context, time.time())
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to store memory")
            raise

        self._flush_accesses()
        self._maybe_checkpoint()

        return {
            "id": mem_id,
            "content": content[:200],  # Truncate in response
            "namespace": namespace,
            "importance": importance,
            "success": True
        }

    def recall(
        self,
        query: str,
        namespace: Optional[str] = None,
        max_results: int = 10,
        min_importance: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Search memories by keyword match.

        Args:
            query: Search term (required)
            namespace: Optional namespace filter
            max_results: Max results (1-100)
            min_importance: Minimum importance filter

        Returns:
            List of matching memories

        Raises:
            ValueError: If query is empty
        """
        if not query or not query.strip():
            raise ValueError("query cannot be empty")

        max_results = max(1, min(100, max_results))

        conn = self._conn()
        try:
            sql = "SELECT * FROM memories WHERE content LIKE ? ESCAPE '\\'"
            # Escape LIKE metacharacters so a literal '%' or '_' in the query
            # does not turn into a wildcard (which would match everything).
            escaped = (query.replace("\\", "\\\\")
                            .replace("%", "\\%")
                            .replace("_", "\\_"))
            params = [f"%{escaped}%"]

            if namespace:
                sql += " AND namespace = ?"
                params.append(namespace)

            if min_importance is not None:
                sql += " AND importance >= ?"
                params.append(min_importance)

            sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
            params.append(max_results)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.exception("Recall query failed")
            return []

        results = [self._row_to_dict(row) for row in rows]

        # Record the access in memory instead of writing it here: the UPDATE +
        # commit was 60% of a median recall (p50 0.310 -> 0.119ms) and it was
        # the write that let a WAL checkpoint stall a read. See _flush_accesses().
        if results:
            pending = self._pending()
            for r in results:
                pending[r["id"]] = pending.get(r["id"], 0) + 1
            hits = getattr(self._local, "pending_hits", 0) + len(results)
            self._local.pending_hits = hits
            if len(pending) >= self.ACCESS_FLUSH_DISTINCT or hits >= self.ACCESS_FLUSH_HITS:
                self._flush_accesses()

        return results

    def list_memories(
        self,
        namespace: Optional[str] = None,
        limit: int = 20,
        sort_by: str = "recent"
    ) -> List[Dict[str, Any]]:
        """
        List memories.

        Args:
            namespace: Optional namespace filter
            limit: Max results (1-1000)
            sort_by: Sort order (recent, importance, access)

        Returns:
            List of memories
        """
        limit = max(1, min(1000, limit))
        # This is where buffered access counts become visible (sort_by="access").
        self._flush_accesses()

        conn = self._conn()
        try:
            sql = "SELECT * FROM memories"
            params = []

            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            sort_map = {
                "recent": "created_at DESC",
                "importance": "importance DESC",
                "access": "access_count DESC",
            }
            sql += f" ORDER BY {sort_map.get(sort_by, 'created_at DESC')}"

            sql += " LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.exception("List query failed")
            return []

        return [self._row_to_dict(row) for row in rows]

    def consolidate(
        self,
        namespace: Optional[str] = None,
        auto_apply: bool = False
    ) -> Dict[str, Any]:
        """
        Consolidate similar memories (dedup by content prefix).

        Args:
            namespace: Optional namespace filter
            auto_apply: Actually delete duplicates if True

        Returns:
            Consolidation results
        """
        conn = self._conn()
        try:
            sql = "SELECT id, content, namespace FROM memories"
            params = []
            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.exception("Consolidate read failed")
            return {"total": 0, "duplicate_groups": 0, "removed": 0, "auto_applied": auto_apply}

        # Simple dedup: group by namespace + first 50 chars of content
        groups: Dict[str, List[str]] = {}
        for row in rows:
            key = f"{row[2]}:{row[1][:50].lower()}"
            groups.setdefault(key, []).append(row[0])

        duplicates = {k: v for k, v in groups.items() if len(v) > 1}
        removed = 0

        if auto_apply and duplicates:
            conn = self._conn()
            try:
                for dup_ids in duplicates.values():
                    for dup_id in dup_ids[1:]:
                        conn.execute("DELETE FROM memories WHERE id = ?", (dup_id,))
                        removed += 1
                conn.commit()
            except sqlite3.Error:
                logger.exception("Consolidate delete failed")
            self._flush_accesses()
            self._maybe_checkpoint()

        return {
            "total": len(rows),
            "duplicate_groups": len(duplicates),
            "removed": removed,
            "auto_applied": auto_apply
        }

    def graph(
        self,
        query: Optional[str] = None,
        namespace: Optional[str] = None,
        depth: int = 1
    ) -> Dict[str, Any]:
        """
        Get memory graph structure.

        Args:
            query: Optional search query (not implemented, reserved)
            namespace: Optional namespace filter
            depth: Graph depth (0 = no edges, 1 = namespace edges)

        Returns:
            Graph structure with nodes and edges
        """
        memories = self.list_memories(
            namespace=namespace,
            limit=1000,
            sort_by="recent"
        )

        nodes = [
            {
                "id": m["id"],
                "content": m["content"][:100],
                "namespace": m["namespace"],
                "importance": m["importance"]
            }
            for m in memories
        ]

        edges = []
        if depth > 0:
            by_namespace: Dict[str, List[str]] = {}
            for mem in memories:
                by_namespace.setdefault(mem["namespace"], []).append(mem["id"])

            for ns, ids in by_namespace.items():
                for i in range(len(ids) - 1):
                    edges.append({
                        "source": ids[i],
                        "target": ids[i + 1],
                        "type": "namespace"
                    })

        return {
            "nodes": nodes,
            "edges": edges,
            "total_memories": len(memories)
        }

    def count(self, namespace: Optional[str] = None) -> int:
        """
        Count memories.

        Args:
            namespace: Optional namespace filter

        Returns:
            Number of memories
        """
        conn = self._conn()
        try:
            if namespace:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                    (namespace,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            logger.exception("Count query failed")
            return 0