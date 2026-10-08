"""Provider consolidation lifecycle and foreground-latency regressions.

Deterministic tests use a fake sleep blocked on an Event in the same place that
the engine's optional summarizer can wait on Hermes' model proxy. Engine-backed
tests trace SQL and gate model/embedding work when the audited engine is available.
Slow inference must yield the foreground Beam lock; database phases retain it
with an isolated worker connection and a stable session snapshot.
"""

import contextlib
import inspect
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import types
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402


class _Cursor:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return _Cursor()

    def close(self):
        self.closed = True


class _Beam:
    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id", "session-a")
        self.db_path = kwargs.get("db_path", "temporary.db")
        self.author_id = kwargs.get("author_id", "author-a")
        self.author_type = kwargs.get("author_type", "user")
        self.channel_id = kwargs.get("channel_id", "channel-a")
        self.canonical_owner_id = "owner-a"
        self.agent_context = "primary"
        self.conn = _Connection()
        self.remembered = []
        self.recall_calls = 0
        self.closed = False

    def remember(self, **kwargs):
        self.remembered.append(kwargs)
        return "memory-id"

    def get_working_stats(self):
        return {"total": 100}

    def _count_unconsolidated_before(self, _cutoff):
        return 1

    def recall(self, *_args, **_kwargs):
        self.recall_calls += 1
        return []

    def close(self):
        self.closed = True


def _provider(beam=None):
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._beam = beam or _Beam()
    provider._auto_sleep_threshold = 1
    provider._AUTO_SLEEP_TIMEOUT_SECONDS = 0.02
    provider.SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 0.02
    provider._sync_roles = {"user"}
    provider._turn_count = 9
    return provider


def _isolate_engine_environment(root):
    keys = ("HERMES_HOME", "MNEMOSYNE_DATA_DIR", "MNEMOSYNE_EMBEDDINGS_OFF")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["HERMES_HOME"] = str(root)
    os.environ["MNEMOSYNE_DATA_DIR"] = str(pathlib.Path(root) / "_engine_data")
    os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
    return saved


def _restore_engine_environment(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _GatedSleepBeam(_Beam):
    def __init__(self, *, started, release, constructed_under_foreground_lock, **kwargs):
        super().__init__(**kwargs)
        self.started = started
        self.release = release
        self.constructed_under_foreground_lock = constructed_under_foreground_lock

    def sleep(self, *, db_lock=None):
        self.started.set()
        if db_lock is not None:
            db_lock.acquire()
            db_lock.release()
        if not self.release.wait(5):
            raise AssertionError("test did not release the fake summarizer")


def _install_gated_sleep(monkeypatch, provider, *, release=None):
    started = threading.Event()
    release = release or threading.Event()
    workers = []

    def make_worker(**kwargs):
        lock = provider._ensure_beam_access_lock()
        worker = _GatedSleepBeam(
            started=started,
            release=release,
            constructed_under_foreground_lock=lock._is_owned(),
            **kwargs,
        )
        workers.append(worker)
        return worker

    monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: make_worker)
    return started, release, workers


def test_auto_sleep_does_not_block_sync_prefetch_or_recall_tool(monkeypatch):
    provider = _provider()
    started, release, workers = _install_gated_sleep(monkeypatch, provider)
    sync_duration = []
    sync_thread = threading.Thread(
        target=lambda: _time_call(
            lambda: provider.sync_turn("This user turn should be saved", ""),
            sync_duration,
        ),
        daemon=True,
    )
    try:
        sync_thread.start()
        assert started.wait(2), "auto-sleep worker did not enter fake summarizer"
        sync_thread.join(timeout=1)
        assert not sync_thread.is_alive(), "sync_turn waited for the summarizer"
        assert sync_duration and sync_duration[0] < 0.5, sync_duration

        prefetch_done = threading.Event()
        prefetch_result = []
        prefetch_thread = threading.Thread(
            target=lambda: _call_and_signal(
                lambda: provider.prefetch("What did I decide?"),
                prefetch_result,
                prefetch_done,
            ),
            daemon=True,
        )
        prefetch_thread.start()
        recall_done = threading.Event()
        recall_result = []
        recall_thread = threading.Thread(
            target=lambda: _call_and_signal(
                lambda: provider.handle_tool_call("mnemosyne_recall", {"query": "decision"}),
                recall_result,
                recall_done,
            ),
            daemon=True,
        )
        recall_thread.start()
        assert prefetch_done.wait(0.5), "prefetch blocked behind background consolidation"
        assert recall_done.wait(0.5), "recall tool blocked behind background consolidation"
        assert prefetch_result == [""]
        assert json.loads(recall_result[0])["results"] == []
        assert len(workers) == 1
        assert workers[0].conn is not provider._beam.conn
        assert workers[0].constructed_under_foreground_lock, (
            "worker Beam construction must remain protected while initializing its connection"
        )
    finally:
        release.set()
        sync_thread.join(timeout=2)
        if "prefetch_thread" in locals():
            prefetch_thread.join(timeout=2)
        if "recall_thread" in locals():
            recall_thread.join(timeout=2)
        _join_consolidation(provider)


