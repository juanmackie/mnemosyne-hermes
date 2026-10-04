"""Engine patch deployment safety: exact hashes, idempotency, and rollback."""

import contextlib
import difflib
import importlib.util
import json
import pathlib
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


if __name__ == "__main__":
    tests = [
        test_patch_apply_dry_run_idempotency_and_restore,
        test_source_drift_refuses_before_any_write,
        test_bad_backup_or_patch_refuses_before_any_write,
        test_write_failure_rolls_back_previous_source,
        test_shipped_patch_digests,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
