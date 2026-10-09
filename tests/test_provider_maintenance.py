"""Lifecycle and privacy regressions for provider consolidation status."""

import json
import pathlib
import sys
import threading
import types
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402


class ForegroundBeam:
    def __init__(self):
        self.session_id = "private-session-id"
        self.db_path = "private-database-path"
        self.author_id = None
        self.author_type = None
        self.channel_id = None
        self.conn = object()
        self.canonical_owner_id = "private-owner"
        self.agent_context = "primary"


class WorkerBeam:
    entered = threading.Event()
    release = threading.Event()
    fail_sleep = False
    fail_close = False

    def __init__(self, **kwargs):
        self.conn = self
        self.closed = False

    def sleep(self, db_lock=None):
        type(self).entered.set()
        assert type(self).release.wait(5), "test did not release consolidation worker"
        if type(self).fail_sleep:
            raise RuntimeError("private model prompt and response")

    def close(self):
        self.closed = True
        if type(self).fail_close:
            raise OSError("private database path")


def _provider():
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._beam = ForegroundBeam()
    provider._reflect_disabled_for_cron = False
    provider._reflect_max_calls_per_session = None
    return provider


def _reset_worker():
    WorkerBeam.entered = threading.Event()
    WorkerBeam.release = threading.Event()
    WorkerBeam.fail_sleep = False
    WorkerBeam.fail_close = False


def test_status_tracks_running_reused_and_success_without_private_data():
    _reset_worker()
    provider = _provider()
    with patch.object(provider_mod, "_get_beam_class", return_value=WorkerBeam):
        thread = provider._start_consolidation("secret trigger text")
        assert thread is not None
        assert WorkerBeam.entered.wait(5), "worker did not reach gated model work"
        second = provider._start_consolidation("auto_sleep")
        snapshot = provider._consolidation_status_snapshot()
        assert second is thread
        assert snapshot["state"] == "running"
        assert snapshot["running"] is True
        assert snapshot["last_trigger"] == "other"
        assert snapshot["reused_triggers"] == 1
        rendered = json.dumps(snapshot)
        for private in ("private-session-id", "private-database-path", "private-owner"):
            assert private not in rendered
        assert "secret trigger text" not in rendered
        assert "model prompt" not in rendered
        WorkerBeam.release.set()
        thread.join(timeout=5)
    assert not thread.is_alive(), "worker failed to drain"
    snapshot = provider._consolidation_status_snapshot()
    assert snapshot["state"] == "succeeded"
    assert snapshot["running"] is False
    assert snapshot["finished_at"] >= snapshot["started_at"]
    assert snapshot["duration_ms"] >= 0
    assert snapshot["error_class"] is None


def test_worker_failure_exposes_only_error_class():
    _reset_worker()
    WorkerBeam.fail_sleep = True
    provider = _provider()
    with patch.object(provider_mod, "_get_beam_class", return_value=WorkerBeam):
        thread = provider._start_consolidation("session_end")
        assert thread is not None
        assert WorkerBeam.entered.wait(5)
        WorkerBeam.release.set()
        thread.join(timeout=5)
    snapshot = provider._consolidation_status_snapshot()
    assert not thread.is_alive()
    assert snapshot["state"] == "failed"
    assert snapshot["error_class"] == "RuntimeError"
    assert "private model prompt and response" not in json.dumps(snapshot)


def test_worker_close_failure_marks_run_failed():
    _reset_worker()
    WorkerBeam.fail_close = True
    provider = _provider()
    with patch.object(provider_mod, "_get_beam_class", return_value=WorkerBeam):
        thread = provider._start_consolidation("session_end")
        assert thread is not None
        assert WorkerBeam.entered.wait(5)
        WorkerBeam.release.set()
        thread.join(timeout=5)
    snapshot = provider._consolidation_status_snapshot()
    assert not thread.is_alive()
    assert snapshot["state"] == "failed"
    assert snapshot["error_class"] == "OSError"
    assert "private database path" not in json.dumps(snapshot)


def test_worker_construction_failure_marks_run_failed():
    provider = _provider()

    class BrokenWorker:
        def __init__(self, **kwargs):
            raise LookupError("private worker config")

    with patch.object(provider_mod, "_get_beam_class", return_value=BrokenWorker):
        thread = provider._start_consolidation("session_end")
        assert thread is not None
        thread.join(timeout=5)
    snapshot = provider._consolidation_status_snapshot()
    assert not thread.is_alive()
    assert snapshot["state"] == "failed"
    assert snapshot["error_class"] == "LookupError"
    assert "private worker config" not in json.dumps(snapshot)


def _capturing_launcher(captured):
    def launch(target, *, name=None, daemon=True):
        def capture_exit():
            try:
                target()
            except BaseException as exc:
                captured.append(type(exc).__name__)

        thread = threading.Thread(target=capture_exit, name=name, daemon=daemon)
        thread.start()
        return thread

    return launch


def test_system_exit_from_sleep_is_failed_and_still_propagates():
    provider = _provider()
    propagated = []

    class ExitingWorker:
        def __init__(self, **kwargs):
            self.conn = self

        def sleep(self, db_lock=None):
            raise SystemExit("private model output")

        def close(self):
            pass

    with (
        patch.object(provider_mod, "_get_beam_class", return_value=ExitingWorker),
        patch.object(
            provider_mod, "spawn_context_thread", side_effect=_capturing_launcher(propagated)
        ),
    ):
        thread = provider._start_consolidation("session_end")
        assert thread is not None
        thread.join(timeout=5)
    snapshot = provider._consolidation_status_snapshot()
    assert not thread.is_alive()
    assert propagated == ["SystemExit"]
    assert snapshot["state"] == "failed"
    assert snapshot["running"] is False
    assert snapshot["error_class"] == "SystemExit"
    assert "private model output" not in json.dumps(snapshot)


