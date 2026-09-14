"""
Agent Factory - Creates Claude SDK agent instances based on role.

This module provides a factory function for creating specialized agents
(Orchestrator, Optimizer, Reviewer, Executor) with Claude SDK integration.

Used by the Python agent bridge (claude_agent_session) to spawn Python agents.
"""

from typing import Any, Dict, Optional, List
import asyncio

# Import agent implementations
from .orchestrator import OrchestratorAgent, OrchestratorConfig
from .optimizer import OptimizerAgent, OptimizerConfig
from .reviewer import ReviewerAgent, ReviewerConfig
from .executor import ExecutorAgent, ExecutorConfig


class MockCoordinator:
    """Mock coordinator for testing/standalone agent usage."""

    def __init__(self):
        self._agents = {}

    def register_agent(self, agent_id: str):
        """Register an agent."""
        self._agents[agent_id] = "idle"

    def update_agent_state(self, agent_id: str, state: str):
        """Update agent state."""
        if agent_id in self._agents:
            self._agents[agent_id] = state

    def get_context_utilization(self) -> float:
        """Get context utilization (mock returns 0.5)."""
        return 0.5


class MockStorage:
    """Mock storage for testing/standalone agent usage."""

    def __init__(self, db_path: Optional[str] = None):
        """Initialize mock storage with optional real backend."""
        self._db_path = db_path
        self._real_storage = None
        if db_path:
            from lib.storage import PythonMemoryStorage
            self._real_storage = PythonMemoryStorage(db_path)

    def store(self, memory: Dict[str, Any]):
        """Store memory — delegates to real storage if available."""
        if self._real_storage:
            self._real_storage.remember(
                content=memory.get("content", ""),
                namespace=memory.get("namespace", "default"),
                importance=memory.get("importance", 5)
            )

    def recall(self, query: str, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search memories."""
        if self._real_storage:
            return self._real_storage.recall(query, namespace)
        return []

    def list_memories(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """List memories."""
        if self._real_storage:
            return self._real_storage.list_memories(namespace)
        return []


class MockParallelExecutor:
    """Mock parallel executor for testing/standalone agent usage."""

    def __init__(self):
        pass


class MockContextMonitor:
    """Mock context monitor for testing/standalone agent usage."""

    def set_preservation_callback(self, callback):
        """Set preservation callback (no-op for testing)."""
        pass


def create_agent(
    role: str,
    config: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None
) -> Any:
    """
    Create an agent instance based on role.

    Args:
        role: Agent role ("orchestrator", "optimizer", "reviewer", "executor")
        config: Optional configuration dict (may include 'anthropic_api_key')
        db_path: Optional path to SQLite database for real storage

    Returns:
        Agent instance with Claude SDK client initialized

    Raises:
        ValueError: If role is unknown
    """
    import os

    config = config or {}

    # If API key is provided in config, set it as environment variable
    if "anthropic_api_key" in config:
        os.environ["ANTHROPIC_API_KEY"] = config["anthropic_api_key"]
        del config["anthropic_api_key"]

    # Use real storage if db_path provided, otherwise mock
    mock_coordinator = MockCoordinator()
    mock_storage = MockStorage(db_path=db_path)
    mock_parallel_executor = MockParallelExecutor()
    mock_context_monitor = MockContextMonitor()

    if role == "orchestrator":
        agent_config = OrchestratorConfig(
            agent_id="orchestrator",
            **config
        )
        return OrchestratorAgent(
            config=agent_config,
            coordinator=mock_coordinator,
            storage=mock_storage,
            context_monitor=mock_context_monitor
        )

    elif role == "optimizer":
        agent_config = OptimizerConfig(
            agent_id="optimizer",
            **config
        )
        return OptimizerAgent(
            config=agent_config,
            skills_directory=config.get("skills_directory", "skills"),
            storage=mock_storage
        )

    elif role == "reviewer":
        agent_config = ReviewerConfig(
            agent_id="reviewer",
            **config
        )
        return ReviewerAgent(
            config=agent_config,
            storage=mock_storage
        )

    elif role == "executor":
        agent_config = ExecutorConfig(
            agent_id="executor",
            **config
        )
        return ExecutorAgent(
            config=agent_config,
            coordinator=mock_coordinator,
            storage=mock_storage,
            parallel_executor=mock_parallel_executor
        )

    else:
        raise ValueError(f"Unknown agent role: {role}. Must be one of: orchestrator, optimizer, reviewer, executor")


async def create_agent_async(role: str, config: Optional[Dict[str, Any]] = None) -> Any:
    """
    Create an agent instance asynchronously and start session.

    Args:
        role: Agent role ("orchestrator", "optimizer", "reviewer", "executor")
        config: Optional configuration dict

    Returns:
        Agent instance with active Claude SDK session

    Raises:
        ValueError: If role is unknown
    """
    agent = create_agent(role, config)
    await agent.start_session()
    return agent
