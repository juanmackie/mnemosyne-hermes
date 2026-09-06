"""Unit tests for benchmark/retrieval/locomo_eval.py.

Verifies the CLI-invocation fix:
- --db-path is placed BEFORE the `recall` subcommand (root-level flag).
- Timeouts fall through to subprocess.run.
- Non-zero exit / unparseable output raise EvalError (execution failure),
  which evaluate() counts separately from plain retrieval misses.

Isolated: mocks subprocess.run, never invokes the real binary or a DB.
"""

import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "..", "benchmark", "retrieval", "locomo_eval.py")
)

_spec = importlib.util.spec_from_file_location("locomo_eval", _MODULE_PATH)
locomo_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(locomo_eval)


class RunRecallOrderingTest(unittest.TestCase):
    def test_db_path_before_recall_subcommand(self):
        """--db-path is root-level: must precede `recall`."""
        with mock.patch.object(locomo_eval.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=0, stdout=json.dumps({"results": []}), stderr=""
            )
            locomo_eval.run_recall("/tmp/x.db", "q", None, 5, False)
        cmd = run.call_args.args[0]
        self.assertIsInstance(cmd, list)
        self.assertIn("recall", cmd)
        self.assertLess(
            cmd.index("--db-path"), cmd.index("recall"),
            "--db-path must precede the `recall` subcommand",
        )
        self.assertEqual(cmd[cmd.index("--db-path") + 1], "/tmp/x.db")

    def test_recall_scoped_flags_follow_subcommand(self):
        with mock.patch.object(locomo_eval.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=0, stdout=json.dumps({"results": []}), stderr=""
            )
            locomo_eval.run_recall(None, "q", "ns", 3, True)
        cmd = run.call_args.args[0]
        for flag in ("--query", "--namespace", "--limit", "--format", "--hierarchical"):
            self.assertGreater(cmd.index(flag), cmd.index("recall"))


class ExecutionFailureTest(unittest.TestCase):
    def test_timeout_raises_evalerror(self):
        with mock.patch.object(locomo_eval.subprocess, "run",
                               side_effect=locomo_eval.subprocess.TimeoutExpired("cmd", 60)):
            with self.assertRaises(locomo_eval.EvalError):
                locomo_eval.run_recall(None, "q", None, 5, False)

    def test_nonzero_exit_raises_evalerror(self):
        with mock.patch.object(locomo_eval.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(locomo_eval.EvalError):
                locomo_eval.run_recall(None, "q", None, 5, False)

    def test_bad_json_raises_evalerror(self):
        with mock.patch.object(locomo_eval.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="not json", stderr="")
            with self.assertRaises(locomo_eval.EvalError):
                locomo_eval.run_recall(None, "q", None, 5, False)


class SeparateFailureFromMissTest(unittest.TestCase):
    def _run(self, stdout):
        return mock.Mock(returncode=0, stdout=stdout, stderr="")

    def test_failures_and_misses_counted_separately(self):
        # One item: execution failure. One item: real miss (ran, no hit).
        # hit@k must be based only on successfully-executing items.
        calls = []

        def fake_run(cmd, **kwargs):
            # First query: execution failure (non-zero). Second: clean miss.
            calls.append(cmd)
            if "fail-query" in cmd:
                return mock.Mock(returncode=1, stdout="", stderr="boom")
            return mock.Mock(returncode=0, stdout=json.dumps({"results": []}), stderr="")

        items = [
            locomo_eval.EvalItem(query="fail-query", relevant_ids=["x"]),
            locomo_eval.EvalItem(query="clean-miss", relevant_ids=["x"]),
        ]
        with mock.patch.object(locomo_eval.subprocess, "run", side_effect=fake_run):
            row = locomo_eval.evaluate(items, None, None, 5, False)

        self.assertEqual(row["failures"], 1)
        self.assertEqual(row["queries"], 2)
        # Denominator excludes the failed query.
        self.assertEqual(row["hit_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