def test_consolidation_is_single_flight_and_uses_trigger_session_snapshot(monkeypatch):
    original = _Beam(
        session_id="session-a",
        db_path="a.db",
        author_id="author-a",
        author_type="user",
        channel_id="channel-a",
    )
    provider = _provider(original)
    started, release, workers = _install_gated_sleep(monkeypatch, provider)
    try:
        provider._maybe_auto_sleep()
        assert started.wait(2), "auto-sleep worker did not start"
        provider._maybe_auto_sleep()
        assert len(workers) == 1, f"overlapping sleep workers started: {len(workers)}"
        assert provider._reflect_calls_this_session == 1, (
            "a skipped overlapping trigger must not consume reflection budget"
        )

        original.canonical_owner_id = "owner-after-trigger"
        original.agent_context = "cron-after-trigger"
        original.session_id = "session-b"
        worker = workers[0]
        assert worker.session_id == "session-a", worker.session_id
        assert worker.db_path == "a.db", worker.db_path
        assert worker.author_id == "author-a", worker.author_id
        assert worker.author_type == "user", worker.author_type
        assert worker.channel_id == "channel-a", worker.channel_id
        assert worker.canonical_owner_id == "owner-a", worker.canonical_owner_id
        assert worker.agent_context == "primary", worker.agent_context
        assert worker.conn is not original.conn
    finally:
        release.set()
        _join_consolidation(provider)


def test_running_worker_rejects_provider_reinitialization(monkeypatch):
    original = _Beam(session_id="session-before")
    provider = _provider(original)
    started, release, _workers = _install_gated_sleep(monkeypatch, provider)
    try:
        provider._maybe_auto_sleep()
        assert started.wait(2), "auto-sleep worker did not start"
        try:
            provider.initialize("session-after")
        except RuntimeError as exc:
            assert "consolidation" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("initialize accepted a running consolidation worker")
        assert provider._beam is original, "rejected initialize replaced the live Beam"
    finally:
        release.set()
        _join_consolidation(provider)


def test_initialize_holds_worker_admission_through_session_replacement(monkeypatch):
    provider = _provider(_Beam(session_id="session-before"))
    init_entered = threading.Event()
    finish_init = threading.Event()
    worker_started = threading.Event()
    release_worker = threading.Event()
    workers = []

    def replace_session(session_id, **_kwargs):
        provider._beam = _Beam(session_id=session_id)
        init_entered.set()
        if not finish_init.wait(5):
            raise AssertionError("test did not finish initialization")

    def make_worker(**kwargs):
        worker = _GatedSleepBeam(
            started=worker_started,
            release=release_worker,
            constructed_under_foreground_lock=provider._beam_access_lock._is_owned(),
            **kwargs,
        )
        workers.append(worker)
        return worker

    monkeypatch.setattr(provider, "_initialize_session", replace_session)
    monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: make_worker)
    initialize_thread = threading.Thread(
        target=lambda: provider.initialize("session-after"), daemon=True
    )
    admission_thread = None
    try:
        initialize_thread.start()
        assert init_entered.wait(2), "initialize did not enter its guarded replacement"
        admission_thread = threading.Thread(target=provider._maybe_auto_sleep, daemon=True)
        admission_thread.start()
        assert not worker_started.wait(0.1), "worker started during session replacement"
        finish_init.set()
        initialize_thread.join(timeout=2)
        assert not initialize_thread.is_alive(), "initialize failed to release admission"
        assert worker_started.wait(2), "deferred auto-sleep was not admitted after initialize"
        assert len(workers) == 1
        assert workers[0].session_id == "session-after", workers[0].session_id
    finally:
        finish_init.set()
        release_worker.set()
        initialize_thread.join(timeout=2)
        if admission_thread is not None:
            admission_thread.join(timeout=2)
        _join_consolidation(provider)


def test_deferred_cleanup_does_not_unregister_a_newer_host_backend(monkeypatch):
    registry = {"backend": None}
    backend_a = object()
    backend_b = object()
    registry_module = types.ModuleType("mnemosyne.core.llm_backends")
    registry_module.get_host_llm_backend = lambda: registry["backend"]
    registry_module.set_host_llm_backend = lambda backend: registry.update(backend=backend)
    adapter_module = types.ModuleType("hermes_memory_provider.hermes_llm_adapter")
    adapter_module.unregister_hermes_host_llm = lambda: registry.update(backend=None)
    monkeypatch.setitem(sys.modules, "mnemosyne.core.llm_backends", registry_module)
    monkeypatch.setitem(sys.modules, "hermes_memory_provider.hermes_llm_adapter", adapter_module)

    provider_a = _provider()
    provider_a._host_llm_backend = backend_a
    registry["backend"] = backend_a
    started, release, workers = _install_gated_sleep(monkeypatch, provider_a)
    try:
        provider_a._maybe_auto_sleep()
        assert started.wait(2), "provider A consolidation did not start"
        provider_a.shutdown()
        assert provider_a._consolidation_cleanup_pending, (
            "shutdown should defer backend cleanup while its worker is running"
        )
        registry["backend"] = backend_b
    finally:
        release.set()
        _join_consolidation(provider_a)

    assert registry["backend"] is backend_b, "provider A cleared provider B's backend"
    assert provider_a._host_llm_backend is None
    assert workers[0].conn.closed


