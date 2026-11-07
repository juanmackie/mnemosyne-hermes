"""SQLite execution shell for the lite consolidation plan."""

import logging
import sqlite3
from typing import Any

from .plan import plan_consolidation

logger = logging.getLogger(__name__)


def consolidate(
    storage: Any, namespace: str | None = None, auto_apply: bool = False
) -> dict[str, Any]:
    """Count and preview exact duplicates, optionally applying the pure plan."""
    from .storage import StorageError

    conn = storage._conn()
    try:
        where = " WHERE namespace = ?" if namespace else ""
        params = (namespace,) if namespace else ()
        total = conn.execute("SELECT COUNT(*) FROM memories" + where, params).fetchone()[0]
        # SQLite lower() folds ASCII without ICU. Non-ASCII prefixes may differ
        # from Python str.lower(); this is only a review hint. Exact duplicate
        # detection and deletion use full content equality.
        proposal_groups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT namespace, substr(lower(content), 1, 50) "
            "FROM memories" + where + " GROUP BY namespace, substr(lower(content), 1, 50) "
            "HAVING COUNT(*) > 1)",
            params,
        ).fetchone()[0]
        exact_groups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT namespace, content FROM memories"
            + where
            + " GROUP BY namespace, content HAVING COUNT(*) > 1)",
            params,
        ).fetchone()[0]
        duplicates = conn.execute(
            "SELECT id, namespace, substr(content, 1, 80), importance, created_at FROM memories WHERE "
            "(namespace, content) IN (SELECT namespace, content FROM memories"
            + where
            + " GROUP BY namespace, content HAVING COUNT(*) > 1)",
            params,
        ).fetchall()
    except sqlite3.Error as exc:
        logger.exception("Consolidate read failed")
        raise StorageError(f"consolidate read failed on {storage.db_path}: {exc}") from exc

    removed = 0
    if auto_apply and duplicates:
        try:
            conn.execute("BEGIN IMMEDIATE")
            members = conn.execute(
                "SELECT group_key, id, importance, created_at FROM ("
                "SELECT DENSE_RANK() OVER (ORDER BY namespace, content) AS group_key, "
                "id, importance, created_at, COUNT(*) OVER "
                "(PARTITION BY namespace, content) AS group_count FROM memories"
                + where
                + ") WHERE group_count > 1",
                params,
            ).fetchall()
            plan = plan_consolidation(tuple(tuple(row) for row in members))
            conn.executemany(
                "DELETE FROM memories WHERE id = ?", ((item,) for item in plan.delete_ids)
            )
            removed = len(plan.delete_ids)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.exception("Consolidate delete failed")
            raise StorageError(f"consolidate delete failed on {storage.db_path}: {exc}") from exc
        storage._flush_accesses()
        storage._maybe_checkpoint()

    return {
        "total": total,
        "duplicate_groups": proposal_groups,
        "exact_duplicate_groups": exact_groups,
        "candidates": []
        if auto_apply
        else [{"id": row[0], "namespace": row[1], "preview": row[2]} for row in duplicates][:50],
        "removed": removed,
        "auto_applied": auto_apply,
    }
