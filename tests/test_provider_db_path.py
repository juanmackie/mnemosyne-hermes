"""T3/T6/T7/T8 unit tests for the vendored Hermes provider.

Engine-free: the provider is imported from the repo and BeamMemory is replaced
by a fake, so this runs in a bare venv. Run with:

    python tests/test_provider_db_path.py
    pytest tests/test_provider_db_path.py
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_ROOT = ROOT / "integrations" / "hermes-provider"
if str(PROVIDER_ROOT) not in sys.path:
    sys.path.insert(0, str(PROVIDER_ROOT))

import hermes_memory_provider as provider_mod  # noqa: E402
from hermes_memory_provider import cli as cli_mod  # noqa: E402


class FakeBeam:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)
        if not hasattr(self, "canonical_owner_id"):
            self.canonical_owner_id = None
        if not hasattr(self, "agent_context"):
            self.agent_context = None

    def close(self):
        pass


def _init(tmp, **kwargs):
    """Build a provider with FakeBeam and run initialize().

    MNEMOSYNE_DATA_DIR points at a throwaway dir so the engine's config
    singleton (and its auto-export of env values) never touches the real box.
    """
    provider = provider_mod.MnemosyneMemoryProvider()
    original = provider_mod._get_beam_class
    original_audit = provider_mod.MnemosyneMemoryProvider._init_audit_log
    original_data_dir = os.environ.get("MNEMOSYNE_DATA_DIR")
    os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(tmp, "_engine_data")
    provider_mod._get_beam_class = lambda: FakeBeam
    provider_mod.MnemosyneMemoryProvider._init_audit_log = lambda self: None
    try:
        provider.initialize(session_id="t3", hermes_home=str(tmp), **kwargs)
    finally:
        provider_mod._get_beam_class = original
        provider_mod.MnemosyneMemoryProvider._init_audit_log = original_audit
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
        assert provider._beam.db_path == target
        assert provider._beam.kwargs["db_path"] == target


def test_db_path_from_hermes_config_beats_env():
    # Reading memory.mnemosyne.db_path uses the engine's hermes_config helper;
    # without the engine the provider is unavailable anyway, so skip in a bare venv.
    if getattr(provider_mod, "read_hermes_config_key", None) is None:
        print("skip: engine helper read_hermes_config_key unavailable (bare venv)")
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
        assert provider._beam.kwargs["db_path"] == str(pathlib.Path(tmp) / "cfg.db")


def test_db_path_kwargs_beats_all():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MNEMOSYNE_DB_PATH"] = str(pathlib.Path(tmp) / "env.db")
        try:
            provider = _init(tmp, db_path=str(pathlib.Path(tmp) / "kwarg.db"))
        finally:
            os.environ.pop("MNEMOSYNE_DB_PATH", None)
        assert provider._beam.kwargs["db_path"] == str(pathlib.Path(tmp) / "kwarg.db")


def test_db_path_unset_passes_nothing_to_beam():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.pop("MNEMOSYNE_DB_PATH", None)
        provider = _init(tmp)
        assert provider._db_path is None
        assert "db_path" not in provider._beam.kwargs


def test_schema_declares_db_path():
    schema = provider_mod.MnemosyneMemoryProvider().get_config_schema()
    keys = {entry["key"] for entry in schema}
    assert "db_path" in keys, keys


def test_db_path_wins_over_profile_isolation():
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
        assert provider._beam.kwargs["db_path"] == str(pathlib.Path(tmp) / "isolated.db")


def test_check_hermes_version_truth_table():
    ok, _ = cli_mod.check_hermes_version("0.21.2")
    assert ok is True
    ok, _ = cli_mod.check_hermes_version("0.18.2")
    assert ok is True
    for bad in ("0.17.9", "0.22.0", "1.0.0", None, "not-a-version"):
        ok, msg = cli_mod.check_hermes_version(bad)
        assert ok is False, (bad, msg)
        assert "range" in msg


def test_check_provider_provenance_canonical():
    ok, msg = cli_mod.check_provider_provenance(
        PROVIDER_ROOT / "hermes_memory_provider"
    )
    assert ok is True, msg


def test_check_provider_provenance_flags_retired_tree():
    with tempfile.TemporaryDirectory() as tmp:
        retired = pathlib.Path(tmp) / "integrations" / "hermes" / "src" / "mnemosyne_hermes"
        retired.mkdir(parents=True)
        (retired / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
        ok, msg = cli_mod.check_provider_provenance(retired)
        assert ok is False
        assert "RETIRED" in msg, msg


def test_describe_memory_location_warns_outside_home():
    with tempfile.TemporaryDirectory() as tmp:
        inside = pathlib.Path(tmp) / "mnemosyne" / "data" / "mnemosyne.db"
        lines = cli_mod.describe_memory_location(inside, tmp)
        assert not any("WARNING" in line for line in lines), lines

        outside = pathlib.Path(tmp).parent / "elsewhere" / "mnemosyne.db"
        lines = cli_mod.describe_memory_location(outside, tmp)
        assert any("WARNING" in line for line in lines), lines


def test_db_writable_missing_parent_is_failure():
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "nope" / "nope" / "mnemosyne.db"
        ok, _ = cli_mod._db_writable(str(target))
        assert ok is False


if __name__ == "__main__":
    tests = [
        test_db_path_from_env,
        test_db_path_from_hermes_config_beats_env,
        test_db_path_kwargs_beats_all,
        test_db_path_unset_passes_nothing_to_beam,
        test_schema_declares_db_path,
        test_db_path_wins_over_profile_isolation,
        test_check_hermes_version_truth_table,
        test_check_provider_provenance_canonical,
        test_check_provider_provenance_flags_retired_tree,
        test_describe_memory_location_warns_outside_home,
        test_db_writable_missing_parent_is_failure,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
