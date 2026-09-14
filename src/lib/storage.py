"""
Python-native memory storage backend.

Replaces the Rust CLI subprocess approach with direct SQLite access.
Provides async-compatible storage for Mnemosyne memories.

Uses sqlite3 from stdlib — no external dependencies needed.
"""
import sqlite3
import json
import os
import hashlib
import time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict
from pathlib import Path


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
    """

    def __init__(self, db_path: str):
        """
        Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._ensure_db_dir()
        self._init_schema()

    def _ensure_db_dir(self):
        """Ensure the database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _init_schema(self):
        """Initialize database schema if not exists."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    importance INTEGER DEFAULT 5,
                    context TEXT,
                    summary TEXT,
                    keywords TEXT,
                    created_at REAL DEFAULT 0,
                    access_count INTEGER DEFAULT 0,
                    last_accessed REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_namespace
                ON memories(namespace)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_importance
                ON memories(importance)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_created
                ON memories(created_at)
            """)
            conn.commit()
        finally:
            conn.close()

    def remember(
        self,
        content: str,
        namespace: str,
        importance: int,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Store a memory."""
        mem_id = hashlib.sha256(
            f"{content}:{namespace}:{time.time()}".encode()
        ).hexdigest()[:16]

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO memories
                   (id, content, namespace, importance, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (mem_id, content, namespace, importance, context, time.time())
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "id": mem_id,
            "content": content,
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
        """Search memories by keyword match."""
        conn = sqlite3.connect(self.db_path)
        try:
            sql = "SELECT * FROM memories WHERE content LIKE ?"
            params = [f"%{query}%"]

            if namespace:
                sql += " AND namespace = ?"
                params.append(namespace)

            if min_importance is not None:
                sql += " AND importance >= ?"
                params.append(min_importance)

            sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
            params.append(max_results)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "content": row[1],
                "namespace": row[2],
                "importance": row[3],
                "context": row[4],
                "summary": row[5],
                "keywords": row[6],
                "created_at": row[7],
                "access_count": row[8],
                "last_accessed": row[9]
            })

        # Update access count
        if results:
            conn = sqlite3.connect(self.db_path)
            try:
                for r in results:
                    conn.execute(
                        "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                        (time.time(), r["id"])
                    )
                conn.commit()
            finally:
                conn.close()

        return results

    def list_memories(
        self,
        namespace: Optional[str] = None,
        limit: int = 20,
        sort_by: str = "recent"
    ) -> List[Dict[str, Any]]:
        """List memories."""
        conn = sqlite3.connect(self.db_path)
        try:
            sql = "SELECT * FROM memories"
            params = []

            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            if sort_by == "recent":
                sql += " ORDER BY created_at DESC"
            elif sort_by == "importance":
                sql += " ORDER BY importance DESC"
            elif sort_by == "access":
                sql += " ORDER BY access_count DESC"
            else:
                sql += " ORDER BY created_at DESC"

            sql += " LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [
            {
                "id": row[0],
                "content": row[1],
                "namespace": row[2],
                "importance": row[3],
                "context": row[4],
                "summary": row[5],
                "keywords": row[6],
                "created_at": row[7],
                "access_count": row[8],
                "last_accessed": row[9]
            }
            for row in rows
        ]

    def consolidate(
        self,
        namespace: Optional[str] = None,
        auto_apply: bool = False
    ) -> Dict[str, Any]:
        """Consolidate similar memories (dedup by content similarity)."""
        conn = sqlite3.connect(self.db_path)
        try:
            sql = "SELECT id, content, namespace FROM memories"
            params = []
            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            rows = conn.execute(sql, params).fetchall()
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
            conn = sqlite3.connect(self.db_path)
            try:
                for dup_ids in duplicates.values():
                    # Keep first, remove rest
                    for dup_id in dup_ids[1:]:
                        conn.execute("DELETE FROM memories WHERE id = ?", (dup_id,))
                        removed += 1
                conn.commit()
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
        """Get memory graph structure."""
        conn = sqlite3.connect(self.db_path)
        try:
            memories = self.list_memories(
                namespace=namespace,
                limit=1000,
                sort_by="recent"
            )
        finally:
            conn.close()

        nodes = []
        edges = []

        for mem in memories:
            nodes.append({
                "id": mem["id"],
                "content": mem["content"][:100],
                "namespace": mem["namespace"],
                "importance": mem["importance"]
            })

        # Create edges between memories in same namespace
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
        """Count memories."""
        conn = sqlite3.connect(self.db_path)
        try:
            if namespace:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                    (namespace,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()