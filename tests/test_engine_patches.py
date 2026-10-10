"""Engine patch deployment safety: exact hashes, idempotency, and rollback."""

import contextlib
import difflib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "engine_patches", ROOT / "scripts/apply_engine_patches.py"
)
assert spec is not None and spec.loader is not None
applier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(applier)


@contextlib.contextmanager
def patch_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        package = root / "mnemosyne"
        patches = root / "patches"
        package.mkdir()
        patches.mkdir()
        manifest = {"version": "fixture", "files": {}}
        for name in ("first.py", "second.py"):
            original = b"value = 'old'\n"
            updated = b"value = 'new'\n"
            delta = "".join(
                difflib.unified_diff(
                    original.decode().splitlines(True),
                    updated.decode().splitlines(True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            ).encode()
            (package / name).write_bytes(original)
            (patches / (name + ".patch")).write_bytes(delta)
            manifest["files"][name] = {
                "original_sha256": applier.digest(original),
                "patched_sha256": applier.digest(updated),
                "patch": name + ".patch",
                "patch_sha256": applier.digest(delta),
            }
        (patches / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(applier, "PATCH_ROOT", patches):
            yield package, patches


def test_patch_apply_dry_run_idempotency_and_restore():
    with patch_fixture() as (package, _):
        before = {p.name: p.read_bytes() for p in package.iterdir()}
        applier.apply_patches(package, dry_run=True)
        assert {p.name: p.read_bytes() for p in package.iterdir()} == before
        applier.apply_patches(package)
        for name in before:
            assert (package / name).read_bytes() == b"value = 'new'\n"
            assert (package / (name + applier.BACKUP_SUFFIX)).read_bytes() == before[name]
        assert all(s.startswith("already patched:") for s in applier.apply_patches(package))
        applier.apply_patches(package, restore=True)
        for name in before:
            assert (package / name).read_bytes() == before[name]
        applier.apply_patches(package)
        assert (package / "first.py").read_bytes() == b"value = 'new'\n"


def test_source_drift_refuses_before_any_write():
    with patch_fixture() as (package, _):
        (package / "second.py").write_bytes(b"unexpected = True\n")
        before = {p.name: p.read_bytes() for p in package.iterdir()}
        try:
            applier.apply_patches(package)
        except ValueError as exc:
            assert "differs from audited" in str(exc)
        else:
            raise AssertionError("unreviewed engine source was patched")
        assert {p.name: p.read_bytes() for p in package.iterdir()} == before


def test_bad_backup_or_patch_refuses_before_any_write():
    for corrupt in ("backup", "patch"):
        with patch_fixture() as (package, patches):
            if corrupt == "backup":
                (package / ("second.py" + applier.BACKUP_SUFFIX)).write_bytes(b"unowned backup")
            else:
                (patches / "second.py.patch").write_bytes(b"unreviewed patch")
            before = {p.name: p.read_bytes() for p in package.iterdir()}
            try:
                applier.apply_patches(package)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unverified {corrupt} accepted")
            assert {p.name: p.read_bytes() for p in package.iterdir()} == before


def test_write_failure_rolls_back_previous_source():
    with patch_fixture() as (package, _):
        original_write = applier.atomic_write
        failed = False

        def fail_second(path, data):
            nonlocal failed
            if path.name == "second.py" and not failed:
                failed = True
                raise OSError("injected source replacement failure")
            original_write(path, data)

        with patch.object(applier, "atomic_write", side_effect=fail_second):
            try:
                applier.apply_patches(package)
            except OSError:
                pass
            else:
                raise AssertionError("injected write failure disappeared")
        assert failed
        for name in ("first.py", "second.py"):
            assert (package / name).read_bytes() == b"value = 'old'\n"


def test_shipped_patch_digests():
    manifest = json.loads((applier.PATCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["distribution"] == "mnemosyne-memory"
    assert manifest["version"] == "3.15.1"
    for entry in manifest["files"].values():
        assert (
            applier.digest((applier.PATCH_ROOT / entry["patch"]).read_bytes())
            == entry["patch_sha256"]
        )


def test_upgrade_from_previous_audited_patch_requires_original_backup():
    with patch_fixture() as (package, patches):
        applier.apply_patches(package)
        manifest_path = patches / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        previous = b"value = 'new'\n"
        updated = b"value = 'newer'\n"
        original = b"value = 'old'\n"
        for entry in manifest["files"].values():
            entry["previous_patched_sha256"] = [applier.digest(previous)]
            entry["patched_sha256"] = applier.digest(updated)
            delta = "".join(
                difflib.unified_diff(
                    original.decode().splitlines(True),
                    updated.decode().splitlines(True),
                    fromfile="a/source.py",
                    tofile="b/source.py",
                )
            ).encode()
            (patches / entry["patch"]).write_bytes(delta)
            entry["patch_sha256"] = applier.digest(delta)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        missing_backup = package / ("second.py" + applier.BACKUP_SUFFIX)
        missing_backup.unlink()
        before = {p.name: p.read_bytes() for p in package.iterdir()}
        try:
            applier.apply_patches(package)
        except ValueError as exc:
            assert "cannot upgrade without verified original backup" in str(exc)
        else:
            raise AssertionError("previous patch upgraded without verified original")
        assert {p.name: p.read_bytes() for p in package.iterdir()} == before
        missing_backup.write_bytes(original)
        applier.apply_patches(package)
        assert (package / "first.py").read_bytes() == updated
        assert (package / "second.py").read_bytes() == updated
        assert all(s.startswith("already patched:") for s in applier.apply_patches(package))
        applier.apply_patches(package, restore=True)
        assert (package / "first.py").read_bytes() == original


def test_engine_import_and_profile_constructor_do_not_touch_default_store():
    """Exercise the real patched engine in a fresh process with isolated paths."""
    engine_root = os.environ.get("MNEMOSYNE_TEST_ENGINE_ROOT")
    if engine_root:
        package_dir = pathlib.Path(engine_root).resolve()
    else:
        package_spec = importlib.util.find_spec("mnemosyne")
        package_dir = (
            pathlib.Path(next(iter(package_spec.submodule_search_locations))).resolve()
            if package_spec and package_spec.submodule_search_locations
            else None
        )
    if package_dir is None or not (package_dir / "core" / "memory.py").is_file():
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise AssertionError("real engine constructor smoke requires mnemosyne-memory")
        print("skip: mnemosyne-memory is not importable")
        return

    script = r"""
import pathlib, sys
package = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(package.parent))
root = pathlib.Path(sys.argv[2])
default_db = root / "environment-data" / "mnemosyne.db"
default_config = root / "environment-data" / "config.yaml"

# Importing the wrapper must not initialize any store or seed global config.
import mnemosyne.core.memory
assert not default_db.exists(), default_db
assert not default_config.exists(), default_config

# Explicit profile construction initializes that database while avoiding the
# process-global config seed and the environment-selected default database.
from mnemosyne import Mnemosyne
profile_db = root / "profile" / "mnemosyne.db"
memory = Mnemosyne(db_path=profile_db, seed_config=False)
assert profile_db.is_file(), profile_db
assert pathlib.Path(memory.db_path) == profile_db
assert not default_db.exists(), default_db
assert not default_config.exists(), default_config

# Diagnostics can be directed at a profile DB and log directory without
# constructing a second default store or writing under the default Hermes home.
from mnemosyne.diagnose import run_diagnostics
diagnostic_db = root / "diagnostic-profile" / "mnemosyne.db"
diagnostic_logs = root / "diagnostic-profile" / "logs"
run_diagnostics(db_path=diagnostic_db, seed_config=False, log_dir=diagnostic_logs)
assert diagnostic_db.is_file(), diagnostic_db
assert list(diagnostic_logs.glob("diagnose_*.jsonl")), diagnostic_logs
assert not default_db.exists(), default_db
assert not default_config.exists(), default_config
assert not (root / "environment-home" / "mnemosyne" / "logs").exists()

# Standalone callers keep the historical default behavior when the new option
# is omitted: selected default DB initialization and config seeding still work.
standalone = Mnemosyne()
assert pathlib.Path(standalone.db_path) == default_db
assert default_db.is_file(), default_db
assert default_config.is_file(), default_config
from mnemosyne.core import memory as memory_module
memory_module._default_instance = None
default_helper = memory_module._get_default()
assert pathlib.Path(default_helper.db_path) == default_db
assert default_db.is_file(), default_db
assert default_config.is_file(), default_config
print("import/profile isolation and standalone defaults passed")
"""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        env = {
            **os.environ,
            "HERMES_HOME": str(root / "environment-home"),
            "MNEMOSYNE_DATA_DIR": str(root / "environment-data"),
            "MNEMOSYNE_EMBEDDINGS_OFF": "1",
            "MNEMOSYNE_AUTO_MIGRATE": "0",
        }
        result = subprocess.run(
            [sys.executable, "-c", script, str(package_dir), str(root)],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    tests = [
        test_patch_apply_dry_run_idempotency_and_restore,
        test_source_drift_refuses_before_any_write,
        test_bad_backup_or_patch_refuses_before_any_write,
        test_write_failure_rolls_back_previous_source,
        test_shipped_patch_digests,
        test_upgrade_from_previous_audited_patch_requires_original_backup,
        test_engine_import_and_profile_constructor_do_not_touch_default_store,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
