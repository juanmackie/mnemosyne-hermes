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
import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from lib.storage import PythonMemoryStorage, StorageError, StorageSchemaError
from lib.mnemosyne_client import resolve_db_path
from orchestration.coordinator import MockCoordinator
from orchestration.context_monitor import (
    ContextMetrics, ContextState, LowLatencyContextMonitor,
)
from orchestration.parallel_executor import (
    ExecutionPlan, ParallelExecutor, SubTask, TaskStatus,
)

# The lite store's exact column list, including the migrated content_lower
# column. Anything else is a foreign database (notably the mnemosyne-memory
# engine's 24-column `memories`).
LEGACY_MEMORIES_DDL = (
    "CREATE TABLE memories ("
    "id TEXT PRIMARY KEY, content TEXT NOT NULL, namespace TEXT NOT NULL, "
    "importance INTEGER DEFAULT 5, context TEXT, summary TEXT, keywords TEXT, "
    "created_at REAL DEFAULT 0, access_count INTEGER DEFAULT 0, last_accessed REAL)"
)


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _columns(path):
    return [row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(memories)")]


def test_recall_treats_wildcards_literally():
    with tempfile.TemporaryDirectory() as d:
        s = PythonMemoryStorage(os.path.join(d, "m.db"))
        s.remember("alpha beta", "ns", 5)
        s.remember("100% cotton", "ns", 5)
        s.remember("under_score", "ns", 5)
        assert len(s.recall("%", namespace="ns")) == 1
        assert len(s.recall("_", namespace="ns")) == 1
        assert len(s.recall("100%", namespace="ns")) == 1
        s.close()


def test_recall_matches_like_semantics_on_the_lowercase_copy():
    """recall() searches content_lower with instr(), not content with LIKE.

    The two must agree: LIKE folds ASCII case only, and instr() needs the
    query folded the same way. Non-ASCII must stay case-sensitive (LIKE does
    not fold it), and '%'/'_'/'\\' must stay literal.
    """
    with tempfile.TemporaryDirectory() as d:
        s = PythonMemoryStorage(os.path.join(d, "m.db"))
        rows = ["Caf\u00e9 M\u00dcNCHEN project", "100% cotton", "under_score",
                "back\\slash", "MixedCASE Alpha", "xylophone"]
        # Distinct importance per row: recall orders by (importance, created_at)
        # and rows written in the same clock tick would otherwise tie, making
        # the ID order plan-dependent rather than comparable.
        for i, text in enumerate(rows):
            s.remember(text, "ns", 5 + i)

        def like_ids(query):
            esc = (query.replace("\\", "\\\\")
                        .replace("%", "\\%").replace("_", "\\_"))
            return [r[0] for r in s._conn().execute(
                "SELECT id FROM memories WHERE content LIKE ? ESCAPE '\\' "
                "ORDER BY importance DESC, created_at DESC LIMIT 50",
                (f"%{esc}%",))]

        for query in ["project", "PROJECT", "m\u00fcnchen", "M\u00dcNCHEN", "caf\u00e9",
                      "%", "100%", "_", "snake_case", "back\\slash", "alpha",
                      "ALPHA", "x", "zzz", "xylophone", "XYLOPHONE"]:
            want = like_ids(query)
            got = [m["id"] for m in s.recall(query, namespace="ns", max_results=50)]
            assert got == want, f"query {query!r}: {got} != {want}"
        s.close()


def test_recall_backfills_rows_missing_the_lowercase_copy():
    """A row written by a pre-migration version has content_lower NULL; instr()
    would return NULL for it and silently hide it, so opening the DB backfills.
    """
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "m.db")
        s = PythonMemoryStorage(path)
        s.remember("xylophone keeper", "ns", 5)
        # Same INSERT a pre-content_lower version would have written.
        s._conn().execute(
            "INSERT INTO memories (id, content, namespace, importance, created_at) "
            "VALUES ('stale', 'stale xylophone row', 'ns', 9, 1.0)"
        )
        s._conn().commit()
        s.close()

        reopened = PythonMemoryStorage(path)
        assert reopened._conn().execute(
            "SELECT COUNT(*) FROM memories WHERE content_lower IS NULL"
        ).fetchone()[0] == 0
        ids = [m["id"] for m in reopened.recall("xylophone", namespace="ns")]
        assert "stale" in ids, ids
        reopened.close()


