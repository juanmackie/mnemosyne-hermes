"""Pure SQL planning for lite memory queries. No SQLite dependency."""

import re
from collections import defaultdict
from collections.abc import Iterable

from .records import AccessBatch, ConsolidationPlan, Query


def fold_accesses(batches: AccessBatch) -> dict[str, int]:
    """Aggregate buffered recall id batches into per-memory hit increments."""
    counts: dict[str, int] = {}
    for ids in batches.ids:
        for memory_id in ids:
            counts[memory_id] = counts.get(memory_id, 0) + 1
    return counts


def fallback_terms(query: str, *, limit: int = 8) -> tuple[str, ...]:
    """Extract distinct, bounded keyword candidates for prefetch fallback."""
    return tuple(dict.fromkeys(re.findall(r"\w{4,}", query.lower())))[:limit]


def plan_consolidation(
    members: Iterable[tuple[int, str, int | None, float | None]],
) -> ConsolidationPlan:
    """Select each duplicate group's keeper by importance then earliest write.

    Rows contain a compact SQL-produced group ordinal, id, importance, and
    creation time. Full memory content never crosses into the planner.
    """
    groups: dict[int, list[tuple[str, int | None, float | None]]] = defaultdict(list)
    for group_id, memory_id, importance, created_at in members:
        groups[group_id].append((memory_id, importance, created_at))
    keep: list[str] = []
    delete: list[str] = []
    for rows in groups.values():
        winner = max(rows, key=lambda row: (row[1] or 0, -(row[2] or 0), row[0]))
        keep.append(winner[0])
        delete.extend(memory_id for memory_id, _, _ in rows if memory_id != winner[0])
    return ConsolidationPlan(tuple(keep), tuple(delete))


def predicate(query: Query, *, alias: str = "m") -> tuple[str, tuple[object, ...]]:
    """Return the shared recall/count WHERE predicate and ordered parameters."""
    if query.match is None:
        clause = f"instr(lower({alias}.content), lower(?)) > 0"
        params: list[object] = [query.literal]
    else:
        clause = "memories_fts MATCH ?"
        params = [query.match]
    if query.namespace:
        clause += f" AND {alias}.namespace = ?"
        params.append(query.namespace)
    if query.min_importance is not None:
        clause += f" AND {alias}.importance >= ?"
        params.append(query.min_importance)
    return clause, tuple(params)


def count_sql(query: Query) -> tuple[str, tuple[object, ...]]:
    clause, params = predicate(query)
    if query.match is None:
        sql = f"SELECT COUNT(*) FROM memories m WHERE {clause}"
    else:
        sql = (
            "SELECT COUNT(*) FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            f"WHERE {clause}"
        )
    return sql, params
