"""
Shared coordinator stub for the orchestration runtime.

The orchestration agents, context monitor, and parallel executor all talk to a
coordinator object that exposes a small shared-state surface (agent states,
metrics, context utilization, ready tasks). Production wires a real
``PyCoordinator``; standalone usage and tests use :class:`MockCoordinator`.

Keeping one implementation here means every component sees the same complete
interface — the previous per-module copies silently lacked methods such as
``get_all_agent_states`` or ``update_context_utilization``, which made the
context monitor fail on every poll.
"""
from typing import Any, Dict, Optional, Set


class MockCoordinator:
    """In-process coordinator used for standalone runs and tests.

    All state lives in this object; nothing is persisted or synchronized
    across processes. Methods are intentionally synchronous (the interface the
    agents already use).
    """

    def __init__(self) -> None:
        self._agents: Dict[str, str] = {}
        self._metrics: Dict[str, Any] = {}
        self._context_utilization: float = 0.0
        self._ready_tasks: Set[str] = set()

    # ---- agent registry -----------------------------------------------------
    def register_agent(self, agent_id: str) -> None:
        """Register an agent as idle (idempotent)."""
        self._agents[agent_id] = self._agents.get(agent_id, "idle")

    def update_agent_state(self, agent_id: str, state: str) -> None:
        """Set the state of an agent, registering it if unknown."""
        self._agents[agent_id] = state

    def get_all_agent_states(self) -> Dict[str, str]:
        """Return a snapshot of agent states keyed by agent id."""
        return dict(self._agents)

    # ---- metrics ------------------------------------------------------------
    def set_metric(self, name: str, value: Any) -> None:
        """Store a named metric value."""
        self._metrics[name] = value

    def get_metric(self, name: str, default: Any = None) -> Any:
        """Return a named metric value, or ``default`` when unset."""
        return self._metrics.get(name, default)

    # ---- context utilization ------------------------------------------------
    def update_context_utilization(self, utilization: float) -> None:
        """Record the current context utilization (0.0 - 1.0)."""
        self._context_utilization = float(utilization)

    def get_context_utilization(self) -> float:
        """Return the last recorded context utilization."""
        return self._context_utilization

    # ---- task readiness -----------------------------------------------------
    def mark_task_ready(self, task_id: str) -> None:
        """Record that a task's dependencies are satisfied."""
        self._ready_tasks.add(task_id)

    def is_task_ready(self, task_id: str) -> bool:
        """Whether a task has been marked ready."""
        return task_id in self._ready_tasks


__all__ = ["MockCoordinator"]