def test_skip_context_backend_cleanup_respects_registration_ownership(monkeypatch):
    """Skipped children borrow a live registration; owners retain cleanup duty."""
    with tempfile.TemporaryDirectory() as temporary:
        saved_env = _isolate_engine_environment(temporary)
        registry = {"backend": None}
        borrowed_backend = object()
        skipped_backend = object()
        primary_backend = object()
        registrations = iter((skipped_backend, primary_backend))
        unregistered = []

        registry_module = types.ModuleType("mnemosyne.core.llm_backends")
        registry_module.get_host_llm_backend = lambda: registry["backend"]
        registry_module.set_host_llm_backend = lambda backend: registry.update(backend=backend)
        adapter_module = types.ModuleType("hermes_memory_provider.hermes_llm_adapter")

        def register():
            backend = next(registrations)
            registry["backend"] = backend
            return True

        def unregister():
            unregistered.append(registry["backend"])
            registry["backend"] = None

        adapter_module.register_hermes_host_llm = register
        adapter_module.unregister_hermes_host_llm = unregister
        monkeypatch.setitem(sys.modules, "mnemosyne.core.llm_backends", registry_module)
        monkeypatch.setitem(
            sys.modules, "hermes_memory_provider.hermes_llm_adapter", adapter_module
        )

        def new_provider():
            provider = provider_mod.MnemosyneMemoryProvider()
            provider._apply_provider_config = lambda _kwargs: None
            provider._canonical_owner = lambda: "test-owner"
            return provider

        monkeypatch.setattr(
            provider_mod,
            "_get_beam_class",
            lambda: lambda **kwargs: _Beam(db_path="", **kwargs),
        )
        try:
            # A skipped instance with no registration of its own must not
            # unregister a primary provider's process-wide backend.
            registry["backend"] = borrowed_backend
            borrower = new_provider()
            borrower.initialize("borrowed-session", agent_context="cron", hermes_home=temporary)
            assert borrower._host_llm_backend is None
            borrower.shutdown()
            assert registry["backend"] is borrowed_backend
            assert unregistered == []

            # A standalone skip-context provider may be the first instance,
            # so it registers the backend and owns its eventual cleanup.
            registry["backend"] = None
            standalone = new_provider()
            standalone.initialize("skip-session", agent_context="cron", hermes_home=temporary)
            assert standalone._host_llm_backend is skipped_backend
            standalone.shutdown()
            assert registry["backend"] is None
            assert unregistered == [skipped_backend]

            # Reinitializing the owning primary as a skipped context must
            # preserve the ownership token so shutdown still clears its own
            # backend, while avoiding a duplicate registration.
            registry["backend"] = None
            primary = new_provider()
            primary.initialize("primary-session", agent_context="primary", hermes_home=temporary)
            assert primary._host_llm_backend is primary_backend
            primary.initialize("subagent-session", agent_context="cron", hermes_home=temporary)
            assert primary._host_llm_backend is primary_backend
            assert registry["backend"] is primary_backend
            assert len(unregistered) == 1
            primary.shutdown()
            assert registry["backend"] is None
            assert unregistered == [skipped_backend, primary_backend]
        finally:
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_shared_worker_connection_is_rejected_without_closing_foreground(monkeypatch):
    foreground = _Beam(session_id="shared-session")
    provider = _provider(foreground)
    monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: lambda **_kwargs: foreground)

    provider._maybe_auto_sleep()
    _join_consolidation(provider)

    assert provider._consolidation_running is False
    assert foreground.conn.closed is False, "rejected shared handle was closed"


def test_shutdown_stops_new_consolidation_and_drains_worker(monkeypatch):
    provider = _provider()
    started, release, workers = _install_gated_sleep(monkeypatch, provider)
    try:
        provider._maybe_auto_sleep()
        assert started.wait(2), "auto-sleep worker did not start"
        provider.shutdown()
        before = len(workers)
        provider._maybe_auto_sleep()
        assert len(workers) == before, "shutdown allowed a new consolidation worker"
        assert provider._consolidation_stopping is True
    finally:
        release.set()
        _join_consolidation(provider)


def test_sleep_error_releases_single_flight_state_and_closes_worker(monkeypatch):
    provider = _provider()
    start_events = [threading.Event(), threading.Event()]
    workers = []

    class _FailingBeam(_Beam):
        def sleep(self, *, db_lock=None):
            start_events[len(workers) - 1].set()
            raise RuntimeError("injected summarizer failure")

    def make_worker(**kwargs):
        worker = _FailingBeam(**kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: make_worker)
    provider._maybe_auto_sleep()
    assert start_events[0].wait(2), "failing sleep worker did not start"
    _join_consolidation(provider)
    assert provider._consolidation_running is False
    assert workers[0].conn.closed, "worker connection was not closed after failure"

    # A failed run must not strand the single-flight latch.
    provider._maybe_auto_sleep()
    assert start_events[1].wait(2), "worker did not restart after cleanup"
    assert len(workers) == 2, "consolidation did not recover after worker failure"
    _join_consolidation(provider)


