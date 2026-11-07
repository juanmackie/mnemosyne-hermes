"""Pure SQL planning for lite memory queries. No SQLite dependency."""

import re

from .records import ConsolidationPlan, Query


def fold_accesses(batches: tuple[tuple[str, ...], ...] | list[tuple[str, ...]]) -> dict[str, int]:
    """Aggregate buffered recall id batches into per-memory hit increments."""
    counts: dict[str, int] = {}
    for ids in batches:
        for memory_id in ids:
            counts[memory_id] = counts.get(memory_id, 0) + 1
    return counts


def fallback_terms(query: str, *, limit: int = 8) -> tuple[str, ...]:
    """Extract distinct, bounded keyword candidates for prefetch fallback."""
    return tuple(dict.fromkeys(re.findall(r"\w{4,}", query.lower())))[:limit]


def plan_consolidation(delete_ids: tuple[str, ...] | list[str]) -> ConsolidationPlan:
    """Freeze SQLite's already-ranked duplicate losers into a delete plan."""
    return ConsolidationPlan(tuple(dict.fromkeys(delete_ids)))


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
        sql = ("SELECT COUNT(*) FROM memories_fts "
               "JOIN memories m ON m.rowid = memories_fts.rowid "
               f"WHERE {clause}")
    return sql, params
