"""P24 lifecycle compatibility and ownership tests for the Hermes provider."""

import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import uuid
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402
from hermes_memory_provider import cli as cli_mod  # noqa: E402


def test_pinned_host_fixture_hashes_match_source_ledger():
    directory = ROOT / "tests/fixtures/hermes_main_a28a5d03"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["files"].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == entry["sha256"], name


class NativeMirrorBeam:
    def __init__(self, db_path=":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.session_id = "engine-profile"
        self.db_path = db_path
        self.invalidations = []
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS test_native_rows "
            "(id TEXT PRIMARY KEY, content TEXT, source TEXT, metadata TEXT, scope TEXT, store TEXT)"
        )
        self.conn.commit()

    @property
    def rows(self):
        return {
            row[0]: {
                "content": row[1],
                "source": row[2],
                "metadata": json.loads(row[3]),
                "scope": row[4],
                "store": row[5],
            }
            for row in self.conn.execute(
                "SELECT id, content, source, metadata, scope, store FROM test_native_rows WHERE store='working'"
            )
        }

    def remember(self, *, content, source, metadata=None, **kwargs):
        if getattr(self, "dedupe_identical", False):
            row = self.conn.execute(
                "SELECT id FROM test_native_rows WHERE content=? AND scope=? AND store='working'",
                (content, kwargs.get("scope")),
            ).fetchone()
            if row:
                return row[0]
        memory_id = f"id-{uuid.uuid4().hex}"
        self.conn.execute(
            "INSERT INTO test_native_rows VALUES (?, ?, ?, ?, ?, 'working')",
            (memory_id, content, source, json.dumps(metadata or {}), kwargs.get("scope")),
        )
        self.conn.commit()
        return memory_id

    def forget_working(self, memory_id):
        cursor = self.conn.execute(
            "DELETE FROM test_native_rows WHERE id=? AND store='working'", (memory_id,)
        )
        removed = cursor.rowcount > 0
        self.conn.commit()
        return removed

    def invalidate(self, memory_id, replacement_id=None):
        self.invalidations.append((memory_id, replacement_id))
        cursor = self.conn.execute("DELETE FROM test_native_rows WHERE id=?", (memory_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def get(self, memory_id):
        row = self.conn.execute(
            "SELECT id, content, source, store FROM test_native_rows WHERE id=?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "content": row[1], "source": row[2], "memory_store": row[3]}

    def move_to_episodic(self, memory_id):
        self.conn.execute("UPDATE test_native_rows SET store='episodic' WHERE id=?", (memory_id,))
        self.conn.commit()

    @property
    def episodic_rows(self):
        return {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT id, content FROM test_native_rows WHERE store='episodic'"
            )
        }


def _provider(beam=None):
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._beam = beam or NativeMirrorBeam()
    provider._session_id = "engine-profile"
    provider._current_session_id = "hermes-session-a"
    provider._agent_identity = "startup-user-must-not-be-current-speaker"
    provider._sync_roles = {"user", "assistant", "tool"}
    provider._skip_contexts = set()
    provider._auto_sleep_enabled = False
    provider._turn_count = 0
    return provider


def test_native_add_replace_remove_retires_only_owned_entry():
    provider = _provider()
    provider.on_memory_write("add", "memory", "old fact", {"session_id": "hs-1"})
    assert len(provider._beam.rows) == 1

    provider.on_memory_write(
        "replace",
        "memory",
        "new fact",
        {"session_id": "hs-1", "previous_content": "old fact"},
    )
    assert [row["content"].endswith("new fact") for row in provider._beam.rows.values()] == [True]

    provider.on_memory_write(
        "remove",
        "memory",
        "",
        {"session_id": "hs-1", "previous_content": "new fact"},
    )
    assert provider._beam.rows == {}


def test_native_entry_id_allows_exact_cross_session_retirement():
    provider = _provider()
    provider.on_memory_write(
        "add", "user", "profile fact", {"session_id": "hs-1", "entry_id": "native-7"}
    )
    provider.on_memory_write("remove", "user", "", {"session_id": "hs-2", "entry_id": "native-7"})
    assert provider._beam.rows == {}


def test_native_replace_and_remove_work_with_entry_id_without_previous_content():
    provider = _provider()
    provider.on_memory_write("add", "user", "old version", {"entry_id": "entry-42"})
    provider.on_memory_write("replace", "user", "new version", {"entry_id": "entry-42"})
    assert [row["content"].endswith("new version") for row in provider._beam.rows.values()] == [
        True
    ]
    assert provider._beam.invalidations[-1][1] == next(iter(provider._beam.rows))

    provider.on_memory_write("remove", "user", "", {"entry_id": "entry-42"})
    assert provider._beam.rows == {}


def test_native_mapping_owner_separates_profiles_sharing_database():
    with tempfile.TemporaryDirectory() as tmp:
        database = str(pathlib.Path(tmp) / "shared.db")
        owner_a_beam = NativeMirrorBeam(database)
        owner_a = _provider(owner_a_beam)
        owner_a._agent_identity = "persona-a"
        owner_a.on_memory_write(
            "add", "user", "same native profile text", {"entry_id": "shared-entry"}
        )
        owner_a_beam.conn.close()

        owner_b_beam = NativeMirrorBeam(database)
        owner_b = _provider(owner_b_beam)
        owner_b._agent_identity = "persona-b"
        owner_b.on_memory_write(
            "remove",
            "user",
            "",
            {"entry_id": "shared-entry", "previous_content": "same native profile text"},
        )
        assert len(owner_b_beam.rows) == 1
        owner_b_beam.conn.close()


def test_duplicate_native_entries_share_engine_row_until_last_association_retires():
    beam = NativeMirrorBeam()
    beam.dedupe_identical = True
    provider = _provider(beam)
    provider.on_memory_write("add", "user", "same profile fact", {"entry_id": "native-a"})
    provider.on_memory_write("add", "user", "same profile fact", {"entry_id": "native-b"})
    assert len(beam.rows) == 1
    mapping_count = beam.conn.execute(
        "SELECT COUNT(*) FROM mnemosyne_hermes_native_mirror_mappings"
    ).fetchone()[0]
    assert mapping_count == 2

    provider.on_memory_write(
        "remove", "user", "", {"entry_id": "native-a", "previous_content": "same profile fact"}
    )
    assert len(beam.rows) == 1
    assert beam.invalidations == []
    provider.on_memory_write(
        "remove", "user", "", {"entry_id": "native-b", "previous_content": "same profile fact"}
    )
    assert beam.rows == {}
    assert len(beam.invalidations) == 1


def test_user_memory_replace_remove_survives_provider_restart_and_new_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        database = pathlib.Path(tmp) / "mnemosyne.db"
        first_beam = NativeMirrorBeam(str(database))
        first = _provider(first_beam)
        first.on_memory_write("add", "user", "old profile fact", {"session_id": "transcript-a"})
        assert len(first_beam.rows) == 1
        first_beam.conn.close()

        restarted_beam = NativeMirrorBeam(str(database))
        restarted_beam.session_id = "engine-session-after-restart"
        restarted = _provider(restarted_beam)
        restarted._current_session_id = "transcript-b"
        restarted.on_memory_write(
            "replace",
            "user",
            "corrected profile fact",
            {"session_id": "transcript-b", "previous_content": "old profile fact"},
        )
        assert [
            row["content"].endswith("corrected profile fact")
            for row in restarted_beam.rows.values()
        ] == [True]
        assert restarted_beam.invalidations[-1][1] == next(iter(restarted_beam.rows))
        restarted._current_session_id = "transcript-c"
        restarted.on_memory_write(
            "remove",
            "user",
            "",
            {"session_id": "transcript-c", "previous_content": "corrected profile fact"},
        )
        assert restarted_beam.rows == {}
        restarted_beam.conn.close()


def test_real_engine_global_mirror_replace_remove_across_provider_restart():
    """Exercise exact global-ID retirement with real BeamMemory visibility."""
    try:
        from mnemosyne.core.beam import BeamMemory  # noqa: F401
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError(
                "MNEMOSYNE_REQUIRE_ENGINE=1 but the engine is not importable"
            ) from None
        pytest.skip("mnemosyne-memory engine is not installed")

    keys = ("MNEMOSYNE_DATA_DIR", "MNEMOSYNE_DB_PATH", "MNEMOSYNE_EMBEDDINGS_OFF", "HERMES_HOME")
    saved = {key: os.environ.get(key) for key in keys}
    open_connections = []
    providers = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        open_connections.append(conn)
        return conn

    tmp_context = tempfile.TemporaryDirectory()
    try:
        with patch.object(sqlite3, "connect", side_effect=tracked_connect):
            tmp = tmp_context.name
            database = str(pathlib.Path(tmp) / "provider.db")
            os.environ["MNEMOSYNE_DATA_DIR"] = str(pathlib.Path(tmp) / "engine-data")
            os.environ["MNEMOSYNE_DB_PATH"] = database
            os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
            os.environ["HERMES_HOME"] = tmp

            def start(session_id):
                current = provider_mod.MnemosyneMemoryProvider()
                current.__dict__["_init_audit_log"] = lambda: None
                current.initialize(
                    session_id=session_id,
                    agent_context="primary",
                    hermes_home=tmp,
                    db_path=database,
                    shared_surface_path=str(pathlib.Path(tmp) / "shared.db"),
                )
                current.__dict__.pop("_init_audit_log", None)
                assert current._beam is not None, current._init_error
                providers.append(current)
                open_connections.append(current._beam.conn)
                return current

            first = start("hermes-transcript-a")
            manual_id = first._beam.remember(
                content="profile fact about cobalt comet",
                source="manual_memory_tool",
                scope="global",
                importance=0.6,
            )
            first.on_memory_write(
                "add", "user", "profile fact about cobalt comet", {"session_id": "transcript-a"}
            )
            first_beam = first._beam
            old_id = first_beam.conn.execute(
                "SELECT engine_memory_id FROM mnemosyne_hermes_native_mirror_mappings WHERE target='user'"
            ).fetchone()[0]
            assert old_id != manual_id
            first_session_id = first._session_id
            first_beam.conn.close()

            restarted = start("hermes-transcript-b")
            assert restarted._session_id != first_session_id
            restarted.on_memory_write(
                "replace",
                "user",
                "corrected profile fact about cobalt comet",
                {
                    "session_id": "transcript-b",
                    "previous_content": "profile fact about cobalt comet",
                },
            )
            assert old_id not in {
                row["id"] for row in restarted._beam.recall("cobalt comet", top_k=20)
            }
            context = restarted.prefetch("cobalt comet", session_id="hermes-transcript-b")
            assert "[HERMES NATIVE USER] profile fact about cobalt comet" not in context
            assert "[HERMES NATIVE USER] corrected profile fact about cobalt comet" in context
            new_id = restarted._beam.conn.execute(
                "SELECT engine_memory_id FROM mnemosyne_hermes_native_mirror_mappings WHERE target='user'"
            ).fetchone()[0]
            assert new_id != old_id
            restarted.on_memory_write(
                "remove",
                "user",
                "",
                {
                    "session_id": "transcript-c",
                    "previous_content": "corrected profile fact about cobalt comet",
                },
            )
            assert new_id not in {
                row["id"] for row in restarted._beam.recall("cobalt comet", top_k=20)
            }
            context_after_remove = restarted.prefetch(
                "cobalt comet", session_id="hermes-transcript-b"
            )
            assert (
                "[HERMES NATIVE USER] corrected profile fact about cobalt comet"
                not in context_after_remove
            )
            assert restarted._beam.get(old_id) is not None
            assert restarted._beam.get(new_id) is not None
            assert restarted._beam.get(manual_id)["source"] == "manual_memory_tool"
            manual_expiry = restarted._beam.conn.execute(
                "SELECT valid_until FROM working_memory WHERE id=? UNION ALL "
                "SELECT valid_until FROM episodic_memory WHERE id=?",
                (manual_id, manual_id),
            ).fetchone()
            assert manual_expiry and manual_expiry[0] is None
            for memory_id in (old_id, new_id):
                expiry = restarted._beam.conn.execute(
                    "SELECT valid_until FROM working_memory WHERE id=? UNION ALL "
                    "SELECT valid_until FROM episodic_memory WHERE id=?",
                    (memory_id, memory_id),
                ).fetchone()
                assert expiry and expiry[0]
    finally:
        for provider in locals().get("providers", []):
            with contextlib.suppress(Exception):
                provider.shutdown()
        for conn in open_connections:
            with contextlib.suppress(Exception):
                conn.close()
        beam_module = sys.modules.get("mnemosyne.core.beam")
        thread_local = getattr(beam_module, "_thread_local", None)
        if thread_local is not None:
            thread_local.conn = None
        tmp_context.cleanup()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_failed_native_replacement_keeps_previous_mirror():
    provider = _provider()
    provider.on_memory_write("add", "memory", "prior fact", {"session_id": "hs-1"})
    with patch.object(provider._beam, "remember", side_effect=RuntimeError("write failed")):
        provider.on_memory_write(
            "replace",
            "memory",
            "replacement fact",
            {"session_id": "hs-1", "previous_content": "prior fact"},
        )
    assert [row["content"].endswith("prior fact") for row in provider._beam.rows.values()] == [True]


def test_consolidated_native_mirror_is_invalidated_by_exact_id():
    provider = _provider()
    provider.on_memory_write("add", "memory", "old fact", {"session_id": "hs-1"})
    memory_id = next(iter(provider._beam.rows))
    provider._beam.move_to_episodic(memory_id)
    provider.on_memory_write(
        "remove", "memory", "", {"session_id": "hs-1", "previous_content": "old fact"}
    )
    assert provider._beam.episodic_rows == {}
    assert provider._beam.invalidations == [(memory_id, None)]


def test_native_previous_content_without_session_mapping_does_not_delete_by_text(caplog):
    provider = _provider()
    provider.on_memory_write("add", "memory", "same text", {"session_id": "hs-1"})
    provider.on_memory_write(
        "remove", "memory", "", {"session_id": "hs-2", "previous_content": "same text"}
    )
    assert [row["content"].endswith("same text") for row in provider._beam.rows.values()] == [True]
    assert "no exact owned mapping" in caplog.text


def test_turn_author_metadata_tracks_each_speaker_without_startup_identity():
    provider = _provider()
    provider.on_turn_start(1, "first", author_id="user-a", author_name="Ada", author_is_bot=False)
    provider.sync_turn(
        "question one",
        "answer one",
        session_id="s1",
        messages=[{"role": "tool", "content": "tool result"}],
    )
    first = next(iter(provider._beam.rows.values()))["metadata"]["turn_author"]
    assert first == {"id": "user-a", "name": "Ada", "is_bot": False}
    tool = next(row for row in provider._beam.rows.values() if row["source"] == "conversation_tool")
    assert tool["metadata"]["turn_author"] == first

    provider.on_turn_start(2, "second", author_id="user-b", author_name="Lin", author_is_bot=False)
    provider.sync_turn("question two", "answer two", session_id="s2", turn_author={"id": "user-c"})
    user_rows = [row for row in provider._beam.rows.values() if row["source"] == "conversation"]
    assert user_rows[-1]["metadata"]["turn_author"] == {"id": "user-c"}
    assert "startup-user-must-not-be-current-speaker" not in json.dumps(provider._beam.rows)


def test_sync_turn_accepts_legacy_call_without_turn_author():
    provider = _provider()
    provider.on_turn_start(1, "message", author_id="current-user")
    provider.sync_turn("question", "", session_id="legacy")
    assert next(iter(provider._beam.rows.values()))["metadata"]["turn_author"] == {
        "id": "current-user"
    }


def test_backup_paths_resolves_external_user_home_store_without_initialization():
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        hermes_home = home / ".hermes"
        database = home / "data" / "mnemosyne.db"
        shared_database = home / "shared" / "surface.db"
        with (
            patch.object(pathlib.Path, "home", return_value=home),
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch.object(cli_mod, "resolve_effective_db_path", return_value=str(database)),
            patch.object(
                provider_mod,
                "read_hermes_config_key",
                side_effect=lambda _home, key: (
                    str(shared_database) if key == "shared_surface_path" else None
                ),
            ),
        ):
            assert provider_mod.MnemosyneMemoryProvider().backup_paths() == [
                str(database.resolve()),
                str(shared_database.resolve()),
            ]


def test_backup_paths_uses_configured_path_before_environment_path():
    try:
        import mnemosyne.hermes_config  # noqa: F401
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            pytest.fail("Strict backup precedence lane requires the engine config reader")
        pytest.skip("Engine config reader is absent in the bare-venv lane")
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        hermes_home = home / ".hermes"
        hermes_home.mkdir()
        configured = home / "configured" / "mnemosyne.db"
        environment = home / "environment" / "mnemosyne.db"
        (hermes_home / "config.yaml").write_text(
            f"memory:\n  mnemosyne:\n    db_path: {configured}\n",
            encoding="utf-8",
        )
        with (
            patch.object(pathlib.Path, "home", return_value=home),
            patch.dict(
                os.environ,
                {"HERMES_HOME": str(hermes_home), "MNEMOSYNE_DB_PATH": str(environment)},
            ),
        ):
            actual = cli_mod.resolve_effective_db_path(str(hermes_home))
            if actual is None:
                print("skip: engine db-path resolver unavailable")
                return
            assert pathlib.Path(actual).resolve() == configured.resolve()
            assert provider_mod.MnemosyneMemoryProvider().backup_paths() == [
                str(configured.resolve())
            ]


def test_backup_paths_excludes_store_inside_hermes_home_or_outside_user_home():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
        home = pathlib.Path(tmp)
        hermes_home = home / ".hermes"
        provider = provider_mod.MnemosyneMemoryProvider()
        with (
            patch.object(pathlib.Path, "home", return_value=home),
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
        ):
            with patch.object(
                cli_mod, "resolve_effective_db_path", return_value=str(hermes_home / "memory.db")
            ):
                assert provider.backup_paths() == []
            with patch.object(
                cli_mod,
                "resolve_effective_db_path",
                return_value=str(pathlib.Path(outside) / "memory.db"),
            ):
                assert provider.backup_paths() == []


def test_hermes_backup_helper_captures_and_restores_live_wal_rows():
    fixture = ROOT / "tests" / "fixtures" / "hermes_main_a28a5d03" / "backup_sqlite.py"
    spec = importlib.util.spec_from_file_location("hermes_main_backup_sqlite_fixture", fixture)
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "live.db"
        snapshot = pathlib.Path(tmp) / "backup.db"
        live = sqlite3.connect(source)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        live.commit()
        live.execute("INSERT INTO evidence VALUES (?)", ("committed while WAL is active",))
        live.commit()
        assert pathlib.Path(str(source) + "-wal").exists()
        assert helper._safe_copy_db(source, snapshot, timeout_seconds=5.0)
        restored = sqlite3.connect(snapshot)
        try:
            assert restored.execute("SELECT value FROM evidence").fetchall() == [
                ("committed while WAL is active",)
            ]
        finally:
            restored.close()
            live.close()


def test_v2_checkpoint_is_complete_durable_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        provider = _provider()
        provider._hermes_home = tmp
        provider._current_session_id = "session-1"
        provider.pre_compress_checkpoint_api_version = 2
        evidence = [
            {"role": "user", "content": "full first message", "source": "gateway"},
            {"role": "assistant", "content": "full second message", "tool_calls": [{"id": "c1"}]},
        ]
        with patch.object(provider_mod.os, "fsync", wraps=os.fsync) as fsync:
            provider.on_pre_compress(evidence, require_checkpoint=True)
            provider.on_pre_compress(evidence, require_checkpoint=True)
        files = list((pathlib.Path(tmp) / "mnemosyne" / "checkpoints").glob("*.json"))
        assert len(files) == 1
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        assert saved["version"] == 2
        assert saved["evidence_messages"] == evidence
        assert (
            saved["evidence_sha256"]
            == hashlib.sha256(
                json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        assert fsync.call_count >= 1


def test_v2_checkpoint_failure_raises_for_host_gate():
    with tempfile.TemporaryDirectory() as tmp:
        provider = _provider()
        provider._hermes_home = tmp
        provider._current_session_id = "session-1"
        provider.pre_compress_checkpoint_api_version = 2
        with patch.object(provider_mod.os, "fsync", side_effect=OSError("disk full")):
            try:
                provider.on_pre_compress(
                    [{"role": "user", "content": "evidence"}], require_checkpoint=True
                )
            except provider_mod.CheckpointError:
                pass
            else:
                raise AssertionError("API-v2 checkpoint failure must propagate")
        directory = pathlib.Path(tmp) / "mnemosyne" / "checkpoints"
        assert list(directory.glob("*.tmp")) == []
        provider.on_pre_compress([{"role": "user", "content": "evidence"}], require_checkpoint=True)
        assert len(list(directory.glob("*.json"))) == 1


def test_pinned_hermes_manager_gate_uses_evidence_and_propagates_failure():
    fixture = ROOT / "tests" / "fixtures" / "hermes_main_a28a5d03" / "memory_manager_checkpoint.py"
    spec = importlib.util.spec_from_file_location("hermes_main_checkpoint_fixture", fixture)
    assert spec and spec.loader
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)

    with tempfile.TemporaryDirectory() as tmp:
        provider = _provider()
        provider._hermes_home = tmp
        provider._current_session_id = "session-main"
        provider.pre_compress_checkpoint_api_version = host.PRE_COMPRESS_CHECKPOINT_API_VERSION
        manager = host.PinnedMemoryManager([provider])
        evidence = [{"role": "user", "content": "normalized full evidence", "speaker_id": "user-1"}]
        assert manager.supports_pre_compress_checkpoint(2)
        manager.on_pre_compress(
            [{"role": "user", "content": "legacy raw transcript"}],
            evidence_messages=evidence,
            require_checkpoint=True,
        )
        manager.on_pre_compress([], evidence_messages=evidence, require_checkpoint=True)
        checkpoints = list((pathlib.Path(tmp) / "mnemosyne" / "checkpoints").glob("*.json"))
        assert len(checkpoints) == 1
        assert (
            json.loads(checkpoints[0].read_text(encoding="utf-8"))["evidence_messages"] == evidence
        )

        with patch.object(provider_mod.os, "fsync", side_effect=OSError("disk full")):
            try:
                manager.on_pre_compress(
                    [],
                    evidence_messages=[{"role": "user", "content": "new evidence"}],
                    require_checkpoint=True,
                )
            except provider_mod.CheckpointError:
                pass
            else:
                raise AssertionError("pinned Hermes strict checkpoint gate must propagate failure")


def test_setup_schema_keeps_advanced_fields_out_of_wizard():
    keys = [item["key"] for item in provider_mod.MnemosyneMemoryProvider().get_config_schema()]
    assert keys == ["db_path", "profile_isolation", "default_scope", "tools"]
    assert "sync_roles" not in keys
    assert "require_checkpoint" not in keys


def test_identity_signature_reads_config_without_initializing_database():
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._beam = None
    with (
        patch.object(
            provider_mod,
            "read_hermes_config_key",
            side_effect=lambda home, key: {
                "default_scope": "global",
                "tools": ["mnemosyne_recall"],
            }.get(key),
        ),
        patch.object(cli_mod, "resolve_effective_db_path", return_value="/tmp/mnemosyne-a.db"),
        patch.object(
            provider_mod, "_get_beam_class", side_effect=AssertionError("must not initialize")
        ),
    ):
        first = provider.identity_signature()
    assert first["mnemosyne"]["db_path"] == str(pathlib.Path("/tmp/mnemosyne-a.db").expanduser())
    assert first["mnemosyne"]["default_scope"] == "global"
    with patch.object(cli_mod, "resolve_effective_db_path", return_value="/tmp/mnemosyne-b.db"):
        second = provider.identity_signature()
    assert first != second


def test_recall_status_requires_nonempty_successful_prefetch():
    class Status:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    provider = _provider()
    provider._prefetch_stats["last_error"] = None
    provider._prefetch_last_context = "## Mnemosyne Context\n  [fact] useful"
    provider._prefetch_last_bank_counts = {"selected": 1}
    with patch.object(provider_mod, "_HERMES_RECALL_STATUS", Status):
        status = provider.recall_status()
        assert isinstance(status, Status)
        assert (status.provider_label, status.count, status.glyph) == ("Mnemosyne", 1, "🧠")

        provider._prefetch_last_context = ""
        assert provider.recall_status() is None
        provider._prefetch_last_context = "## Mnemosyne Context\nidentity"
        provider._prefetch_stats["last_error"] = "bank: StorageError"
        assert provider.recall_status() is None
