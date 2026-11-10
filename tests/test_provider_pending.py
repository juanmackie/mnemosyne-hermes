"""Pending approvals must be claimed before a concurrent apply can write."""

import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "integrations" / "hermes-provider"))
import hermes_memory_provider as provider_module  # noqa: E402


class _TransactionalConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._defer_commit = False

    def commit(self):
        if not self._defer_commit:
            super().commit()

    def _real_commit(self):
        super().commit()


@contextmanager
def _deferred_commits(conn):
    conn._defer_commit = True
    try:
        yield
    except BaseException:
        conn._defer_commit = False
        conn.rollback()
        raise
    else:
        conn._defer_commit = False
        conn._real_commit()


def _engine_transaction_modules():
    mnemosyne = types.ModuleType("mnemosyne")
    mnemosyne.__path__ = []
    core = types.ModuleType("mnemosyne.core")
    core.__path__ = []
    beam = types.ModuleType("mnemosyne.core.beam")
    beam._deferred_commits = _deferred_commits
    return {"mnemosyne": mnemosyne, "mnemosyne.core": core, "mnemosyne.core.beam": beam}


class _Beam:
    def __init__(self, started, release):
        self.started = started
        self.release = release
        self.calls = 0

    def remember(self, **_kwargs):
        self.calls += 1
        self.started.set()
        assert self.release.wait(2)
        return "memory-id"


