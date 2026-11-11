"""Issue #55 regression: a fresh Hermes PM generation and the audited patches.

A Hermes PM dependency-generation rebuild installs a fresh upstream
``mnemosyne-memory`` wheel; the audited engine patches are gone and
``hermes mnemosyne doctor --no-fix`` fails until the patches are re-applied to
the newly selected generation. Doctor's fix path is that repair: it runs under
the generation's interpreter and re-applies the audited, hash-verified patch
set (and refuses anything else). This lane starts from a fresh generation and
verifies doctor passes after the supported repair, without touching a live
install.

The fixture mirrors the repository layout: integrations/engine-patches (fixture
manifest + patch files), integrations/hermes-provider/hermes_memory_provider
(the real provider package), scripts/apply_engine_patches.py (the real
applier), and a stub engine on PYTHONPATH whose sources ship unpatched — the
state a fresh PM generation is in. Every doctor invocation runs in a fresh
subprocess so imports reflect the on-disk sources, exactly like
``hermes mnemosyne doctor`` runs.
"""

import difflib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types
from collections.abc import Callable
from contextlib import ExitStack
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "integrations/hermes-provider/hermes_memory_provider"
BACKUP_SUFFIX = ".mnemosyne-hermes-original"

if str(ROOT / "integrations/hermes-provider") not in sys.path:
    sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

import hermes_memory_provider.cli as cli  # noqa: E402

_ORIGINAL_SOURCES = {
    "core/beam.py": (
        "class BeamMemory:\n    def __init__(self, db_path=None):\n        self.db_path = db_path\n"
    ),
    "core/memory.py": (
        "class Mnemosyne:\n    def __init__(self, db_path=None):\n        self.db_path = db_path\n"
    ),
}


def _patched(source: str) -> str:
    return source.replace(
        "(self, db_path=None)",
        "(self, db_path=None, seed_config=True)",
    ).replace(
        "        self.db_path = db_path\n",
        "        self.seed_config = seed_config\n        self.db_path = db_path\n",
    )


_DOCTOR_RUNNER = r"""
import importlib, importlib.util, pathlib, sys

provider_dir = pathlib.Path(sys.argv[1]).resolve()
mode = sys.argv[2]
spec = importlib.util.spec_from_file_location(
    "hermes_memory_provider", provider_dir / "__init__.py",
    submodule_search_locations=[str(provider_dir)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
cli = importlib.import_module("hermes_memory_provider.cli")


class Args:
    mnemosyne_cmd = "doctor"
    no_fix = mode == "--no-fix"
    dry_run = mode == "--dry-run"


code = 0
try:
    result = cli.mnemosyne_command(Args())
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else 1
else:
    if isinstance(result, int):
        code = result
sys.exit(code)
"""


@pytest.fixture
def pm_generation(tmp_path):
    return _build_pm_generation(tmp_path)


