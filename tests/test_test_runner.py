"""Regression tests for the cross-platform test runner environment."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BASH: str | None = None


def _find_bash() -> str | None:
    global _BASH
    if os.name == "nt":
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        ):
            if base:
                candidate = Path(base) / "Git" / "bin" / "bash.exe"
                if candidate.is_file():
                    _BASH = str(candidate)
                    return str(candidate)
    bash = shutil.which("bash")
    if bash:
        _BASH = bash
        return bash
    return None


def _bash_path(path: Path) -> str:
    """Return a path Bash can execute, including under Git Bash on Windows."""
    if os.name != "nt":
        return str(path)
    cygpath = shutil.which("cygpath")
    if not cygpath and _BASH:
        for candidate in (
            Path(_BASH).parent / "cygpath.exe",
            Path(_BASH).parent.parent / "usr" / "bin" / "cygpath.exe",
        ):
            if candidate.is_file():
                cygpath = str(candidate)
                break
    if not cygpath:
        drive, tail = os.path.splitdrive(str(path))
        if drive:
            tail = tail.replace("\\", "/").lstrip("/")
            return f"/{drive[0].lower()}/{tail}"
        pytest.skip("Git Bash cygpath is unavailable for a non-drive path")
    result = subprocess.run([cygpath, "-u", str(path)], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_runner_preserves_native_pythonpath_and_python_path_with_spaces(
    tmp_path: Path,
) -> None:
    bash = _find_bash()
    if not bash:
        pytest.skip("bash is unavailable")

    engine = tmp_path / "patched engine"
    engine.mkdir()
    (engine / "runner_engine_probe.py").write_text("VALUE = 'found'\n", encoding="utf-8")

    capture = tmp_path / "captured pythonpath.txt"
    wrapper_dir = tmp_path / "python wrapper with spaces"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "python wrapper"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'if [[ "${1:-}" == "-c" ]]; then\n'
        '  exec "$TEST_REAL_PYTHON" "$@"\n'
        "fi\n"
        'printf "%s" "${PYTHONPATH-<unset>}" > "$TEST_CAPTURE"\n',
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": _bash_path(wrapper),
            "PYTHONPATH": str(engine),
            "TEST_CAPTURE": _bash_path(capture),
            "TEST_REAL_PYTHON": _bash_path(Path(sys.executable)),
        }
    )
    subprocess.run(
        [bash, _bash_path(ROOT / "test-all.sh"), "--skip-llm"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    actual = capture.read_text(encoding="utf-8")
    expected_src = str((ROOT / "src").resolve())
    assert actual.split(os.pathsep) == [expected_src, str(engine)]

    probe_env = env.copy()
    probe_env["PYTHONPATH"] = actual
    probe = subprocess.run(
        [sys.executable, "-c", "import runner_engine_probe; print(runner_engine_probe.VALUE)"],
        env=probe_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.stdout.strip() == "found"

    env.pop("PYTHONPATH")
    subprocess.run(
        [bash, _bash_path(ROOT / "test-all.sh"), "--skip-llm"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert capture.read_text(encoding="utf-8").split(os.pathsep) == [expected_src]
