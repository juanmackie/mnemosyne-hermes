"""
Agent Factory - Creates Claude SDK agent instances based on role.

This module provides a factory function for creating specialized agents
(Orchestrator, Optimizer, Reviewer, Executor) with Claude SDK integration.

Used by the Python agent bridge (claude_agent_session) to spawn Python agents.
"""

from typing import Any, Dict, Optional, List
import asyncio
import tempfile
import os
import atexit
import shutil

# Import agent implementations
from .orchestrator import OrchestratorAgent, OrchestratorConfig
from .optimizer import OptimizerAgent, OptimizerConfig
from .reviewer import ReviewerAgent, ReviewerConfig
from .executor import ExecutorAgent, ExecutorConfig

# Shared coordinator (single complete implementation)
from ..coordinator import MockCoordinator

# Import storage (relative to src/)
import sys
_sys_path_added = False
def _ensure_storage_import():
    global _sys_path_added
    if not _sys_path_added:
        _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)
        _sys_path_added = True
_ensure_storage_import()
from lib.storage import PythonMemoryStorage  # noqa: E402


class MockStorage:
    """Mock storage that delegates to real PythonMemoryStorage.

    Uses an in-memory SQLite database by default for standalone usage,
    or a persistent file if db_path is provided.
    """

    _temp_dirs: set = set()

    def __init__(self, db_path: Optional[str] = None):
        """Initialize storage with real backend."""
        if db_path:
            self._storage = PythonMemoryStorage(db_path)
            self._tmp_dir = None
        else:
            # Use temp directory for standalone usage
            self._tmp_dir = tempfile.mkdtemp(prefix='mnemosyne_')
            db_file = os.path.join(self._tmp_dir, 'memories.db')
            self._storage = PythonMemoryStorage(db_file)
            MockStorage._temp_dirs.add(self._tmp_dir)
            atexit.register(self._cleanup_temp)

    @staticmethod
    def _cleanup_temp():
        """Clean up temp directories on exit."""
        for d in list(MockStorage._temp_dirs):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        MockStorage._temp_dirs.clear()

    def _ensure_storage(self):
        """Ensure storage backend is available."""
        if not hasattr(self, '_storage') or self._storage is None:
            raise RuntimeError("Storage not initialized")

    def store(self, memory: Dict[str, Any]):
        """Store memory in real storage."""
        self._ensure_storage()
        self._storage.remember(
            content=memory.get("content", ""),
            namespace=memory.get("namespace", "default"),
            importance=memory.get("importance", 5)
        )

    def remember(self, content: str, namespace: str, importance: int,
                 context: Optional[str] = None) -> Dict[str, Any]:
        """Remember a memory."""
        self._ensure_storage()
        return self._storage.remember(content, namespace, importance, context)

    def recall(self, query: str, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search memories."""
        self._ensure_storage()
        return self._storage.recall(query, namespace)

    def list_memories(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        """List memories."""
        self._ensure_storage()
        return self._storage.list_memories(namespace)

    def count(self, namespace: Optional[str] = None) -> int:
        """Count memories."""
        self._ensure_storage()
        return self._storage.count(namespace)

    def graph(self, query: Optional[str] = None, namespace: Optional[str] = None,
              depth: int = 1) -> Dict[str, Any]:
        """Get memory graph."""
        self._ensure_storage()
        return self._storage.graph(query, namespace, depth)

    def consolidate(self, namespace: Optional[str] = None,
                    auto_apply: bool = False) -> Dict[str, Any]:
        """Consolidate similar memories."""
        self._ensure_storage()
        return self._storage.consolidate(namespace, auto_apply)


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
            coordinator=mock_coordinator,
            storage=mock_storage
        )

    elif role == "reviewer":
        agent_config = ReviewerConfig(
            agent_id="reviewer",
            **config
        )
        return ReviewerAgent(
            config=agent_config,
            coordinator=mock_coordinator,
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
