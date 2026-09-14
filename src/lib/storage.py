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
    ]

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
        self._init_schema()

    def _ensure_db_dir(self):
        """Ensure the database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """Get a connection with WAL mode and safe settings."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self):
        """Initialize database schema if not exists."""
        conn = self._get_conn()
        try:
            conn.execute(self.SCHEMA)
            for idx_sql in self.INDEXES:
                conn.execute(idx_sql)
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Schema initialization failed: {e}")
            raise
        finally:
            conn.close()

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

        conn = self._get_conn()
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
        finally:
            conn.close()

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

        conn = self._get_conn()
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
        finally:
            conn.close()

        results = [self._row_to_dict(row) for row in rows]

        # Update access count
        if results:
            conn = self._get_conn()
            try:
                now = time.time()
                for r in results:
                    conn.execute(
                        "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                        (now, r["id"])
                    )
                conn.commit()
            except sqlite3.Error:
                logger.warning("Failed to update access counts")
            finally:
                conn.close()

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

        conn = self._get_conn()
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
        finally:
            conn.close()

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
        conn = self._get_conn()
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
        finally:
            conn.close()

        # Simple dedup: group by namespace + first 50 chars of content
        groups: Dict[str, List[str]] = {}
        for row in rows:
            key = f"{row[2]}:{row[1][:50].lower()}"
            groups.setdefault(key, []).append(row[0])

        duplicates = {k: v for k, v in groups.items() if len(v) > 1}
        removed = 0

        if auto_apply and duplicates:
            conn = self._get_conn()
            try:
                for dup_ids in duplicates.values():
                    for dup_id in dup_ids[1:]:
                        conn.execute("DELETE FROM memories WHERE id = ?", (dup_id,))
                        removed += 1
                conn.commit()
            except sqlite3.Error:
                logger.exception("Consolidate delete failed")
            finally:
                conn.close()

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
        conn = self._get_conn()
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
        finally:
            conn.close()