class PendingApplyTests(unittest.TestCase):
    def test_concurrent_pending_apply_only_applies_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            pending_dir = tmp_path / "pending" / "memory"
            pending_dir.mkdir(parents=True)
            pending_id = "deadbeef"
            (pending_dir / f"{pending_id}.json").write_text(
                json.dumps({"id": pending_id, "payload": {"content": "approved memory"}})
            )
            constants = types.ModuleType("hermes_constants")
            constants.get_hermes_home = lambda: tmp_path
            veracity = types.ModuleType("mnemosyne.core.veracity_consolidation")
            veracity.clamp_veracity = lambda value, **_kwargs: value
            started, release = threading.Event(), threading.Event()
            provider = provider_module.MnemosyneMemoryProvider()
            provider._beam = _Beam(started, release)
            provider._default_scope = "default"
            provider._asserted_caller_metadata = lambda value: value
            results = []
            with patch.dict(
                sys.modules,
                {
                    "hermes_constants": constants,
                    "mnemosyne.core.veracity_consolidation": veracity,
                },
            ):
                worker = threading.Thread(
                    target=lambda: results.append(
                        provider._handle_apply_pending({"pending_ids": [pending_id]})
                    )
                )
                worker.start()
                self.assertTrue(started.wait(2))
                second = json.loads(provider._handle_apply_pending({"pending_ids": [pending_id]}))
                self.assertEqual(second["applied_count"], 0)
                self.assertIn("claimed", second["failed"][0]["error"])
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(provider._beam.calls, 1)
            self.assertFalse((pending_dir / f"{pending_id}.json").exists())
            self.assertFalse((pending_dir / f"{pending_id}.json.applying").exists())

    def test_prefetch_cache_payload_keeps_recall_ids_per_call(self):
        provider = provider_module.MnemosyneMemoryProvider()
        provider._beam = object()
        provider._prefetch_profile = "general"
        provider._prefetch_cache_enabled = lambda: True
        provider._prefetch_cache_key = lambda query, _session, _profile: (query,)
        profile = SimpleNamespace(sources=["bank"], dedup=False)
        first_recalled = threading.Event()
        second_recalled = threading.Event()

        def bank(query, _session, _profile):
            memory_id = query.upper()
            recall_ids = [(memory_id, "working")]
            counts = {"selected": 1, "rows_seen": 1}
            provider._prefetch_call_state.recall_ids = recall_ids
            provider._prefetch_call_state.bank_counts = counts
            # Reproduce the old shared-field race as well as the production
            # per-call data: query B overwrites diagnostics before A caches.
            provider._prefetch_last_recall_ids = recall_ids
            provider._prefetch_last_bank_counts = counts
            if query == "a":
                first_recalled.set()
                self.assertTrue(second_recalled.wait(2))
            else:
                second_recalled.set()
            return f"context {query}"

        provider._prefetch_bank = bank
        provider._prefetch_model_slots = lambda *_args: ""
        provider._prefetch_identity = lambda *_args: ""
        results = {}
        with patch.object(provider_module, "_resolve_profile", return_value=profile):
            first = threading.Thread(target=lambda: results.setdefault("a", provider.prefetch("a")))
            second = threading.Thread(
                target=lambda: results.setdefault("b", provider.prefetch("b"))
            )
            first.start()
            self.assertTrue(first_recalled.wait(2))
            second.start()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, {"a": "context a", "b": "context b"})
        self.assertEqual(provider._prefetch_cache[("a",)][3], [("A", "working")])
        self.assertEqual(provider._prefetch_cache[("b",)][3], [("B", "working")])

    def test_sync_turn_failure_rolls_back_prior_role_write_and_retry_completes(self):
        class FailingBeam:
            def __init__(self):
                self.conn = sqlite3.connect(
                    ":memory:", factory=_TransactionalConnection, check_same_thread=False
                )
                self.conn.execute("CREATE TABLE writes (content TEXT NOT NULL)")
                self.conn.commit()
                self.fail_on_call = 2
                self.calls = 0

            def remember(self, *, content, **_kwargs):
                self.calls += 1
                if self.calls == self.fail_on_call:
                    raise RuntimeError("injected second-role failure")
                self.conn.execute("INSERT INTO writes VALUES (?)", (content,))
                self.conn.commit()
                return "memory-id"

        provider = provider_module.MnemosyneMemoryProvider()
        beam = FailingBeam()
        provider._beam = beam
        provider._agent_context = "primary"
        provider._default_scope = "default"
        provider._sync_roles = {"user", "assistant"}
        provider._auto_sleep_enabled = False
        provider._turn_count = 0

        with patch.dict(sys.modules, _engine_transaction_modules()):
            provider.sync_turn("a sufficiently long user request", "a sufficiently long answer")
        self.assertEqual(beam.conn.execute("SELECT content FROM writes").fetchall(), [])
        diagnostics = provider._sync_turn_diagnostics()
        self.assertEqual(diagnostics["failed"], 1)
        self.assertEqual(diagnostics["completed"], 0)

        beam.fail_on_call = None
        with patch.dict(sys.modules, _engine_transaction_modules()):
            provider.sync_turn("a sufficiently long user request", "a sufficiently long answer")
        self.assertEqual(
            beam.conn.execute("SELECT content FROM writes ORDER BY rowid").fetchall(),
            [
                ("[USER] a sufficiently long user request",),
                ("[ASSISTANT] a sufficiently long answer",),
            ],
        )
        diagnostics = provider._sync_turn_diagnostics()
        self.assertEqual(diagnostics["failed"], 1)
        self.assertEqual(diagnostics["completed"], 1)

    def test_concurrent_sync_turns_commit_as_whole_turns(self):
        first_entered = threading.Event()
        release_first = threading.Event()

        class BlockingBeam:
            def __init__(self):
                self.conn = sqlite3.connect(
                    ":memory:", factory=_TransactionalConnection, check_same_thread=False
                )
                self.conn.execute("CREATE TABLE writes (content TEXT NOT NULL)")
                self.conn.commit()

            def remember(self, *, content, **_kwargs):
                if content.startswith("[USER] first question"):
                    first_entered.set()
                    assert release_first.wait(2)
                self.conn.execute("INSERT INTO writes VALUES (?)", (content,))
                self.conn.commit()
                return content

        provider = provider_module.MnemosyneMemoryProvider()
        provider._beam = BlockingBeam()
        provider._agent_context = "primary"
        provider._default_scope = "default"
        provider._sync_roles = {"user", "assistant"}
        provider._auto_sleep_enabled = False
        provider._turn_count = 0
        errors = []

        def sync(question, answer):
            try:
                provider.sync_turn(question, answer)
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=sync, args=("first question", "first answer long enough"))
        second = threading.Thread(
            target=sync, args=("second question", "second answer long enough")
        )
        with patch.dict(sys.modules, _engine_transaction_modules()):
            first.start()
            self.assertTrue(first_entered.wait(2))
            second.start()
            release_first.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        rows = {row[0] for row in provider._beam.conn.execute("SELECT content FROM writes")}
        diagnostics = provider._sync_turn_diagnostics()
        self.assertEqual(diagnostics["completed"], 2, diagnostics)
        self.assertEqual(diagnostics["failed"], 0, diagnostics)
        self.assertEqual(
            rows,
            {
                "[USER] first question",
                "[ASSISTANT] first answer long enough",
                "[USER] second question",
                "[ASSISTANT] second answer long enough",
            },
        )


if __name__ == "__main__":
    unittest.main()