def test_real_engine_foreground_write_survives_gated_worker(monkeypatch):
    """Exercise separate SQLite handles while the background model stage waits."""
    try:
        from mnemosyne.core.beam import BeamMemory
    except ImportError as exc:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("strict engine lane requires mnemosyne-memory") from exc
        print("skip: mnemosyne-memory engine is not importable")
        return

    temporary = tempfile.TemporaryDirectory()
    saved_env = _isolate_engine_environment(temporary.name)
    connections = []
    real_connect = sqlite3.connect
    started = threading.Event()
    release = threading.Event()
    workers = []

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    try:
        with patch.object(sqlite3, "connect", side_effect=tracked_connect):
            db_path = str(pathlib.Path(temporary.name) / "engine.db")

            def make_beam(**kwargs):
                beam = BeamMemory(**kwargs)
                return beam

            foreground = make_beam(session_id="engine-session", db_path=db_path)
            provider = _provider(foreground)
            provider._auto_sleep_enabled = True
            provider._turn_count = 9
            foreground.get_working_stats = lambda: {"total": 100}
            foreground._count_unconsolidated_before = lambda _cutoff: 1

            def make_worker(**kwargs):
                worker = make_beam(**kwargs)
                workers.append(worker)

                def gated_sleep(*, db_lock=None):
                    started.set()
                    if not release.wait(5):
                        raise AssertionError("test did not release fake summarizer")

                worker.sleep = gated_sleep
                return worker

            monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: make_worker)
            provider.sync_turn("I chose the engine-backed memory store", "")
            assert started.wait(2), "real-engine sleep worker did not reach summarizer gate"
            assert workers[0].conn is not foreground.conn, "worker reused the foreground connection"

            # A later turn can still commit and recall its row while the
            # simulated model stage remains unresolved.
            provider.sync_turn("I chose a local SQLite database", "")
            found = foreground.recall("local SQLite database", top_k=5)
            assert any("local SQLite database" in row["content"] for row in found), found
            assert release.is_set() is False
    finally:
        release.set()
        if "provider" in locals():
            _join_consolidation(provider)
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
        beam_module = sys.modules.get("mnemosyne.core.beam")
        thread_local = getattr(beam_module, "_thread_local", None)
        if thread_local is not None:
            thread_local.conn = None
        temporary.cleanup()
        _restore_engine_environment(saved_env)


