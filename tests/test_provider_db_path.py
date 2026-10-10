"""T3/T6/T7/T8 unit tests for the vendored Hermes provider.

Engine-free: the provider is imported from the repo and BeamMemory is replaced
by a fake, so this runs in a bare venv. Run with:

    python tests/test_provider_db_path.py
    pytest tests/test_provider_db_path.py
"""

import base64
import contextvars
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
from typing import Any
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402
from hermes_memory_provider import cli as cli_mod  # noqa: E402


def _module_attr(module: Any, name: str) -> Any:
    """Resolve optional vendored attributes without inventing static stubs."""
    value = getattr(module, name, None)
    assert value is not None, f"missing runtime API {name}"
    return value


class FakeBeam:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.db_path = kwargs.get("db_path")
        for key, value in kwargs.items():
            setattr(self, key, value)
        if not hasattr(self, "canonical_owner_id"):
            self.canonical_owner_id = None
        if not hasattr(self, "agent_context"):
            self.agent_context = None

    def close(self):
        pass


def _beam(provider):
    """Provider beam, typed so the checker sees FakeBeam rather than None.

    The vendored provider declares ``self._beam = None`` and assigns the real
    object in ``initialize()``; the annotation on that attribute cannot be
    tightened without another vendored-file patch, so narrow it here.
    """
    beam = provider._beam
    assert isinstance(beam, FakeBeam), beam
    return beam


def _init(tmp, **kwargs):
    """Build a provider with FakeBeam and run initialize().

    MNEMOSYNE_DATA_DIR points at a throwaway dir so the engine's config
    singleton (and its auto-export of env values) never touches the real box.
    """
    provider = provider_mod.MnemosyneMemoryProvider()
    original_data_dir = os.environ.get("MNEMOSYNE_DATA_DIR")
    os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(tmp, "_engine_data")
    # Instance override and scoped module patch avoid leaking shared state.
    provider.__dict__["_init_audit_log"] = lambda: None
    try:
        with patch.object(provider_mod, "_get_beam_class", return_value=FakeBeam):
            provider.initialize(session_id="t3", hermes_home=str(tmp), **kwargs)
    finally:
        provider.__dict__.pop("_init_audit_log", None)
        if original_data_dir is None:
            os.environ.pop("MNEMOSYNE_DATA_DIR", None)
        else:
            os.environ["MNEMOSYNE_DATA_DIR"] = original_data_dir
    return provider


def test_db_path_from_env():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "env", "mnemosyne.db")
        os.environ["MNEMOSYNE_DB_PATH"] = target
        try:
            provider = _init(tmp)
        finally:
            os.environ.pop("MNEMOSYNE_DB_PATH", None)
        assert provider._db_path == target, provider._db_path
        assert _beam(provider).db_path == target
        assert _beam(provider).kwargs["db_path"] == target