def test_system_exit_from_close_is_failed_and_still_propagates():
    provider = _provider()
    propagated = []

    class ExitingCloseWorker:
        def __init__(self, **kwargs):
            self.conn = self

        def sleep(self, db_lock=None):
            pass

        def close(self):
            raise SystemExit("private database detail")

    with (
        patch.object(provider_mod, "_get_beam_class", return_value=ExitingCloseWorker),
        patch.object(
            provider_mod, "spawn_context_thread", side_effect=_capturing_launcher(propagated)
        ),
    ):
        thread = provider._start_consolidation("session_end")
        assert thread is not None
        thread.join(timeout=5)
    snapshot = provider._consolidation_status_snapshot()
    assert not thread.is_alive()
    assert propagated == ["SystemExit"]
    assert snapshot["state"] == "failed"
    assert snapshot["running"] is False
    assert snapshot["error_class"] == "SystemExit"
    assert "private database detail" not in json.dumps(snapshot)


def test_shutdown_timeout_is_visible_while_worker_continues():
    _reset_worker()
    provider = _provider()
    provider.SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 0
    with patch.object(provider_mod, "_get_beam_class", return_value=WorkerBeam):
        thread = provider._start_consolidation("session_end")
        assert thread is not None
        assert WorkerBeam.entered.wait(5)
        provider.shutdown()
        snapshot = provider._consolidation_status_snapshot()
        assert snapshot["state"] == "running"
        assert snapshot["running"] is True
        assert snapshot["stopping"] is True
        assert snapshot["shutdown_timed_out"] is True
        WorkerBeam.release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    snapshot = provider._consolidation_status_snapshot()
    assert snapshot["state"] == "succeeded"
    assert snapshot["shutdown_timed_out"] is True


def test_thread_start_failure_is_recorded():
    provider = _provider()
    with patch.object(provider_mod, "spawn_context_thread", side_effect=OSError("secret path")):
        try:
            provider._start_consolidation("session_end")
        except OSError:
            pass
        else:
            raise AssertionError("thread start failure was swallowed")
    snapshot = provider._consolidation_status_snapshot()
    assert snapshot["state"] == "failed"
    assert snapshot["running"] is False
    assert snapshot["error_class"] == "OSError"
    assert "secret path" not in json.dumps(snapshot)


def test_reinitialize_resets_completed_status_and_refuses_running_worker():
    provider = _provider()
    provider._consolidation_status = {
        "state": "failed",
        "last_trigger": "session_end",
        "started_at": 10.0,
        "finished_at": 12.0,
        "duration_ms": 2000.0,
        "error_class": "RuntimeError",
        "reused_triggers": 3,
        "skipped_triggers": 4,
        "shutdown_timed_out": True,
        "running": False,
        "stopping": False,
    }
    provider.__dict__["_initialize_session"] = lambda *args, **kwargs: None

    provider.initialize("next-session")
    snapshot = provider._consolidation_status_snapshot()
    assert snapshot["state"] == "idle"
    assert snapshot["last_trigger"] is None
    assert snapshot["started_at"] is None
    assert snapshot["finished_at"] is None
    assert snapshot["duration_ms"] is None
    assert snapshot["error_class"] is None
    assert snapshot["reused_triggers"] == 0
    assert snapshot["skipped_triggers"] == 0
    assert snapshot["shutdown_timed_out"] is False
    assert snapshot["stopping"] is False

    provider._consolidation_running = True
    provider._consolidation_status = {**snapshot, "state": "running", "running": True}
    try:
        provider.initialize("must-not-retarget")
    except RuntimeError as exc:
        assert "must finish" in str(exc)
    else:
        raise AssertionError("reinitialize accepted a running worker")
    assert provider._consolidation_status_snapshot()["state"] == "running"


def test_diagnose_exposes_status_without_waiting_for_admission_lock():
    provider = _provider()
    provider._beam.db_path = None
    provider.get_audit_diagnostics = lambda: {"status": "ok"}
    diagnostic_module = types.SimpleNamespace(run_diagnostics=lambda **kwargs: {"status": "ok"})
    foreground_lock = provider._ensure_beam_access_lock()
    admission_waiting = threading.Event()
    provider._ensure_beam_access_lock = lambda: admission_waiting.set() or foreground_lock
    provider._reserve_reflection_budget = lambda trigger: {"status": "skipped"}
    admission = threading.Thread(
        target=lambda: provider._start_consolidation("session_end"), daemon=True
    )
    with patch.dict(sys.modules, {"mnemosyne.diagnose": diagnostic_module}):
        # Admission owns the consolidation lock while waiting for this foreground lock.
        with foreground_lock:
            admission.start()
            assert admission_waiting.wait(2), "admission did not reach foreground-lock wait"
            payload = json.loads(provider._handle_diagnose({}))
            assert payload["consolidation"]["state"] == "idle"
        admission.join(timeout=5)
    assert not admission.is_alive(), "admission failed to leave foreground-lock wait"