def test_engine_sql_phases_hold_foreground_lock_but_llm_wait_does_not(monkeypatch):
    """Gate sleep, model-refresh, and degradation calls around real SQL phases."""
    try:
        from mnemosyne.core import local_llm, model_refresh
        from mnemosyne.core.beam import BeamMemory
    except ImportError as exc:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("strict engine lane requires mnemosyne-memory") from exc
        print("skip: mnemosyne-memory engine is not importable")
        return
    if "db_lock" not in inspect.signature(BeamMemory.sleep).parameters:
        message = "installed engine lacks the audited sleep SQL-phase lock API"
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError(message)
        print(f"skip: {message}")
        return

    temporary = tempfile.TemporaryDirectory()
    saved_env = _isolate_engine_environment(temporary.name)
    saved_auto_apply = os.environ.get("MNEMOSYNE_SLEEP_MODEL_REFRESH_AUTO_APPLY")
    os.environ["MNEMOSYNE_SLEEP_MODEL_REFRESH_AUTO_APPLY"] = "false"
    connections = []
    real_connect = sqlite3.connect
    sleep_started = threading.Event()
    sleep_release = threading.Event()
    model_started = threading.Event()
    model_release = threading.Event()
    degradation_started = threading.Event()
    degradation_release = threading.Event()
    summary_states = []
    model_states = []
    embedding_states = []
    sql_states = []
    provider = None
    worker_ref = {}

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    connect_patch = patch.object(sqlite3, "connect", side_effect=tracked_connect)
    connect_patch.start()

    try:

        def make_beam(**kwargs):
            beam = BeamMemory(**kwargs)
            connections.append(beam.conn)
            return beam

        foreground = make_beam(
            session_id="engine-sql-session",
            db_path=str(pathlib.Path(temporary.name) / "engine.db"),
        )
        foreground.remember(
            "The indexed local memory store keeps this decision available.",
            source="conversation",
            importance=0.7,
            scope="session",
        )
        old_episodic = "A detailed archival memory for tiered degradation. " * 12
        episodic_id = foreground.consolidate_to_episodic(
            summary=old_episodic,
            source_wm_ids=[],
            source="preexisting_archive",
            importance=0.7,
            scope="session",
        )
        old_timestamp = "2000-01-01T00:00:00"
        foreground.conn.execute(
            "UPDATE working_memory SET timestamp = ?, consolidated_at = NULL WHERE session_id = ?",
            (old_timestamp, foreground.session_id),
        )
        foreground.conn.execute(
            "UPDATE episodic_memory SET created_at = ?, tier = 1 WHERE id = ?",
            (old_timestamp, episodic_id),
        )
        foreground.conn.commit()
        provider = _provider(foreground)
        beam_module = sys.modules["mnemosyne.core.beam"]
        embedding_module = beam_module._embeddings
        np = embedding_module.np

        def embed(texts):
            conn = getattr(beam_module._thread_local, "conn", None)
            if conn is not None and conn is not foreground.conn:
                embedding_states.append(
                    (conn.in_transaction, provider._beam_access_lock._is_owned())
                )
            return np.ones((len(texts), embedding_module.EMBEDDING_DIM), dtype=np.float32)

        monkeypatch.setattr(embedding_module, "available", lambda: True)
        monkeypatch.setattr(embedding_module, "embed", embed)

        def wait_at_gate(started, release, label):
            conn = worker_ref["beam"].conn
            assert conn.in_transaction is False, f"{label} began inside a SQLite transaction"
            assert provider._beam_access_lock._is_owned() is False, (
                f"{label} held the foreground database lock"
            )
            if not release.wait(5):
                raise AssertionError(f"test did not release the {label} gate")

        summary_call = {"count": 0}

        def summarize(_memories, *, source=""):
            summary_call["count"] += 1
            if summary_call["count"] == 1:
                summary_states.append(
                    (worker_ref["beam"].conn.in_transaction, provider._beam_access_lock._is_owned())
                )
                sleep_started.set()
                wait_at_gate(sleep_started, sleep_release, "sleep summary")
                return f"sleep summary for {source}"
            if summary_call["count"] == 2:
                summary_states.append(
                    (worker_ref["beam"].conn.in_transaction, provider._beam_access_lock._is_owned())
                )
                degradation_started.set()
                wait_at_gate(degradation_started, degradation_release, "degradation summary")
                return "degraded archival memory"
            raise AssertionError(f"unexpected summarizer call {summary_call['count']}")

        def infer_proposals(items):
            conn = worker_ref["beam"].conn
            model_states.append((conn.in_transaction, provider._beam_access_lock._is_owned()))
            model_started.set()
            wait_at_gate(model_started, model_release, "model refresh")
            return [
                {
                    "category": "model:user",
                    "name": "preferred_storage",
                    "body": "SQLite for local memory",
                    "confidence": 0.55,
                    "evidence_ids": [],
                }
            ]

        monkeypatch.setattr(local_llm, "llm_available", lambda: True)
        monkeypatch.setattr(local_llm, "summarize_memories", summarize)
        monkeypatch.setattr(model_refresh, "infer_model_update_proposals", infer_proposals)

        def make_worker(**kwargs):
            worker = make_beam(**kwargs)
            worker_ref["beam"] = worker
            worker.conn.set_trace_callback(
                lambda sql: sql_states.append(
                    (sql, provider._beam_access_lock._is_owned(), worker.conn.in_transaction)
                )
            )
            return worker

        monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: make_worker)
        provider._start_consolidation("session_end")
        assert sleep_started.wait(5), "real sleep never reached its summary gate"

        def foreground_round_trip(marker):
            provider.sync_turn(f"I prefer the {marker} local memory store", "")
            found = foreground.recall(marker, top_k=5)
            assert any(marker in row["content"] for row in found), found

        foreground_round_trip("sleep-phase")
        assert model_started.is_set() is False
        sleep_release.set()
        assert model_started.wait(5), "sleep never reached model-refresh inference"
        foreground_round_trip("model-refresh-phase")
        model_release.set()
        assert degradation_started.wait(5), "sleep never reached tier-1 degradation"
        foreground_round_trip("degradation-phase")
        degradation_release.set()
        _join_consolidation(provider)

        assert summary_call["count"] == 2, summary_call
        assert summary_states == [(False, False), (False, False)], summary_states
        assert model_states == [(False, False)], model_states
        assert embedding_states and all(state == (False, False) for state in embedding_states), (
            embedding_states
        )
        assert sql_states and all(locked for _, locked, _ in sql_states), sql_states
        normalized_sql = [sql.strip().upper() for sql, _, _ in sql_states]
        assert any(sql.startswith("SAVEPOINT DEGRADE_ROW") for sql in normalized_sql), (
            normalized_sql
        )
        assert any(
            sql.startswith("UPDATE EPISODIC_MEMORY SET CONTENT") for sql in normalized_sql
        ), normalized_sql
        assert any(sql.startswith("INSERT INTO WORKING_MEMORY") for sql in normalized_sql), (
            normalized_sql
        )
        assert any("RELEASE DEGRADE_ROW" in sql for sql in normalized_sql), normalized_sql
    finally:
        sleep_release.set()
        model_release.set()
        degradation_release.set()
        if provider is not None:
            _join_consolidation(provider)
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
        beam_module = sys.modules.get("mnemosyne.core.beam")
        thread_local = getattr(beam_module, "_thread_local", None)
        if thread_local is not None:
            thread_local.conn = None
        connect_patch.stop()
        temporary.cleanup()
        _restore_engine_environment(saved_env)
        if saved_auto_apply is None:
            os.environ.pop("MNEMOSYNE_SLEEP_MODEL_REFRESH_AUTO_APPLY", None)
        else:
            os.environ["MNEMOSYNE_SLEEP_MODEL_REFRESH_AUTO_APPLY"] = saved_auto_apply


