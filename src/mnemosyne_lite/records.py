"""Small immutable records shared by the pure lite planning functions."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Query:
    match: str | None
    literal: str
    namespace: str | None = None
    min_importance: int | None = None
    limit: int = 10


@dataclass(slots=True)
class ThreadState:
    conn: Any = None
    recall_cache: dict[Any, Any] | None = None
    pending: list[tuple[str, ...]] | None = None
    pending_hits: int = 0
    ignored_changes: int = 0
    checkpoint_counter: int = 0
    merge_counter: int = 0


@dataclass(frozen=True, slots=True)
class ConsolidationPlan:
    delete_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...]