def test_storage_reuses_one_connection_per_thread():
    import threading
    with tempfile.TemporaryDirectory() as d:
        s = PythonMemoryStorage(os.path.join(d, "m.db"))
        s.remember("alpha beta", "ns", 5)
        main_conn = s._conn()
        assert s._conn() is main_conn, "connection must be reused, not reopened"
        seen = {}

        def worker(k):
            try:
                seen[k] = s._conn()
                s.remember(f"worker {k} note", "ns", 5)
            finally:
                s.close()

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
        verifier = PythonMemoryStorage(path)
        try:
            assert verifier.list_memories("ns")[0]["access_count"] == 3
        finally:
            verifier.close()

        # The buffer is capped, so a read-only workload cannot grow it forever.
        s2 = PythonMemoryStorage(path)
        for _ in range(s2.ACCESS_FLUSH_HITS + 10):
            s2.recall("alpha", namespace="ns")
        assert len(s2._pending()) < s2.ACCESS_FLUSH_DISTINCT
        # Recalling one memory forever must still reach the database unwritten-to.
        verifier = PythonMemoryStorage(path)
        try:
            assert verifier.list_memories("ns")[0]["access_count"] > 3
        finally:
            verifier.close()
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


def test_foreign_schemas_are_refused_without_writing():
    """A database that is not a lite store is refused byte-identically.

    This is the regression for the worst bug found: `_ensure_content_lower`
    ALTERed and rewrote EVERY row before validating the file, and because
    Python's sqlite3 autocommits DDL the damage survived the failure. On a live
    engine bank (24-column `memories`) it did not even fail — it adopted the
    bank silently and added a column to it.
    """
    with tempfile.TemporaryDirectory() as d:
        cases = {}

        p = os.path.join(d, "no_namespace.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, created_at REAL)")
        c.execute("INSERT INTO memories VALUES ('a', 'row', 1.0)")
        c.commit(); c.close()
        cases["memories without namespace"] = p

        p = os.path.join(d, "engine_shaped.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, namespace TEXT, "
                  "importance REAL, memory_type TEXT, tags TEXT, embedding BLOB, updated_at REAL)")
        c.execute("INSERT INTO memories (id, content, namespace) VALUES ('b', 'live', 'agent:hermes')")
        c.commit(); c.close()
        cases["engine-shaped with namespace"] = p

        p = os.path.join(d, "newer.db")
        c = sqlite3.connect(p); c.execute("PRAGMA user_version = 99"); c.commit(); c.close()
        cases["newer sentinel"] = p

        p = os.path.join(d, "not_sqlite.db")
        with open(p, "wb") as fh:
            fh.write(b"this file is not a sqlite database, not even close")
        cases["not a sqlite file"] = p

        for label, path in cases.items():
            before, wal_before = _sha(path), os.path.exists(path + "-wal")
            try:
                PythonMemoryStorage(path)
            except StorageSchemaError:
                pass
            else:
                raise AssertionError(f"{label}: foreign database was accepted")
            assert _sha(path) == before, f"{label}: file was modified"
            assert os.path.exists(path + "-wal") == wal_before, f"{label}: WAL sidecar appeared"


