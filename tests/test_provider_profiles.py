"""Profile/store safety regressions, including a fresh real-engine process."""

import os
import pathlib
import subprocess
import sys
import types
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

import hermes_memory_provider as provider_mod  # noqa: E402
from hermes_memory_provider import cli  # noqa: E402
from hermes_memory_provider.evidence import source_reference, turn_metadata  # noqa: E402


def _require_yaml():
    pytest.importorskip("yaml", reason="provider profile config tests require PyYAML")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "environment-home"))
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path / "environment-data"))
    monkeypatch.delenv("MNEMOSYNE_DB_PATH", raising=False)


def test_provider_reads_selected_profile_engine_config_without_singleton(tmp_path):
    _require_yaml()
    home = tmp_path / "profiles" / "work"
    (home / "mnemosyne").mkdir(parents=True)
    (home / "mnemosyne/config.yaml").write_text("default_scope: global\n", encoding="utf-8")
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._hermes_home = str(home)
    singleton = types.ModuleType("mnemosyne.core.config")
    singleton.get_config = lambda: (_ for _ in ()).throw(AssertionError("global config accessed"))
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    with patch.dict(sys.modules, {"mnemosyne.core.config": singleton}):
        assert provider._read_config_key("default_scope") == "global"
        assert provider._read_config_key("absent") is None
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("text", ["memory: [", "memory: []", "memory:\n  mnemosyne: []"])
def test_malformed_profile_config_refuses_before_database_access(tmp_path, text):
    _require_yaml()
    home = tmp_path / "work"
    home.mkdir()
    (home / "config.yaml").write_text(text, encoding="utf-8")
    provider = provider_mod.MnemosyneMemoryProvider()
    with (
        patch.object(provider_mod, "_get_beam_class", side_effect=AssertionError("DB accessed")),
        pytest.raises((ValueError, RuntimeError)),
    ):
        provider.initialize("session", hermes_home=str(home))
    assert provider._beam is None
    assert not list(home.rglob("*.db"))


def test_profile_bank_path_is_shared_by_provider_cli_identity_and_backup(tmp_path):
    _require_yaml()
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "memory:\n  mnemosyne:\n    profile_isolation: true\n", encoding="utf-8"
    )
    expected = pathlib.Path(os.environ["MNEMOSYNE_DATA_DIR"]) / "banks/work/mnemosyne.db"
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._hermes_home = str(home)
    assert pathlib.Path(cli.resolve_effective_db_path(str(home))) == expected
    assert pathlib.Path(provider.identity_signature()["mnemosyne"]["db_path"]) == expected
    with patch.object(pathlib.Path, "home", return_value=tmp_path):
        assert provider.backup_paths() == [str(expected.resolve())]
    assert not expected.parent.exists()


@pytest.mark.parametrize("name", ["Work", "work space", "work.space", "a" * 65])
def test_ambiguous_bank_names_refuse_instead_of_collapsing(tmp_path, name):
    _require_yaml()
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "memory:\n  mnemosyne:\n    profile_isolation: true\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unambiguous"):
        cli.resolve_effective_db_path(str(home))
    provider = provider_mod.MnemosyneMemoryProvider()
    with (
        patch.object(provider_mod, "_get_beam_class", side_effect=AssertionError("DB accessed")),
        pytest.raises(ValueError, match="unambiguous"),
    ):
        provider.initialize("session", hermes_home=str(home))
    assert not list(tmp_path.rglob("*.db"))


def test_explicit_path_beats_isolation_even_with_ambiguous_identity(tmp_path):
    provider = provider_mod.MnemosyneMemoryProvider()
    target = str(tmp_path / "explicit.db")
    provider.initialize(
        "session",
        hermes_home=str(tmp_path / "work"),
        agent_context="subagent",
        agent_identity="unrelated identity",
        profile_isolation=True,
        db_path=target,
    )
    assert provider._db_path == target
    assert provider.identity_signature()["mnemosyne"]["db_path"] == target
    provider.shutdown()


def test_identity_notices_path_changes_while_backup_keeps_active_store(tmp_path):
    _require_yaml()
    home = tmp_path / "work"
    home.mkdir()
    old, new = tmp_path / "old.db", tmp_path / "new.db"
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._hermes_home = str(home)
    provider._beam = types.SimpleNamespace(db_path=str(old))
    (home / "config.yaml").write_text(
        f"memory:\n  mnemosyne:\n    db_path: {new.as_posix()}\n", encoding="utf-8"
    )
    assert pathlib.Path(provider.identity_signature()["mnemosyne"]["db_path"]) == new
    with patch.object(pathlib.Path, "home", return_value=tmp_path):
        assert provider.backup_paths() == [str(old.resolve())]