def test_engine_rejects_stale_summary_after_public_memory_update(monkeypatch):
    """A concurrent edit invalidates a claimed source snapshot and permits retry."""
    try:
        from mnemosyne.core import local_llm
        from mnemosyne.core.beam import BeamMemory
    except ImportError as exc:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("strict engine lane requires mnemosyne-memory") from exc
        print("skip: mnemosyne-memory engine is not importable")
        return
    if "db_lock" not in inspect.signature(BeamMemory.sleep).parameters:
        message = "installed engine lacks the audited sleep SQL-phase lock API"
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError(message)
        print(f"skip: {message}")
        return

    temporary = tempfile.TemporaryDirectory()
    saved_env = _isolate_engine_environment(temporary.name)
    connections = []
    real_connect = sqlite3.connect
    summary_started = threading.Event()
    release_summary = threading.Event()
    summary_calls = []
    provider = None

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    connect_patch = patch.object(sqlite3, "connect", side_effect=tracked_connect)
    connect_patch.start()
    try:
        foreground = BeamMemory(
            session_id="stale-summary-session",
            db_path=str(pathlib.Path(temporary.name) / "engine.db"),
        )
        connections.append(foreground.conn)
        original_content = "The original project choice was a remote memory database."
        updated_content = "The corrected project choice is a local SQLite memory database."
        foreground.remember(
            original_content,
            source="user_note",
            importance=0.8,
            scope="session",
        )
        row = foreground.conn.execute(
            "SELECT id FROM working_memory WHERE session_id = ? AND content = ?",
            (foreground.session_id, original_content),
        ).fetchone()
        assert row is not None, "test source memory was not inserted"
        memory_id = row["id"]
        foreground.conn.execute(
            "UPDATE working_memory SET timestamp = '2000-01-01T00:00:00' WHERE id = ?",
            (memory_id,),
        )
        foreground.conn.commit()

        provider = _provider(foreground)
        provider._read_config_key = lambda key: ["*"] if key == "tools" else None

        def summarize(memories, *, source=""):
            summary_calls.append((list(memories), source))
            if len(summary_calls) == 1:
                summary_started.set()
                if not release_summary.wait(5):
                    raise AssertionError("test did not release stale summary gate")
                return "stale summary of the remote memory database choice"
            return "fresh summary of the corrected SQLite database choice"

        monkeypatch.setattr(local_llm, "llm_available", lambda: True)
        monkeypatch.setattr(local_llm, "summarize_memories", summarize)

        provider._start_consolidation("session_end")
        assert summary_started.wait(5), "sleep did not claim and summarize the source row"
        assert original_content in summary_calls[0][0], summary_calls

        updated = json.loads(
            provider.handle_tool_call(
                "mnemosyne_update",
                {"memory_id": memory_id, "content": updated_content},
            )
        )
        assert updated.get("status") == "updated", updated
        during_update = foreground.conn.execute(
            "SELECT content FROM working_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        assert during_update["content"] == updated_content, during_update
    finally:
        release_summary.set()
        if provider is not None:
            _join_consolidation(provider)

    try:
        stale_rows = foreground.conn.execute(
            "SELECT content FROM episodic_memory WHERE content LIKE '%stale summary%'"
        ).fetchall()
        assert not stale_rows, stale_rows
        source = foreground.conn.execute(
            "SELECT content, consolidated_at, consolidation_claimed_at "
            "FROM working_memory WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert source["content"] == updated_content, source
        assert source["consolidated_at"] is None, (
            "a stale source claim must be released so the edited memory can be retried",
            source,
        )
        assert source["consolidation_claimed_at"] is None, source
        assert any(
            updated_content in memory["content"]
            for memory in foreground.recall("corrected SQLite", top_k=5)
        ), "the edited source must remain recallable"

        provider._start_consolidation("session_end")
        _join_consolidation(provider)
        fresh_rows = foreground.conn.execute(
            "SELECT content FROM episodic_memory WHERE content LIKE '%fresh summary%'"
        ).fetchall()
        assert fresh_rows, "the updated source was not eligible for a later consolidation retry"
        assert any(updated_content in memories for memories, _ in summary_calls[1:]), summary_calls
    finally:
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
        beam_module = sys.modules.get("mnemosyne.core.beam")
        thread_local = getattr(beam_module, "_thread_local", None)
        if thread_local is not None:
            thread_local.conn = None
        connect_patch.stop()
        temporary.cleanup()
        _restore_engine_environment(saved_env)


@pytest.mark.parametrize("mutation", ["edit", "delete"])
@pytest.mark.parametrize("embed_raises", [False, True])
def test_engine_remember_embed_yields_lock_and_rejects_stale_enrichment(
    monkeypatch, mutation, embed_raises
):
    """A mutation during remember's embed wait wins over stale vector/extraction work."""
    try:
        from mnemosyne.core import beam as beam_module
        from mnemosyne.core.beam import BeamMemory
    except ImportError as exc:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("strict engine lane requires mnemosyne-memory") from exc
        print("skip: mnemosyne-memory engine is not importable")
        return
    if "db_lock" not in inspect.signature(BeamMemory.remember).parameters:
        message = "installed engine lacks the audited remember DB-lock API"
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError(message)
        print(f"skip: {message}")
        return

    temporary = tempfile.TemporaryDirectory()
    saved_env = _isolate_engine_environment(temporary.name)
    connections = []
    real_connect = sqlite3.connect
    embed_started = threading.Event()
    release_embed = threading.Event()
    embed_calls = []
    entity_calls = []
    fact_calls = []
    writer_result = []
    writer_errors = []
    provider = None

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    connect_patch = patch.object(sqlite3, "connect", side_effect=tracked_connect)
    connect_patch.start()
    try:
        foreground = BeamMemory(
            session_id=f"remember-embed-{mutation}-{embed_raises}",
            db_path=str(pathlib.Path(temporary.name) / "engine.db"),
        )
        connections.append(foreground.conn)
        provider = _provider(foreground)
        provider._read_config_key = lambda key: ["*"] if key == "tools" else None
        embedding_module = beam_module._embeddings
        np = embedding_module.np
        original_content = "The proposed server region is Saffron Coast."
        edited_content = "The corrected server region is Blue Ridge."

        def gated_embed(texts):
            embed_calls.append(list(texts))
            if len(embed_calls) == 1:
                embed_started.set()
                if not release_embed.wait(5):
                    raise AssertionError("test did not release remember embedding")
                if embed_raises:
                    raise RuntimeError("injected slow embedding failure")
                return np.ones((len(texts), embedding_module.EMBEDDING_DIM), dtype=np.float32)
            return np.full((len(texts), embedding_module.EMBEDDING_DIM), 2.0, dtype=np.float32)

        monkeypatch.setattr(embedding_module, "available", lambda: True)
        monkeypatch.setattr(embedding_module, "embed", gated_embed)
        monkeypatch.setattr(
            beam_module,
            "_extract_and_store_entities",
            lambda *args, **kwargs: entity_calls.append((args, kwargs)),
        )
        original_extract_facts = foreground.extract_and_store_facts

        def count_fact_extraction(*args, **kwargs):
            fact_calls.append((args, kwargs))
            return original_extract_facts(*args, **kwargs)

        foreground.extract_and_store_facts = count_fact_extraction
        lock = provider._ensure_beam_access_lock()

        def write_memory():
            try:
                with lock:
                    writer_result.append(
                        foreground.remember(
                            original_content,
                            source="consolidation_proposal",
                            scope="session",
                            extract_entities=True,
                            db_lock=lock,
                        )
                    )
            except BaseException as exc:
                writer_errors.append(exc)

        writer = threading.Thread(target=write_memory, daemon=True)
        writer.start()
        assert embed_started.wait(5), "remember did not reach the embedding gate"
        assert lock.acquire(timeout=0.5), "remember held the foreground lock during embedding"
        lock.release()

        row = foreground.conn.execute(
            "SELECT id FROM working_memory WHERE content = ?", (original_content,)
        ).fetchone()
        assert row is not None, "remember must commit the proposal before embedding"
        memory_id = row["id"]
        if mutation == "edit":
            updated = json.loads(
                provider.handle_tool_call(
                    "mnemosyne_update",
                    {"memory_id": memory_id, "content": edited_content},
                )
            )
            assert updated.get("status") == "updated", updated
        else:
            deleted = json.loads(
                provider.handle_tool_call("mnemosyne_forget", {"memory_id": memory_id})
            )
            assert deleted.get("status") == "deleted", deleted
    finally:
        release_embed.set()
        if "writer" in locals():
            writer.join(timeout=5)
            assert not writer.is_alive(), "remember worker leaked past test cleanup"

    try:
        assert not writer_errors, writer_errors
        assert writer_result == [memory_id], writer_result
        assert len(embed_calls) == (2 if mutation == "edit" else 1), embed_calls
        assert entity_calls == [], "stale proposal ran entity extraction after its content changed"
        assert fact_calls == [], "stale proposal ran fact extraction after its content changed"
        if mutation == "edit":
            current = foreground.conn.execute(
                "SELECT content FROM working_memory WHERE id = ?", (memory_id,)
            ).fetchone()
            assert current["content"] == edited_content, current
            vector = foreground.conn.execute(
                "SELECT embedding_json FROM memory_embeddings WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            assert vector is not None, "the winning edit's embedding was lost"
            values = json.loads(vector["embedding_json"])
            assert values and set(values) == {2.0}, "stale embedding replaced the updated vector"
        else:
            current = foreground.conn.execute(
                "SELECT 1 FROM working_memory WHERE id = ?", (memory_id,)
            ).fetchone()
            vector = foreground.conn.execute(
                "SELECT 1 FROM memory_embeddings WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            assert current is None, current
            assert vector is None, "deleted proposal received a late vector"
    finally:
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
        thread_local = getattr(beam_module, "_thread_local", None)
        if thread_local is not None:
            thread_local.conn = None
        connect_patch.stop()
        temporary.cleanup()
        _restore_engine_environment(saved_env)


def test_engine_sleep_does_not_invalidate_after_conflict_snapshot_changes(monkeypatch):
    """A conflict result is discarded if either source changed during inference."""
    try:
        from mnemosyne.core import llm_conflict_detector, local_llm, model_refresh
        from mnemosyne.core.beam import BeamMemory
    except ImportError as exc:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("strict engine lane requires mnemosyne-memory") from exc
        print("skip: mnemosyne-memory engine is not importable")
        return
    if "db_lock" not in inspect.signature(BeamMemory.sleep).parameters:
        message = "installed engine lacks the audited sleep SQL-phase lock API"
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError(message)
        print(f"skip: {message}")
        return

    temporary = tempfile.TemporaryDirectory()
    saved_env = _isolate_engine_environment(temporary.name)
    connections = []
    real_connect = sqlite3.connect
    validation_started = threading.Event()
    release_validation = threading.Event()
    foreground_roundtrip = threading.Event()
    provider = None
    workers = []
    older_id = None
    newer_id = None
    updated_content = "The corrected server region is Blue Ridge."

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    connect_patch = patch.object(sqlite3, "connect", side_effect=tracked_connect)
    connect_patch.start()
    try:
        foreground = BeamMemory(
            session_id="conflict-snapshot-session",
            db_path=str(pathlib.Path(temporary.name) / "engine.db"),
        )
        connections.append(foreground.conn)
        older_text = "The server region was Saffron Coast."
        newer_text = "The server region is Saffron Coast."
        older_id = foreground.remember(older_text, source="user_note", scope="session")
        newer_id = foreground.remember(newer_text, source="user_note", scope="session")
        foreground.conn.execute(
            "UPDATE working_memory SET timestamp = CASE id WHEN ? THEN ? ELSE ? END "
            "WHERE id IN (?, ?)",
            (
                older_id,
                "2000-01-01T00:00:00",
                "2000-01-02T00:00:00",
                older_id,
                newer_id,
            ),
        )
        foreground.conn.commit()
        provider = _provider(foreground)
        provider._read_config_key = lambda key: ["*"] if key == "tools" else None
        monkeypatch.setattr(local_llm, "llm_available", lambda: False)
        monkeypatch.setattr(model_refresh, "infer_model_update_proposals", lambda _items: [])
        monkeypatch.setattr(llm_conflict_detector, "LLM_CONFLICT_DETECTION_ENABLED", True)

        def validate_pair(older, newer, *, db_lock=None, **_kwargs):
            assert older == older_text, older
            assert newer == newer_text, newer
            assert db_lock is not None
            db_lock.release()
            try:
                assert db_lock._is_owned() is False
                validation_started.set()
                if not release_validation.wait(5):
                    raise AssertionError("test did not release conflict validation")
            finally:
                db_lock.acquire()
            return True, 0.99, updated_content

        monkeypatch.setattr(llm_conflict_detector, "validate_conflict_pair", validate_pair)

        def make_worker(**kwargs):
            worker = BeamMemory(**kwargs)
            workers.append(worker)
            worker._detect_conflicts = lambda items: [(older_id, newer_id)]
            return worker

        monkeypatch.setattr(provider_mod, "_get_beam_class", lambda: make_worker)
        provider._start_consolidation("session_end")
        assert validation_started.wait(5), "sleep never reached conflict validation"
        assert workers and workers[0].conn is not foreground.conn

        updated = json.loads(
            provider.handle_tool_call(
                "mnemosyne_update", {"memory_id": newer_id, "content": updated_content}
            )
        )
        assert updated.get("status") == "updated", updated
        foreground_roundtrip.set()
    finally:
        release_validation.set()
        if provider is not None:
            _join_consolidation(provider)

    try:
        assert foreground_roundtrip.is_set()
        older = foreground.conn.execute(
            "SELECT content, valid_until, superseded_by FROM working_memory WHERE id = ?",
            (older_id,),
        ).fetchone()
        newer = foreground.conn.execute(
            "SELECT content, valid_until, superseded_by FROM working_memory WHERE id = ?",
            (newer_id,),
        ).fetchone()
        assert older["content"] == older_text, older
        assert older["valid_until"] is None and older["superseded_by"] is None, older
        assert newer["content"] == updated_content, newer
        assert newer["valid_until"] is None and newer["superseded_by"] is None, newer
    finally:
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
        beam_module = sys.modules.get("mnemosyne.core.beam")
        thread_local = getattr(beam_module, "_thread_local", None)
        if thread_local is not None:
            thread_local.conn = None
        connect_patch.stop()
        temporary.cleanup()
        _restore_engine_environment(saved_env)


def _time_call(fn, durations):
    started = time.perf_counter()
    fn()
    durations.append(time.perf_counter() - started)


def _call_and_signal(fn, results, done):
    try:
        results.append(fn())
    finally:
        done.set()


def _join_consolidation(provider):
    thread = getattr(provider, "_consolidation_thread", None)
    if thread is not None:
        thread.join(timeout=2)
        assert not thread.is_alive(), "consolidation worker leaked past test cleanup"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
