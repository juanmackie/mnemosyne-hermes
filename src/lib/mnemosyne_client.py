"""
Python client for Mnemosyne.

Provides async interface for storing and retrieving memories from Python code.
Uses Python-native SQLite storage — no subprocess overhead.
"""
import os
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse

from .storage import PythonMemoryStorage


def resolve_db_path(db_path: Optional[str] = None) -> str:
    """Resolve a usable SQLite file path from an explicit path or DATABASE_URL.

    SQLite only understands filesystem paths (and the ``:memory:`` special
    case). A URL like ``sqlite:///var/lib/mnemosyne.db`` is stripped to its
    path; any other scheme (``postgres://``, ``mysql://``, ...) is rejected
    loudly instead of being silently used to create a bogus file whose name is
    the URL.

    Args:
        db_path: Explicit path. When omitted, ``DATABASE_URL`` is consulted,
            then ``~/.mnemosyne/mnemosyne.db``.

    Returns:
        A filesystem path (or ``:memory:``).

    Raises:
        ValueError: If DATABASE_URL uses an unsupported scheme.
    """
    if db_path:
        return db_path

    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        return os.path.expanduser("~/.mnemosyne/mnemosyne.db")

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme not in ("sqlite", "sqlite3", "file"):
            raise ValueError(
                f"Unsupported DATABASE_URL scheme {parsed.scheme!r}; "
                "Mnemosyne storage is SQLite-only (use sqlite:///path or a plain path)"
            )
        # sqlite:///abs/path -> /abs/path; sqlite:///rel -> rel; sqlite:// -> ''
        path = parsed.path or parsed.netloc
        if parsed.netloc and parsed.netloc != "localhost":
            path = parsed.netloc + parsed.path
        if path in ("", "/"):
            return ":memory:"
        return path

    return raw


class MnemosyneClient:
    """
    Async client for Mnemosyne memory operations.

    Uses Python-native SQLite storage for direct database access.
    No subprocess overhead — all operations are in-process.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        storage: Optional[PythonMemoryStorage] = None
    ):
        """
        Initialize Mnemosyne client.

        Args:
            db_path: Optional path to SQLite database
            storage: Optional pre-configured storage backend
        """
        self.db_path = resolve_db_path(db_path)
        self.storage = storage or PythonMemoryStorage(self.db_path)

    async def remember(
        self,
        content: str,
        namespace: str,
        importance: int,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Store a memory in Mnemosyne.

        Args:
            content: Memory content
            namespace: Namespace (e.g., "session:orchestration")
            importance: Importance score 1-10
            context: Optional context information

        Returns:
            dict: Memory metadata (id, summary, keywords)
        """
        return self.storage.remember(content, namespace, importance, context)

    async def recall(
        self,
        query: str,
        namespace: Optional[str] = None,
        max_results: int = 10,
        min_importance: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Search Mnemosyne memories.

        Args:
            query: Search query
            namespace: Optional namespace filter
            max_results: Maximum number of results
            min_importance: Minimum importance filter

        Returns:
            List[dict]: Matching memories
        """
        return self.storage.recall(query, namespace, max_results, min_importance)

    async def list_memories(
        self,
        namespace: Optional[str] = None,
        limit: int = 20,
        sort_by: str = "recent"
    ) -> List[Dict[str, Any]]:
        """List memories.

        Args:
            namespace: Optional namespace filter
            limit: Maximum number of results
            sort_by: Sort order (recent, importance, access)

        Returns:
            List[dict]: Memories
        """
        return self.storage.list_memories(namespace, limit, sort_by)

    async def consolidate(
        self,
        namespace: Optional[str] = None,
        auto_apply: bool = False
    ) -> Dict[str, Any]:
        """Consolidate similar memories.

        Args:
            namespace: Optional namespace filter
            auto_apply: Automatically apply consolidation recommendations

        Returns:
            dict: Consolidation results
        """
        return self.storage.consolidate(namespace, auto_apply)

    async def graph(
        self,
        query: Optional[str] = None,
        namespace: Optional[str] = None,
        depth: int = 1,
    ) -> Dict[str, Any]:
        """Get memory graph.

        Args:
            query: Optional search query to center graph
            namespace: Optional namespace filter
            depth: Graph traversal depth

        Returns:
            dict: Graph structure (nodes, edges)
        """
        return self.storage.graph(query, namespace, depth)

    def __repr__(self) -> str:
        return f"MnemosyneClient(db={self.db_path})"