def test_unreadable_profile_config_fails_visibly_without_fallback(tmp_path):
    _require_yaml()
    home = tmp_path / "work"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("memory: {}", encoding="utf-8")
    provider = provider_mod.MnemosyneMemoryProvider()
    provider._hermes_home = str(home)
    read_text = pathlib.Path.read_text

    def denied(selected, *args, **kwargs):
        if selected == path:
            raise PermissionError("synthetic unreadable profile")
        return read_text(selected, *args, **kwargs)

    with patch.object(pathlib.Path, "read_text", denied):
        with pytest.raises(ValueError, match="Cannot read"):
            provider._read_config_key("default_scope")
        with pytest.raises(ValueError, match="Cannot read"):
            provider.identity_signature()
        with pytest.raises(ValueError, match="Cannot read"):
            provider.backup_paths()


def test_reinitialization_resets_all_profile_settings(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    provider = provider_mod.MnemosyneMemoryProvider()
    # Skip DB startup in both calls; the real public initialization still applies config.
    provider.initialize(
        "a",
        hermes_home=str(first),
        agent_context="subagent",
        default_scope="global",
        shared_surface_path=str(first / "shared.db"),
        shared_surface_read=True,
        ignore_patterns=["secret"],
        sync_roles=["assistant"],
        auto_sleep=False,
        sleep_threshold=17,
        reflect_max_calls_per_session=1,
    )
    provider.initialize("b", hermes_home=str(second), agent_context="subagent")
    assert provider._session_id == "hermes_b"
    assert provider._default_scope == "session"
    assert provider._shared_surface_path is None
    assert provider._shared_surface_read is False
    assert provider._ignore_patterns == []
    assert provider._sync_roles == {"user"}
    assert provider._auto_sleep_enabled is True
    assert provider._auto_sleep_threshold == 50
    assert provider._reflect_max_calls_per_session == 3
    provider.shutdown()


@pytest.mark.parametrize("source", ["kwargs", "config", "environment"])
def test_explicit_db_path_with_isolation_warns_and_path_wins(tmp_path, source, caplog, monkeypatch):
    if source == "config":
        _require_yaml()
    home = tmp_path / "work"
    home.mkdir()
    target = tmp_path / f"{source}.db"
    kwargs = {"profile_isolation": True}
    if source == "kwargs":
        kwargs["db_path"] = str(target)
    elif source == "config":
        (home / "config.yaml").write_text(
            f"memory:\n  mnemosyne:\n    db_path: {target.as_posix()}\n    profile_isolation: true\n",
            encoding="utf-8",
        )
        kwargs.pop("profile_isolation")
    else:
        monkeypatch.setenv("MNEMOSYNE_DB_PATH", str(target))
    provider = provider_mod.MnemosyneMemoryProvider()
    provider.initialize("session", hermes_home=str(home), agent_context="subagent", **kwargs)
    assert provider._db_path == str(target)
    assert "both db_path=" in caplog.text
    assert "db_path wins and profile banks are ignored" in caplog.text


def test_doctor_uses_active_hermes_home_and_read_only_sqlite(tmp_path, capsys):
    _require_yaml()
    import sqlite3

    home = tmp_path / "active"
    home.mkdir()
    database = home / "mnemosyne/data/mnemosyne.db"
    database.parent.mkdir(parents=True)
    (home / "config.yaml").write_text(
        f"memory:\n  mnemosyne:\n    db_path: {database.as_posix()}\n", encoding="utf-8"
    )
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (id TEXT)")
    host = types.ModuleType("hermes_constants")
    host.get_hermes_home = lambda: home
    calls = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    with (
        patch.dict(sys.modules, {"hermes_constants": host}),
        patch.object(sqlite3, "connect", side_effect=tracked_connect),
        pytest.raises(SystemExit),
    ):
        cli.mnemosyne_command(types.SimpleNamespace(mnemosyne_cmd="doctor", no_fix=True))
    output = capsys.readouterr().out
    assert f"HERMES_HOME: {home}" in output
    assert calls and all(kwargs.get("uri") and "mode=ro" in args[0] for args, kwargs in calls)


def test_doctor_race_cannot_create_missing_database(tmp_path):
    import sqlite3

    database = tmp_path / "gone.db"
    database.touch()
    real_connect = sqlite3.connect

    def disappearing_connect(*args, **kwargs):
        database.unlink()
        return real_connect(*args, **kwargs)

    with patch.object(sqlite3, "connect", side_effect=disappearing_connect):
        ok, _ = cli._db_integrity(database)
    assert ok is False
    assert not database.exists()


def test_successful_doctor_no_fix_leaves_store_and_file_inventory_unchanged(tmp_path):
    import sqlite3

    home = tmp_path / "work"
    (home / "plugins/mnemosyne").mkdir(parents=True)
    database = tmp_path / "environment-data/mnemosyne.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (id TEXT)")
    host = types.ModuleType("hermes_constants")
    host.get_hermes_home = lambda: home
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    with (
        patch.dict(sys.modules, {"hermes_constants": host}),
        patch.object(cli, "_provider_registration_count", return_value=(1, "synthetic discovery")),
        patch.object(cli, "check_provider_provenance", return_value=(True, "synthetic provenance")),
    ):
        assert (
            cli.mnemosyne_command(types.SimpleNamespace(mnemosyne_cmd="doctor", no_fix=True)) == 0
        )
    assert {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_source_references_keep_internal_and_platform_namespaces_distinct():
    message = {"role": "user", "content": "fact", "id": "row-7", "message_id": "platform-9"}
    other = {"role": "assistant", "content": "different", "id": "platform-9", "message_id": "row-7"}
    reference = source_reference(turn_metadata("fact", "user", [message, other], "session"))
    assert reference is not None
    assert reference["message_id"] == "platform-9"
    assert reference["message_id_namespace"] == "platform"
    duplicate = {**other, "message_id": "platform-9"}
    assert "source_ref" not in turn_metadata("fact", "user", [message, duplicate], "session")


def test_integer_host_ids_are_normalized_within_their_namespace():
    message = {"role": "user", "content": "fact", "id": 7, "message_id": 9}
    other = {"role": "assistant", "content": "different", "id": 9, "message_id": 7}
    reference = source_reference(turn_metadata("fact", "user", [message, other], "session"))
    assert reference is not None and reference["message_id"] == "9"
    assert reference["message_id_namespace"] == "platform"
    assert "source_ref" not in turn_metadata(
        "fact", "user", [message, {**other, "message_id": "9"}], "session"
    )


@pytest.mark.parametrize("data_override", [True, False])
def test_real_engine_fresh_process_opens_only_selected_profile_stores(tmp_path, data_override):
    if provider_mod._ENGINE_IMPORT_ERROR is not None:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            pytest.fail("Real-engine profile smoke requires the installed audited engine")
        pytest.skip("engine not installed")
    # No engine globals or constructors are mocked. Environment defaults differ
    # from two active profiles, and reinitialization uses the same provider.
    script = r"""
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from hermes_memory_provider import MnemosyneMemoryProvider
from hermes_memory_provider.cli import resolve_effective_db_path
root = pathlib.Path(sys.argv[2])
provider = MnemosyneMemoryProvider()
for name in ("work", "personal"):
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("memory:\n  mnemosyne:\n    profile_isolation: true\n", encoding="utf-8")
    provider.initialize("smoke", hermes_home=str(home), auto_sleep=False)
    assert provider._beam is not None, provider._init_error
    data = pathlib.Path(os.environ["MNEMOSYNE_DATA_DIR"]) if os.environ.get("MNEMOSYNE_DATA_DIR") else home / "mnemosyne/data"
    expected = data / "banks" / name / "mnemosyne.db"
    assert pathlib.Path(provider._beam.db_path) == expected
    assert pathlib.Path(resolve_effective_db_path(str(home))) == expected
    assert pathlib.Path(provider.identity_signature()["mnemosyne"]["db_path"]) == expected
    provider._beam.remember(content="synthetic fact " + name, source="profile-smoke", scope="global")
    contents = {row[0] for row in provider._beam.conn.execute("SELECT content FROM working_memory")}
    assert contents == {"synthetic fact " + name}, contents
    # First import and real wrapper construction must stay within this store.
    from mnemosyne import Mnemosyne
    memory = Mnemosyne(session_id="smoke", db_path=expected, seed_config=False)
    assert pathlib.Path(memory.db_path) == expected
    assert not (data / "mnemosyne.db").exists()
    assert not (home / "mnemosyne/config.yaml").exists()
provider.shutdown()
assert not (root / "default-data/mnemosyne.db").exists()
assert not (root / "default-data/config.yaml").exists()
assert not (root / "environment-home").exists()
print("two real profile stores, no default DB or config created")
"""
    environment = {
        **os.environ,
        "MNEMOSYNE_DATA_DIR": str(tmp_path / "default-data"),
        "MNEMOSYNE_EMBEDDINGS_OFF": "1",
        "MNEMOSYNE_AUTO_MIGRATE": "0",
    }
    if not data_override:
        environment.pop("MNEMOSYNE_DATA_DIR")
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "integrations/hermes-provider"), str(tmp_path)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
