"""
Regression tests for Executor execution-boundary fixes.

Covers:
- create_file with a plain relative filename (no parent dir) no longer crashes
  on os.makedirs("").
- File tools are confined to the trusted execution boundary and reject paths
  that escape it.
- run_command runs via a bounded async subprocess (no cross-boundary cwd).

These tests exercise pure executor logic and need no API key.
"""

import asyncio
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "orchestration", "agents"))


class _MockCoordinator:
    def register_agent(self, agent_id):
        pass

    def update_agent_state(self, agent_id, state):
        pass


def _make_executor(working_dir):
    from executor import ExecutorAgent, ExecutorConfig
    return ExecutorAgent(
        config=ExecutorConfig(agent_id="test_executor", working_dir=working_dir),
        coordinator=_MockCoordinator(),
        storage=None,
        parallel_executor=None,
    )


def test_create_file_plain_relative_filename_no_makedirs_crash():
    """create_file with 'main.py' (no parent dir) must not crash on makedirs('')."""
    with tempfile.TemporaryDirectory(prefix="mnem_exec_") as workspace:
        executor = _make_executor(workspace)
        result = asyncio.run(executor._execute_tool(
            "create_file",
            {"file_path": "main.py", "content": "print(1)"},
        ))
        assert result["success"] is True
        assert os.path.isfile(os.path.join(workspace, "main.py"))


def test_create_file_nested_parent_created():
    """create_file with a nested relative path still creates parent dirs."""
    with tempfile.TemporaryDirectory(prefix="mnem_exec_") as workspace:
        executor = _make_executor(workspace)
        result = asyncio.run(executor._execute_tool(
            "create_file",
            {"file_path": "src/lib/mod.rs", "content": "pub fn f() {}"},
        ))
        assert result["success"] is True
        assert os.path.isfile(os.path.join(workspace, "src", "lib", "mod.rs"))


def test_boundary_rejects_escape():
    """A path escaping the trusted boundary must be rejected, not written."""
    with tempfile.TemporaryDirectory(prefix="mnem_exec_") as workspace, \
            tempfile.TemporaryDirectory(prefix="mnem_out_") as outside:
        executor = _make_executor(workspace)
        escape = os.path.join(outside, "pwned.txt")
        # Absolute path outside the boundary
        result = asyncio.run(executor._execute_tool(
            "create_file",
            {"file_path": escape, "content": "evil"},
        ))
        assert result["success"] is False
        assert "boundary" in result["error"]
        assert not os.path.exists(escape)
        # Path traversal via ..
        result2 = asyncio.run(executor._execute_tool(
            "create_file",
            {"file_path": os.path.join(workspace, "..", "..", "escape.txt"), "content": "evil"},
        ))
        assert result2["success"] is False
        assert not os.path.exists(os.path.join(outside, "escape.txt"))


def test_run_command_confined_to_boundary():
    """run_command must reject a working_dir outside the trusted boundary."""
    with tempfile.TemporaryDirectory(prefix="mnem_exec_") as workspace, \
            tempfile.TemporaryDirectory(prefix="mnem_out_") as outside:
        executor = _make_executor(workspace)
        result = asyncio.run(executor._execute_tool(
            "run_command",
            {"command": "echo hi", "working_dir": outside},
        ))
        assert result["success"] is False
        assert "boundary" in result["error"]
