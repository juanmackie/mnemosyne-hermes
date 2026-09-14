"""Regression checks for the Python-runtime hardening fixes.

Runs two ways:

    python3 tests/test_python_hardening.py     # plain asserts, no pytest needed
    pytest tests/test_python_hardening.py      # if pytest is available

Covers the bugs that were fixed, each of which used to silently misbehave:
  * recall() treating '%'/'_' in a query as LIKE wildcards
  * DATABASE_URL being used verbatim as a SQLite file path
  * the context monitor re-firing preservation ~100x/second
  * the parallel executor hanging forever on unmet dependencies
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from lib.storage import PythonMemoryStorage
from lib.mnemosyne_client import resolve_db_path
from orchestration.coordinator import MockCoordinator
from orchestration.context_monitor import (
    ContextMetrics, ContextState, LowLatencyContextMonitor,
)
from orchestration.parallel_executor import (
    ExecutionPlan, ParallelExecutor, SubTask, TaskStatus,
)


def test_recall_treats_wildcards_literally():
    with tempfile.TemporaryDirectory() as d:
        s = PythonMemoryStorage(os.path.join(d, "m.db"))
        s.remember("alpha beta", "ns", 5)
        s.remember("100% cotton", "ns", 5)
        s.remember("under_score", "ns", 5)
        assert len(s.recall("%", namespace="ns")) == 1
        assert len(s.recall("_", namespace="ns")) == 1
        assert len(s.recall("100%", namespace="ns")) == 1


def test_storage_reuses_one_connection_per_thread():
    import threading
    with tempfile.TemporaryDirectory() as d:
        s = PythonMemoryStorage(os.path.join(d, "m.db"))
        s.remember("alpha beta", "ns", 5)
        main_conn = s._conn()
        assert s._conn() is main_conn, "connection must be reused, not reopened"
        seen = {}

        def worker(k):
            seen[k] = s._conn()
            s.remember(f"worker {k} note", "ns", 5)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert seen[0] is not seen[1], "connections must not be shared across threads"
        assert seen[0] is not main_conn
        assert s.count("ns") == 3
        s.close()


def test_buffered_access_counts_are_exact_and_flushed():
    """recall() defers access counts; they must still land, exactly."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "m.db")
        s = PythonMemoryStorage(path)
        s.remember("alpha beta", "ns", 5)

        s.recall("alpha", namespace="ns")
        s.recall("alpha", namespace="ns")
        # Two hits of one memory inside one buffer must count twice, not once.
        assert s.list_memories("ns")[0]["access_count"] == 2

        s.recall("alpha", namespace="ns")
        s.close()  # close() must flush rather than drop
        assert PythonMemoryStorage(path).list_memories("ns")[0]["access_count"] == 3

        # The buffer is capped, so a read-only workload cannot grow it forever.
        s2 = PythonMemoryStorage(path)
        for _ in range(s2.ACCESS_FLUSH_HITS + 10):
            s2.recall("alpha", namespace="ns")
        assert len(s2._pending()) < s2.ACCESS_FLUSH_DISTINCT
        # Recalling one memory forever must still reach the database unwritten-to.
        assert PythonMemoryStorage(path).list_memories("ns")[0]["access_count"] > 3
        s2.close()


def test_resolve_db_path_rejects_non_sqlite():
    old = os.environ.get("DATABASE_URL")
    try:
        os.environ.pop("DATABASE_URL", None)
        assert resolve_db_path("/explicit.db") == "/explicit.db"
        os.environ["DATABASE_URL"] = "sqlite:///tmp/x.db"
        assert resolve_db_path() == "/tmp/x.db"
        os.environ["DATABASE_URL"] = "/plain.db"
        assert resolve_db_path() == "/plain.db"
        os.environ["DATABASE_URL"] = "postgres://host/db"
        try:
            resolve_db_path()
        except ValueError:
            pass
        else:
            raise AssertionError("non-sqlite DATABASE_URL must be rejected")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def test_context_monitor_edge_triggers():
    mon = LowLatencyContextMonitor(
        coordinator=MockCoordinator(),
        preservation_threshold=0.75, critical_threshold=0.90,
    )
    fired = {"pres": 0, "crit": 0}
    mon.set_preservation_callback(lambda m: fired.__setitem__("pres", fired["pres"] + 1))
    mon.set_critical_callback(lambda m: fired.__setitem__("crit", fired["crit"] + 1))

    def metrics(u):
        state = (ContextState.SAFE if u < 0.5 else ContextState.MODERATE if u < 0.75
                 else ContextState.HIGH if u < 0.90 else ContextState.CRITICAL)
        return ContextMetrics(u, 100, int(u * 100), 100 - int(u * 100), state, 0.0, 1, 0, 0)

    async def drive():
        for u in (0.5, 0.8, 0.8, 0.8, 0.85, 0.95, 0.95, 0.6, 0.8):
            await mon._check_thresholds(metrics(u))
    asyncio.run(drive())
    # crossed into preservation twice (0.8, and again after dropping to 0.6),
    # critical once at 0.95 -- not once per poll.
    assert fired == {"pres": 2, "crit": 1}, fired


def test_parallel_executor_does_not_hang_on_unmet_dep():
    async def run():
        ex = ParallelExecutor(MockCoordinator(), storage=None)
        plan = ExecutionPlan(
            tasks={"x": SubTask("x", "X", depends_on=["missing"])},
            critical_path=["x"],
        )
        try:
            await asyncio.wait_for(ex.execute(plan), timeout=3)
        except RuntimeError:
            return
        raise AssertionError("unmet dependency should fail, not hang")

    asyncio.run(run())


def test_parallel_executor_reusable():
    async def run():
        ex = ParallelExecutor(MockCoordinator(), storage=None)
        plan = ExecutionPlan(
            tasks={
                "a": SubTask("a", "A"),
                "b": SubTask("b", "B", depends_on=["a"]),
            },
            critical_path=["a", "b"],
        )
        first = await ex.execute(plan)
        second = await ex.execute(plan)
        assert first["completed"] == second["completed"] == 2

    asyncio.run(run())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