def _build_pm_generation(tmp_path):
    """A fixture repo + home + a freshly selected stub engine generation."""
    repo = tmp_path / "repo"
    provider_dir = repo / "integrations/hermes-provider/hermes_memory_provider"
    shutil.copytree(PROVIDER, provider_dir, ignore=shutil.ignore_patterns("__pycache__"))
    applier = repo / "scripts/apply_engine_patches.py"
    applier.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/apply_engine_patches.py", applier)

    engine = tmp_path / "modules/mnemosyne"
    (engine / "core").mkdir(parents=True)
    (engine / "__init__.py").write_text('__version__ = "3.15.1"\n', encoding="utf-8")
    (engine / "core" / "__init__.py").touch()
    for name, source in _ORIGINAL_SOURCES.items():
        (engine / name).write_text(source, encoding="utf-8")
    dist_info = tmp_path / "modules/mnemosyne_memory-3.15.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Name: mnemosyne-memory\nVersion: 3.15.1\n", encoding="utf-8"
    )

    patches = repo / "integrations/engine-patches"
    patches.mkdir()
    manifest = {"distribution": "mnemosyne-memory", "version": "3.15.1", "files": {}}
    for name, source in _ORIGINAL_SOURCES.items():
        updated = _patched(source)
        delta = "".join(
            difflib.unified_diff(
                source.splitlines(True),
                updated.splitlines(True),
                fromfile="a/" + name,
                tofile="b/" + name,
            )
        )
        patch_file = name.replace("/", "-").replace(".py", ".py.patch")
        (patches / patch_file).write_text(delta, encoding="utf-8")
        manifest["files"][name] = {
            "original_sha256": _sha256(source.encode()),
            "patched_sha256": _sha256(updated.encode()),
            "patch": patch_file,
            "patch_sha256": _sha256(delta.encode()),
        }
    (patches / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text("memory:\n  provider: mnemosyne\n", encoding="utf-8")
    try:
        (home / "plugins" / "mnemosyne").symlink_to(provider_dir, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    return types.SimpleNamespace(
        provider_dir=provider_dir,
        modules=tmp_path / "modules",
        home=home,
        engine=engine,
        originals=_ORIGINAL_SOURCES,
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _doctor(gen, mode="fix"):
    return subprocess.run(
        [sys.executable, "-c", _DOCTOR_RUNNER, str(gen.provider_dir), mode],
        env={
            **os.environ,
            "HERMES_HOME": str(gen.home),
            "PYTHONPATH": str(gen.modules),
            "MNEMOSYNE_DB_PATH": os.environ.get("MNEMOSYNE_DB_PATH", ""),
            "MNEMOSYNE_DATA_DIR": os.environ.get("MNEMOSYNE_DATA_DIR", ""),
        },
        text=True,
        capture_output=True,
        timeout=120,
    )


def _engine_bytes(gen):
    return {name: (gen.engine / name).read_bytes() for name in gen.originals}


def test_fresh_generation_doctor_fails_then_fix_path_repairs_and_doctor_passes(pm_generation):
    gen = pm_generation

    # 1. The fresh generation fails doctor, which writes nothing.
    failed = _doctor(gen, "--no-fix")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "[FAIL] engine patches applied" in failed.stdout
    assert "[FAIL] engine matches audited patches" in failed.stdout
    assert "core/beam.py" in failed.stdout  # the audit names the differing file
    assert "hermes mnemosyne doctor" in failed.stdout  # the repair is named
    assert _engine_bytes(gen) == {name: source.encode() for name, source in gen.originals.items()}

    # 2. The supported repair: doctor's fix path re-applies the audited patches.
    repaired = _doctor(gen)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    for name, source in gen.originals.items():
        target = gen.engine / name
        assert "seed_config" in target.read_text(encoding="utf-8"), target
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        assert backup.read_text(encoding="utf-8") == source, backup
    assert "Restart the gateway" in repaired.stdout

    # 3. A fresh doctor --no-fix passes: the issue's acceptance criterion.
    healthy = _doctor(gen, "--no-fix")
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert "[PASS] engine patches applied" in healthy.stdout
    assert "[PASS] engine matches audited patches" in healthy.stdout


def test_pm_rebuild_cycle_is_repaired_by_doctor_again(pm_generation):
    gen = pm_generation
    assert _doctor(gen).returncode == 0

    # Hermes PM selects another fresh generation: the wheel replaces the sources.
    for name, source in gen.originals.items():
        (gen.engine / name).write_text(source, encoding="utf-8")
    failed = _doctor(gen, "--no-fix")
    assert failed.returncode == 1, failed.stdout + failed.stderr

    repaired = _doctor(gen)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert _doctor(gen, "--no-fix").returncode == 0


def test_unknown_engine_revision_is_refused_without_writing(pm_generation):
    gen = pm_generation
    (gen.engine / "core/beam.py").write_text(
        "class BeamMemory:\n    hand_edited = True\n", encoding="utf-8"
    )
    before = _engine_bytes(gen)

    failed = _doctor(gen, "--no-fix")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert _engine_bytes(gen) == before

    attempt = _doctor(gen)
    assert attempt.returncode == 1, attempt.stdout + attempt.stderr
    assert "automatic patch re-apply refused" in attempt.stdout
    assert "differs from audited" in attempt.stdout
    assert _engine_bytes(gen) == before


def test_dry_run_prints_the_plan_without_writing(pm_generation):
    gen = pm_generation
    before = _engine_bytes(gen)

    planned = _doctor(gen, "--dry-run")
    assert planned.returncode == 1, planned.stdout + planned.stderr
    assert "dry-run patch plan: patch: core/beam.py" in planned.stdout
    assert "dry-run patch plan: patch: core/memory.py" in planned.stdout
    assert _engine_bytes(gen) == before


# ---------------------------------------------------------------------------
# In-process wiring checks: the repair hook only runs in fix mode and only
# when a patch check actually failed (no engine on disk is touched here).
# ---------------------------------------------------------------------------


def _doctor_engine_modules(seed_config: bool):
    engine = types.ModuleType("mnemosyne")
    engine.__file__ = "/installed/mnemosyne/__init__.py"
    core = types.ModuleType("mnemosyne.core")
    core.__path__ = []
    beam = types.ModuleType("mnemosyne.core.beam")
    memory = types.ModuleType("mnemosyne.core.memory")

    signature = "(self, db_path=None, seed_config=True)" if seed_config else "(self, db_path=None)"
    namespace: dict = {}
    exec(f"class BeamMemory:\n    def __init__{signature}: pass\n", namespace)
    exec(f"class Mnemosyne:\n    def __init__{signature}: pass\n", namespace)
    # Dynamic stub attributes go through a name-bearing setattr loop: a direct
    # `engine.__version__ = ...` inside a parameterized function is rejected by
    # mypy ("Module has no attribute") and a constant-name setattr trips B010.
    for module, name, value in (
        (engine, "__version__", "3.15.1"),
        (beam, "BeamMemory", namespace["BeamMemory"]),
        (memory, "Mnemosyne", namespace["Mnemosyne"]),
    ):
        setattr(module, name, value)
    return {
        "mnemosyne": engine,
        "mnemosyne.core": core,
        "mnemosyne.core.beam": beam,
        "mnemosyne.core.memory": memory,
    }


def _doctor_patches(tmp_path, audit_results, repair):
    (tmp_path / "plugins" / "mnemosyne").mkdir(parents=True, exist_ok=True)
    return [
        patch.dict(sys.modules, _doctor_engine_modules(seed_config=False)),
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}),
        patch.object(cli, "_provider_registration_count", return_value=(1, "synthetic discovery")),
        patch.object(cli, "check_provider_provenance", return_value=(True, "synthetic provenance")),
        patch.object(cli, "_engine_distribution_matches", return_value=(True, "synthetic engine")),
        patch.object(cli, "_engine_patch_audit", side_effect=list(audit_results)),
        patch.object(cli, "_repair_engine_patches", side_effect=repair),
    ]


def test_fix_mode_repairs_and_flips_failed_patch_checks(tmp_path):
    import io
    from contextlib import redirect_stdout

    calls = []

    def repair(dry_run=False, **_kwargs):
        calls.append(dry_run)
        return True, ["patch: core/beam.py", "patch: core/memory.py"]

    contexts = _doctor_patches(
        tmp_path,
        audit_results=[(False, "stale source"), (True, "audited 3.15.1 source")],
        repair=repair,
    )
    with ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.mnemosyne_command(types.SimpleNamespace(mnemosyne_cmd="doctor"))
    assert code == 0
    assert calls == [False]
    out = buffer.getvalue()
    assert "[PASS] engine patches applied" in out
    assert "audited 3.15.1 source" in out
    assert "Restart the gateway" in out


def test_no_fix_never_attempts_repair(tmp_path):
    import io
    from contextlib import redirect_stdout

    calls = []

    def repair(dry_run=False, **_kwargs):
        calls.append(dry_run)
        raise AssertionError("diagnose-only doctor must not repair")

    contexts = _doctor_patches(
        tmp_path,
        audit_results=[(False, "stale source")],
        repair=repair,
    )
    with ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                cli.mnemosyne_command(types.SimpleNamespace(mnemosyne_cmd="doctor", no_fix=True))
        except SystemExit as failure:
            assert failure.code == 1
        else:
            raise AssertionError("diagnose-only doctor must fail on a stale engine")
    assert calls == []
    out = buffer.getvalue()
    assert "[FAIL] engine patches applied" in out
    assert "[FAIL] engine matches audited patches" in out


def test_healthy_engine_never_triggers_repair(tmp_path):
    import io
    from contextlib import redirect_stdout

    calls = []

    def repair(dry_run=False, **_kwargs):
        calls.append(dry_run)
        raise AssertionError("a healthy engine must not be repaired")

    (tmp_path / "plugins" / "mnemosyne").mkdir(parents=True)
    modules = _doctor_engine_modules(seed_config=True)
    with (
        patch.dict(sys.modules, modules),
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}),
        patch.object(cli, "_provider_registration_count", return_value=(1, "synthetic discovery")),
        patch.object(cli, "check_provider_provenance", return_value=(True, "synthetic provenance")),
        patch.object(cli, "_engine_distribution_matches", return_value=(True, "synthetic engine")),
        patch.object(cli, "_engine_patch_audit", return_value=(True, "audited 3.15.1 source")),
        patch.object(cli, "_repair_engine_patches", side_effect=repair),
    ):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.mnemosyne_command(types.SimpleNamespace(mnemosyne_cmd="doctor", no_fix=True))
    assert code == 0
    assert calls == []


if __name__ == "__main__":
    import tempfile

    tests: list[Callable[..., None]] = [
        test_fresh_generation_doctor_fails_then_fix_path_repairs_and_doctor_passes,
        test_pm_rebuild_cycle_is_repaired_by_doctor_again,
        test_unknown_engine_revision_is_refused_without_writing,
        test_dry_run_prints_the_plan_without_writing,
        test_fix_mode_repairs_and_flips_failed_patch_checks,
        test_no_fix_never_attempts_repair,
        test_healthy_engine_never_triggers_repair,
    ]
    with tempfile.TemporaryDirectory() as bare_tmp:
        root = pathlib.Path(bare_tmp)
        for fn in tests:
            if "pm_generation" in fn.__code__.co_varnames:
                workdir = root / fn.__name__
                workdir.mkdir()
                fn(pm_generation=_build_pm_generation(workdir))
            elif "tmp_path" in fn.__code__.co_varnames:
                workdir = root / fn.__name__
                workdir.mkdir()
                fn(tmp_path=workdir)
            else:
                fn()
            print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