def test_db_path_from_hermes_config_beats_env():
    # The provider's safe YAML reader is local; keep this engine-free gate
    # runnable in a bare venv where the optional PyYAML package may be absent.
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("skip: PyYAML unavailable (bare venv)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "config.yaml"
        config.write_text(
            "memory:\n  mnemosyne:\n    db_path: %s\n" % (pathlib.Path(tmp) / "cfg.db"),
            encoding="utf-8",
        )
        os.environ["MNEMOSYNE_DB_PATH"] = str(pathlib.Path(tmp) / "env.db")
        try:
            provider = _init(tmp)
        finally:
            os.environ.pop("MNEMOSYNE_DB_PATH", None)
        assert provider._db_path == str(pathlib.Path(tmp) / "cfg.db")
        assert _beam(provider).kwargs["db_path"] == str(pathlib.Path(tmp) / "cfg.db")


def test_db_path_kwargs_beats_all():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MNEMOSYNE_DB_PATH"] = str(pathlib.Path(tmp) / "env.db")
        try:
            provider = _init(tmp, db_path=str(pathlib.Path(tmp) / "kwarg.db"))
        finally:
            os.environ.pop("MNEMOSYNE_DB_PATH", None)
        assert _beam(provider).kwargs["db_path"] == str(pathlib.Path(tmp) / "kwarg.db")


def test_db_path_unset_passes_resolved_default_to_beam():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.pop("MNEMOSYNE_DB_PATH", None)
        provider = _init(tmp)
        expected = str(pathlib.Path(tmp) / "_engine_data" / "mnemosyne.db")
        assert provider._db_path == expected
        assert _beam(provider).kwargs["db_path"] == expected
        assert _beam(provider).kwargs["seed_config"] is False


def test_schema_declares_db_path():
    schema = provider_mod.MnemosyneMemoryProvider().get_config_schema()
    keys = {entry["key"] for entry in schema}
    assert "db_path" in keys, keys


def test_db_path_wins_over_profile_isolation():
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("skip: PyYAML unavailable (bare venv)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "config.yaml"
        config.write_text("memory:\n  mnemosyne:\n    profile_isolation: true\n", encoding="utf-8")
        os.environ["MNEMOSYNE_DB_PATH"] = str(pathlib.Path(tmp) / "isolated.db")
        try:
            provider = _init(tmp)
        finally:
            os.environ.pop("MNEMOSYNE_DB_PATH", None)
        assert provider._db_path == str(pathlib.Path(tmp) / "isolated.db")
        # db_path wins => the plain BeamMemory branch, not the bank branch.
        assert _beam(provider).kwargs["db_path"] == str(pathlib.Path(tmp) / "isolated.db")


def test_spawn_context_thread_propagates_contextvars():
    value = contextvars.ContextVar("provider-test-value", default="missing")
    token = value.set("inherited")
    result = []
    done = threading.Event()
    try:
        thread = _module_attr(provider_mod, "spawn_context_thread")(
            lambda: (result.append(value.get()), done.set()),
            name="provider-context-test",
        )
        assert done.wait(2), "context thread did not complete"
        thread.join(timeout=0)
        assert result == ["inherited"], result
    finally:
        value.reset(token)


def test_on_session_switch_resets_session_scoped_state_only():
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._session_id = "agent:hermes"
    provider._turn_count = 11
    provider._reflect_calls_this_session = 2

    provider.on_session_switch("new-session", parent_session_id="old-session", reset=True)

    assert provider._current_session_id == "new-session"
    assert provider._turn_count == 0
    assert provider._reflect_calls_this_session == 0
    assert provider._session_id == "agent:hermes", "persistent memory namespace must not rotate"


def test_on_delegation_is_opt_in_and_bounded():
    class Recorder:
        def __init__(self):
            self.calls = []

        def remember(self, **kwargs):
            self.calls.append(kwargs)

    provider = provider_mod.MnemosyneMemoryProvider()
    recorder = Recorder()
    provider._beam = recorder
    provider._sync_roles = {"user"}
    provider.on_delegation("private task", "private result", child_session_id="child")
    assert recorder.calls == [], "delegation capture must be opt-in"

    provider._sync_roles = {"delegation"}
    provider.on_delegation("task " + "t" * 9000, "result " + "r" * 9000, child_session_id="child")
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["source"] == "conversation_delegation"
    assert call["metadata"]["child_session_id"] == "child"
    assert len(call["content"]) <= provider.DELEGATION_MAX_CHARS


class _PausedTransactionBeam:
    """A real SQLite transaction held open by a failing background turn."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE working_memory (content, importance, timestamp, source, session_id)"
        )
        self.session_id = "test-session"
        self.inserted = threading.Event()
        self.release = threading.Event()

    def remember(self, *, content, source, **kwargs):
        self.conn.execute(
            "INSERT INTO working_memory VALUES (?, 0.95, '', 'identity', ?)",
            (content, self.session_id),
        )
        if source == "conversation":
            self.inserted.set()
            if not self.release.wait(5):
                raise RuntimeError("background transaction was not released")
            self.conn.rollback()
            raise sqlite3.OperationalError("injected write failure")
        self.conn.commit()

    def recall(self, *args, **kwargs):
        return [
            {"content": row[0]} for row in self.conn.execute("SELECT content FROM working_memory")
        ]


def _during_failed_sync(operation):
    """Run an actual provider entry point while sync_turn owns a transaction."""
    provider = provider_mod.MnemosyneMemoryProvider()
    beam = _PausedTransactionBeam()
    provider._beam = beam
    provider._auto_sleep_enabled = False
    provider._sync_roles = {"user"}
    started = threading.Event()
    finished = threading.Event()
    results = []
    errors = []

    def call():
        started.set()
        try:
            results.append(operation(provider))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    sync = threading.Thread(target=provider.sync_turn, args=("uncommitted turn", ""), daemon=True)
    caller = threading.Thread(target=call, daemon=True)
    try:
        sync.start()
        assert beam.inserted.wait(5), "sync_turn did not start its transaction"
        caller.start()
        assert started.wait(5), "concurrent provider call did not start"
        # Give an unguarded caller time to read/commit the pending transaction.
        overlapped = finished.wait(0.2)
    finally:
        beam.release.set()
        sync.join(timeout=5)
        if caller.ident is not None:
            caller.join(timeout=5)
    try:
        assert not sync.is_alive() and not caller.is_alive(), "provider access deadlocked"
        assert not errors, errors
        assert not overlapped, (
            f"provider call bypassed the in-flight sync transaction: "
            f"result={results}, durable rows={beam.recall()}"
        )
        assert provider._sync_turn_diagnostics()["failed"] == 1
        return results[0], beam.recall()
    finally:
        beam.conn.close()


def test_recall_tool_waits_for_background_transaction():
    result, rows = _during_failed_sync(
        lambda provider: provider.handle_tool_call("mnemosyne_recall", {"query": "turn"})
    )
    assert json.loads(result)["results"] == [], "recall exposed an uncommitted memory"
    assert rows == []


def test_memory_write_cannot_commit_a_failed_background_turn():
    _, rows = _during_failed_sync(
        lambda provider: provider.on_memory_write("add", "user", "durable mirror")
    )
    assert rows == [{"content": "[HERMES NATIVE USER]\ndurable mirror"}], (
        "mirror committed another turn's transaction"
    )


def test_identity_prefetch_waits_for_background_transaction():
    def prefetch(provider):
        # Isolate always-injected identity context: the bank already has a lock.
        with patch.object(provider, "_prefetch_bank", return_value=""):
            return provider.prefetch("")

    result, rows = _during_failed_sync(prefetch)
    assert result == "", "identity prefetch exposed an uncommitted memory"
    assert rows == []


def test_model_prefetch_waits_for_background_transaction():
    class ModelStore:
        def __init__(self, conn):
            self.conn = conn

        def list(self, owner_id, *, category):
            if category != "model:user":
                return []
            return [
                {"category": category, "name": "test", "body": row[0], "confidence": 1.0}
                for row in self.conn.execute("SELECT content FROM working_memory")
            ]

    def prefetch(provider):
        provider._beam.canonical = ModelStore(provider._beam.conn)
        with patch.object(provider, "_prefetch_bank", return_value=""):
            return provider.prefetch("uncommitted")

    result, rows = _during_failed_sync(prefetch)
    assert result == "", "model prefetch exposed an uncommitted memory"
    assert rows == []


def test_diagnostic_tool_can_reenter_the_beam_lock():
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._beam = types.SimpleNamespace(db_path=None)
    provider._read_config_key = lambda key: ["*"] if key == "tools" else None
    diagnostic_module = types.SimpleNamespace(run_diagnostics=lambda **kwargs: {"status": "ok"})
    results = []
    with patch.dict(sys.modules, {"mnemosyne.diagnose": diagnostic_module}):
        caller = threading.Thread(
            target=lambda: results.append(provider.handle_tool_call("mnemosyne_diagnose", {})),
            daemon=True,
        )
        caller.start()
        caller.join(timeout=5)
    assert not caller.is_alive(), "diagnostic tool deadlocked while reacquiring the Beam lock"
    payload = json.loads(results[0])
    assert payload["status"] == "ok", payload
    assert payload["sync_turn"]["in_flight"] == 0


class _SessionScopedBeam:
    """Real SQLite rows behind the engine's `get`/`update_working` predicates."""

    def __init__(self, session_id="hermes_a"):
        self.session_id = session_id
        self.conn = sqlite3.connect(":memory:")
        for table in ("working_memory", "episodic_memory"):
            self.conn.execute(
                f"CREATE TABLE {table} (id TEXT PRIMARY KEY, content TEXT, "
                "importance REAL, session_id TEXT, scope TEXT)"
            )
        self.refreshed = []
        self.cache_invalidations = 0

    def add(self, table, memory_id, session_id, scope):
        self.conn.execute(
            f"INSERT INTO {table} VALUES (?, 'old', 0.5, ?, ?)", (memory_id, session_id, scope)
        )
        self.conn.commit()

    def update_working(self, memory_id, content=None, importance=None):
        # Upstream predicate: working memory only, owning session only.
        cursor = self.conn.execute(
            "UPDATE working_memory SET content = COALESCE(?, content), "
            "importance = COALESCE(?, importance) WHERE id = ? AND session_id = ?",
            (content, importance, memory_id, self.session_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get(self, memory_id):
        for table, store in (("working_memory", "working"), ("episodic_memory", "episodic")):
            row = self.conn.execute(
                f"SELECT content, importance FROM {table} "
                "WHERE id = ? AND (session_id = ? OR scope = 'global')",
                (memory_id, self.session_id),
            ).fetchone()
            if row:
                return {
                    "id": memory_id,
                    "content": row[0],
                    "importance": row[1],
                    "memory_store": store,
                }
        return None

    def _refresh_episodic_embedding(self, memory_id, rowid, new_content):
        self.refreshed.append((memory_id, new_content))

    def _invalidate_query_cache(self):
        self.cache_invalidations += 1


def test_update_resolves_every_row_get_resolves():
    provider = provider_mod.MnemosyneMemoryProvider()
    beam = _SessionScopedBeam()
    provider.__dict__["_beam"] = beam
    # This engine-free test isolates fallback mutations. The engine-backed
    # lane exercises the real scope helpers and runtime toggle.
    provider.__dict__["_visible_memory_clause"] = lambda target, memory_id: (
        "id = ? AND (session_id = ? OR scope = 'global')",
        (memory_id, target.session_id),
    )
    beam.add("working_memory", "own", "hermes_a", "session")
    beam.add("working_memory", "global-other", "hermes_b", "global")
    beam.add("working_memory", "private-other", "hermes_b", "session")
    beam.add("episodic_memory", "episodic", "hermes_a", "session")

    def call(tool, **args):
        return json.loads(getattr(provider, f"_handle_{tool}")(args))

    for memory_id, store in (
        ("own", "working"),
        ("global-other", "working"),
        ("episodic", "episodic"),
    ):
        assert call("get", memory_id=memory_id)["status"] == "ok"
        result = call("update", memory_id=memory_id, content="new", importance=0.9)
        assert result == {"status": "updated", "memory_id": memory_id, "memory_store": store}, (
            result
        )
        memory = call("get", memory_id=memory_id)["memory"]
        assert (memory["content"], memory["importance"]) == ("new", 0.9), memory
    assert beam.refreshed == [("episodic", "new")]
    assert beam.cache_invalidations == 2

    # Another session's private row stays invisible to both paths.
    assert call("get", memory_id="private-other")["status"] == "not_found"
    assert call("update", memory_id="private-other", content="new")["status"] == "not_found"
    row = beam.conn.execute(
        "SELECT content FROM working_memory WHERE id = 'private-other'"
    ).fetchone()
    assert row[0] == "old"
    assert call("update", memory_id="missing", content="new")["status"] == "not_found"
    # Nothing to change is a caller error, not a missing row.
    assert call("update", memory_id="own") == {
        "error": "content or importance is required",
        "memory_id": "own",
    }


def test_on_pre_compress_writes_required_checkpoint_and_returns_context():
    provider = provider_mod.MnemosyneMemoryProvider()
    with tempfile.TemporaryDirectory() as tmp:
        provider._hermes_home = tmp
        provider._current_session_id = "session/checkpoint"
        provider._read_config_key = lambda key: key == "require_checkpoint"
        messages = [
            {"role": "system", "content": "ignore this"},
            {"role": "user", "content": "remember the local test host"},
            {"role": "assistant", "content": "host is frost-01"},
            {"role": "tool", "content": {"private": "not plaintext"}},
        ]
        context = provider.on_pre_compress(messages)
        assert "frost-01" in context, context
        checkpoint_dir = pathlib.Path(tmp) / "mnemosyne" / "checkpoints"
        checkpoints = list(checkpoint_dir.glob("*.json"))
        assert len(checkpoints) == 1, checkpoints
        if os.name != "nt":
            assert checkpoint_dir.stat().st_mode & 0o077 == 0, oct(checkpoint_dir.stat().st_mode)
        payload = json.loads(checkpoints[0].read_text(encoding="utf-8"))
        assert payload["version"] == 1, payload
        assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
        assert "session/checkpoint" not in checkpoints[0].name, (
            "session id must not leak to the filename"
        )


def test_on_pre_compress_required_checkpoint_fails_on_write_error():
    provider = provider_mod.MnemosyneMemoryProvider()
    with tempfile.TemporaryDirectory() as tmp:
        provider._hermes_home = tmp
        provider._current_session_id = "session"
        provider._read_config_key = lambda key: key == "require_checkpoint"
        try:
            provider.on_pre_compress([{"role": "user", "content": "\ud800"}])
        except _module_attr(provider_mod, "CheckpointError"):
            pass
        else:
            raise AssertionError("invalid Unicode escaped the checkpoint error contract")

        oversized_utf8 = "🙂" * 65537
        try:
            provider.on_pre_compress([{"role": "user", "content": oversized_utf8}])
        except _module_attr(provider_mod, "CheckpointError"):
            pass
        else:
            raise AssertionError("checkpoint byte limit did not bound multibyte text")

        not_a_dir = pathlib.Path(tmp) / "occupied"
        not_a_dir.write_text("not a directory", encoding="utf-8")
        provider._hermes_home = str(not_a_dir)
        provider._current_session_id = "session"
        provider._read_config_key = lambda key: key == "require_checkpoint"
        try:
            provider.on_pre_compress([{"role": "user", "content": "fact"}])
        except _module_attr(provider_mod, "CheckpointError"):
            pass
        else:
            raise AssertionError("require_checkpoint did not fail on checkpoint I/O error")


def test_default_tool_surface_is_curated():
    provider = provider_mod.MnemosyneMemoryProvider()
    with patch.object(provider, "_read_config_key", return_value=None):
        names = [schema["name"] for schema in provider.get_tool_schemas()]
    assert len(names) == 4, names
    assert set(names) == {
        "mnemosyne_remember",
        "mnemosyne_recall",
        "mnemosyne_stats",
        "mnemosyne_forget",
    }, names


def test_full_tool_surface_requires_explicit_wildcard():
    provider = provider_mod.MnemosyneMemoryProvider()
    with patch.object(provider, "_read_config_key", return_value=["*"]):
        full_names = [schema["name"] for schema in provider.get_tool_schemas()]
    assert len(full_names) == 40, full_names
    assert "mnemosyne_graph_query" in full_names


def test_tool_surface_allows_explicit_subset_and_rejects_mixed_wildcard():
    provider = provider_mod.MnemosyneMemoryProvider()
    with patch.object(provider, "_read_config_key", return_value=["mnemosyne_recall"]):
        assert [s["name"] for s in provider.get_tool_schemas()] == ["mnemosyne_recall"]
    with patch.object(provider, "_read_config_key", return_value=[]):
        assert provider.get_tool_schemas() == []
    with patch.object(provider, "_read_config_key", return_value=["*", "mnemosyne_recall"]):
        try:
            provider.get_tool_schemas()
        except ValueError as exc:
            assert "only" in str(exc)
        else:
            raise AssertionError("mixed wildcard configuration was accepted")


def test_check_provider_provenance_canonical():
    ok, msg = _module_attr(cli_mod, "check_provider_provenance")(
        PROVIDER_ROOT / "hermes_memory_provider"
    )
    assert ok is True, msg


def test_check_provider_provenance_flags_retired_tree():
    with tempfile.TemporaryDirectory() as tmp:
        retired = pathlib.Path(tmp) / "integrations" / "hermes" / "src" / "mnemosyne_hermes"
        retired.mkdir(parents=True)
        (retired / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
        ok, msg = _module_attr(cli_mod, "check_provider_provenance")(retired)
        assert ok is False
        assert "RETIRED" in msg, msg


def test_check_provider_provenance_accepts_only_unchanged_verified_copy():
    source = PROVIDER_ROOT / "hermes_memory_provider"
    with tempfile.TemporaryDirectory() as tmp:
        copied = pathlib.Path(tmp) / "plugins" / "mnemosyne"
        copied.mkdir(parents=True)
        for item in source.iterdir():
            if item.is_file() and item.name != "PROVENANCE.json":
                shutil.copy2(item, copied / item.name)

        check = _module_attr(cli_mod, "check_provider_provenance")
        ok, msg = check(copied)
        assert ok is False and "PROVENANCE.json" in msg, msg

        def digest(path):
            value = hashlib.sha256(path.read_bytes()).digest()
            return base64.urlsafe_b64encode(value).decode().rstrip("=")

        marker = {
            "format_version": 1,
            "copied_from": str(source),
            "files": {item.name: digest(item) for item in copied.iterdir() if item.is_file()},
        }
        marker_path = copied / "PROVENANCE.json"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        ok, msg = check(copied)
        assert ok is True and "verified copy" in msg, msg

        nested = copied / "nested"
        nested.mkdir()
        (nested / "PROVENANCE.json").write_text("{}", encoding="utf-8")
        ok, msg = check(copied)
        assert ok is False, f"nested provenance file was excluded from the inventory: {msg}"
        shutil.rmtree(nested)

        with (copied / "cli.py").open("a", encoding="utf-8") as output:
            output.write("# changed after installation\n")
        ok, msg = check(copied)
        assert ok is False and "edited after it was installed" in msg, msg
        shutil.copy2(source / "cli.py", copied / "cli.py")

        helper = copied / "hermes_llm_adapter.py"
        with helper.open("a", encoding="utf-8") as output:
            output.write("# changed after installation\n")
        ok, msg = check(copied)
        assert ok is False, f"unhashed provider module was accepted: {msg}"
        shutil.copy2(source / "hermes_llm_adapter.py", helper)

        (copied / "rogue.py").write_text("# unexpected post-install file\n", encoding="utf-8")
        ok, msg = check(copied)
        assert ok is False, f"unexpected provider file was accepted: {msg}"
        (copied / "rogue.py").unlink()

        (copied / "rogue.pyc").write_bytes(b"untracked executable bytecode")
        ok, msg = check(copied)
        assert ok is False, f"top-level executable bytecode was accepted: {msg}"


def test_describe_memory_location_warns_outside_home():
    with tempfile.TemporaryDirectory() as tmp:
        inside = pathlib.Path(tmp) / "mnemosyne" / "data" / "mnemosyne.db"
        lines = _module_attr(cli_mod, "describe_memory_location")(inside, tmp)
        assert not any("WARNING" in line for line in lines), lines

        outside = pathlib.Path(tmp).parent / "elsewhere" / "mnemosyne.db"
        lines = _module_attr(cli_mod, "describe_memory_location")(outside, tmp)
        assert any("WARNING" in line for line in lines), lines


def test_db_writable_fresh_install_parents_missing():
    # P0-2: right after `./install.sh` the data dir does not exist yet, because
    # the installer deliberately creates nothing. doctor must still pass.
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "mnemosyne" / "data" / "mnemosyne.db"
        ok, msg = _module_attr(cli_mod, "_db_writable")(str(target))
        assert ok is True, msg
        assert "will be created" in msg, msg


def test_db_writable_existing_file_and_parent():
    with tempfile.TemporaryDirectory() as tmp:
        direct = pathlib.Path(tmp) / "mnemosyne.db"
        direct.touch()
        ok, msg = _module_attr(cli_mod, "_db_writable")(str(direct))
        assert ok is True and "file" in msg, msg

        nested_ok, nested_msg = _module_attr(cli_mod, "_db_writable")(
            str(pathlib.Path(tmp) / "new.db")
        )
        assert nested_ok is True and "not created yet" in nested_msg, nested_msg


def test_db_writable_unresolved_path_is_failure():
    for bad in (None, ""):
        ok, msg = _module_attr(cli_mod, "_db_writable")(bad)
        assert ok is False, (bad, msg)
        assert "could not be resolved" in msg, msg


if __name__ == "__main__":
    tests = [
        test_db_path_from_env,
        test_db_path_from_hermes_config_beats_env,
        test_db_path_kwargs_beats_all,
        test_db_path_unset_passes_resolved_default_to_beam,
        test_schema_declares_db_path,
        test_db_path_wins_over_profile_isolation,
        test_default_tool_surface_is_curated,
        test_full_tool_surface_requires_explicit_wildcard,
        test_tool_surface_allows_explicit_subset_and_rejects_mixed_wildcard,
        test_on_session_switch_resets_session_scoped_state_only,
        test_on_delegation_is_opt_in_and_bounded,
        test_recall_tool_waits_for_background_transaction,
        test_memory_write_cannot_commit_a_failed_background_turn,
        test_identity_prefetch_waits_for_background_transaction,
        test_model_prefetch_waits_for_background_transaction,
        test_diagnostic_tool_can_reenter_the_beam_lock,
        test_update_resolves_every_row_get_resolves,
        test_on_pre_compress_writes_required_checkpoint_and_returns_context,
        test_on_pre_compress_required_checkpoint_fails_on_write_error,
        test_spawn_context_thread_propagates_contextvars,
        test_check_provider_provenance_canonical,
        test_check_provider_provenance_flags_retired_tree,
        test_check_provider_provenance_accepts_only_unchanged_verified_copy,
        test_describe_memory_location_warns_outside_home,
        test_db_writable_fresh_install_parents_missing,
        test_db_writable_existing_file_and_parent,
        test_db_writable_unresolved_path_is_failure,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