def test_migration_rolls_back_atomically():
    """A migration that fails part-way leaves the store exactly as it was."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        c = sqlite3.connect(db)
        c.execute(LEGACY_MEMORIES_DDL)
        c.execute("INSERT INTO memories (id, content, namespace) VALUES ('x', 'Hello World', 'ns')")
        c.commit(); c.close()

        # Fail AFTER the ALTER + backfill by making the index step invalid.
        with mock.patch.object(PythonMemoryStorage, "INDEXES",
                               ["CREATE INDEX IF NOT EXISTS bogus ON memories(no_such_column)"]):
            try:
                PythonMemoryStorage(db)
            except sqlite3.Error:
                pass
            else:
                raise AssertionError("a failing index migration must raise")

        c = sqlite3.connect(db)
        try:
            columns = [row[1] for row in c.execute("PRAGMA table_info(memories)")]
            assert "content_lower" not in columns, f"ALTER survived the rollback: {columns}"
            assert c.execute("PRAGMA user_version").fetchone()[0] == 0, "sentinel survived the rollback"
            assert c.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
            assert c.execute("SELECT content FROM memories").fetchone()[0] == "Hello World"
        finally:
            c.close()


def test_legacy_store_migrates_and_stamps_schema_version():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        c = sqlite3.connect(db)
        c.execute(LEGACY_MEMORIES_DDL)
        c.execute("INSERT INTO memories (id, content, namespace) VALUES ('x', 'Hello World', 'ns')")
        c.commit(); c.close()

        s = PythonMemoryStorage(db)
        assert s.recall("hello", namespace="ns")[0]["id"] == "x"
        s.close()
        c = sqlite3.connect(db)
        try:
            assert c.execute("PRAGMA user_version").fetchone()[0] == PythonMemoryStorage.SCHEMA_VERSION
            assert c.execute("SELECT content_lower FROM memories").fetchone()[0] == "hello world"
        finally:
            c.close()
        # Re-opening a migrated store must stay a no-op, not a re-migration.
        s = PythonMemoryStorage(db); s.close()


def test_wal_stays_bounded_under_sustained_writes():
    """wal_autocheckpoint=0 must not mean an unbounded WAL.

    The counter was stored on self but read from self._local, so the modulo
    never matched and nothing checkpointed until close() (a 281MB WAL next to a
    36KB database was measured).
    """
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "wal.db")
        s = PythonMemoryStorage(db)
        blob = "x" * 4000
        sizes = []
        for i in range(400):
            s.remember(f"{blob}-{i}", "ns", 5)
            if i % 100 == 99:
                wal = db + "-wal"
                sizes.append(os.path.getsize(wal) if os.path.exists(wal) else 0)
        s.close()
        bound = PythonMemoryStorage.WAL_CHECKPOINT_BYTES * 2
        assert max(sizes) <= bound, f"WAL grew to {max(sizes)} bytes (bound {bound})"
        assert len(sizes) == 4, "expected four size samples"
        s2 = PythonMemoryStorage(db)
        try:
            assert s2.count() == 400
        finally:
            s2.close()


def test_concurrent_cold_opens_succeed():
    """16 simultaneous cold opens used to crash at construction (9/16)."""
    import threading
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "race.db")
        barrier = threading.Barrier(16)
        errors, opened = [], []

        def worker(i):
            barrier.wait()
            try:
                s = PythonMemoryStorage(db)
                s.remember(f"memory {i}", "ns", 5)
                s.close()
                opened.append(i)
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"cold-start contention failed: {errors[:3]}"
        assert len(opened) == 16
        s = PythonMemoryStorage(db)
        try:
            assert s.count() == 16
        finally:
            s.close()


def test_dedup_removes_only_exact_duplicates():
    """Maintenance must not delete memories that merely share a 50-char prefix."""
    with tempfile.TemporaryDirectory() as d:
        s = PythonMemoryStorage(os.path.join(d, "dedup.db"))
        prefix = "Q3 revenue was 12% and the pipeline looked healthy across every region in EMEA "
        s.remember(prefix + "north", "ns", 5)
        s.remember(prefix + "south", "ns", 7)
        s.remember("identical text", "ns", 3)
        s.remember("identical text", "ns", 9)
        s.remember("identical text", "other", 5)

        preview = s.consolidate()
        assert preview["duplicate_groups"] >= 1, "prefix twins should be proposed"
        assert preview["exact_duplicate_groups"] == 1
        assert preview["removed"] == 0, "a dry run must not delete"
        assert preview["candidates"], "a dry run must show what would go"

        result = s.consolidate(auto_apply=True)
        assert result["removed"] == 1, result
        rows = s.list_memories(limit=50)
        kept = {(m["content"], m["namespace"]) for m in rows}
        assert (prefix + "north", "ns") in kept and (prefix + "south", "ns") in kept
        assert ("identical text", "other") in kept, "other namespace must be untouched"
        survivor = [m for m in rows if m["content"] == "identical text" and m["namespace"] == "ns"]
        assert len(survivor) == 1 and survivor[0]["importance"] == 9, "keep the most important row"
        s.close()


def test_cli_refuses_missing_store_and_accepts_trailing_db_path():
    """Read commands must not fabricate a database at a mistyped path.

    `mnemosyne-lite list --db-path /typo/x.db` used to create the directory and
    empty store, then report "0 memories".
    """
    spec = importlib.util.spec_from_file_location(
        "mnemosyne_lite_cli_under_test",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "mnemosyne_lite", "cli.py"),
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "sub", "missing.db")
        assert cli.main(["--db-path", missing, "list"]) == 1
        assert not os.path.exists(missing), "a read command created a database"

        db = os.path.join(d, "cli.db")
        assert cli.main(["--db-path", db, "init"]) == 0
        # --db-path AFTER the subcommand must work too (argparse used to reject it).
        assert cli.main(["remember", "--db-path", db, "--content", "hello", "--namespace", "ns"]) == 0
        assert cli.main(["recall", "--db-path", db, "--query", "hello"]) == 0
        s = PythonMemoryStorage(db)
        try:
            assert s.count() == 1
        finally:
            s.close()


def test_refusal_corpus_if_available():
    """Opt-in: run the foreign-schema guard against a real engine-bank corpus.

    CI has no such corpus, so this skips unless the directory exists. On a host
    that has one it is the check that matters: every bank must be refused
    byte-identically. Point it somewhere else with MNEMOSYNE_BANK_CORPUS.

    Measured on the dev host (AFS_166): 137 banks, all 24-column with a
    namespace column, 137/137 refused, 0 bytes changed, 0 WAL sidecars.
    """
    import glob
    import shutil
    base = os.environ.get("MNEMOSYNE_BANK_CORPUS") or os.path.expanduser("~/.mnemosyne")
    if not os.path.isdir(base):
        print(f"skip: no bank corpus at {base} (set MNEMOSYNE_BANK_CORPUS)")
        return
    banks = sorted(glob.glob(os.path.join(base, "*.db")))
    if not banks:
        print(f"skip: {base} contains no *.db files")
        return
    lite_shapes = (
        list(PythonMemoryStorage.MEMORY_COLUMNS),
        list(PythonMemoryStorage.MIGRATED_MEMORY_COLUMNS),
    )
    refused = 0
    with tempfile.TemporaryDirectory() as d:
        for i, src in enumerate(banks):
            dst = os.path.join(d, f"{i:04d}.db")
            shutil.copy2(src, dst)          # never open the original
            before = _sha(dst)
            try:
                PythonMemoryStorage(dst)
            except StorageSchemaError:
                refused += 1
                assert _sha(dst) == before, f"{src} was modified by a refusal"
                assert not os.path.exists(dst + "-wal"), f"{src} left a WAL sidecar"
                continue
            # An accepted file must really be a lite store (a fresh/legacy one).
            assert _columns(dst) in lite_shapes, f"{src} was accepted but is not a lite store"
    print(f"corpus check: {refused}/{len(banks)} banks refused byte-identically")


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
