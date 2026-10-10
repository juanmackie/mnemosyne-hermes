"""scripts/update.sh against a local origin: notify-only check, apply, rollback.

The installer is a stub that records its arguments; the git history, fast-forward
and rollback behavior is the real script.
"""

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

STUB_INSTALL = """#!/bin/bash
echo "$@" >> "$(dirname "$0")/install.log"
[ -f "$(dirname "$0")/BREAK" ] && exit 1
exit 0
"""


FAKE_HERMES = """#!/bin/bash
echo "$@" >> "$FAKE_HERMES_LOG"
[ -f "$FAKE_HERMES_FAIL" ] && exit 1
exit 0
"""


def _bash():
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.insert(0, "C:/Program Files/Git/bin/bash.exe")
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            probe = subprocess.run(
                [candidate, "-c", "command -v git"], capture_output=True, timeout=10
            )
            if probe.returncode == 0:
                return candidate
    pytest.skip("update.sh needs Bash and git")


def _git(cwd, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(cwd, name, body, message):
    (cwd / name).write_text(body, encoding="utf-8", newline="\n")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "checkout", "-q", "-b", "main")
    (seed / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "update.sh", seed / "scripts" / "update.sh")
    (seed / "install.sh").write_text(STUB_INSTALL, encoding="utf-8", newline="\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    user = tmp_path / "user"
    _git(tmp_path, "clone", "-q", str(origin), str(user))
    return seed, user


@pytest.fixture(autouse=True)
def fake_hermes(tmp_path, monkeypatch):
    """Keep the run hermetic: a recorded fake `hermes` shadows any real one on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "hermes"
    exe.write_text(FAKE_HERMES, encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HERMES_LOG", str(tmp_path / "hermes.log"))
    monkeypatch.setenv("FAKE_HERMES_FAIL", str(tmp_path / "hermes.fail"))
    return tmp_path


def _run(user, *args):
    return subprocess.run(
        [_bash(), "scripts/update.sh", *args], cwd=user, capture_output=True, text=True, timeout=60
    )


def _publish(seed, message="new feature"):
    _commit(seed, "feature.txt", message, message)
    _git(seed, "push", "-q", "origin", "main")


def test_check_reports_update_without_changing_the_checkout(repos):
    seed, user = repos
    before = _git(user, "rev-parse", "HEAD")
    current = _run(user, "--check", "--quiet")  # nothing new yet
    assert current.returncode == 0 and current.stdout == ""
    assert "up to date" in _run(user, "--check").stdout

    _publish(seed, "add the new thing")
    result = _run(user)  # --check is the default
    assert result.returncode == 10, result.stderr
    assert "add the new thing" in result.stdout
    assert _git(user, "rev-parse", "HEAD") == before
    assert not (user / "install.log").exists()


def test_apply_fast_forwards_reinstalls_and_forwards_install_args(repos, fake_hermes):
    seed, user = repos
    _publish(seed)
    result = _run(user, "--apply", "--", "--hermes-home", "/some/home")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(user, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")
    assert (user / "install.log").read_text().split() == ["--yes", "--hermes-home", "/some/home"]
    assert (fake_hermes / "hermes.log").read_text().split() == ["mnemosyne", "doctor", "--no-fix"]
    assert _run(user, "--apply").returncode == 0  # already current: no second install
    assert len((user / "install.log").read_text().splitlines()) == 1


def test_failed_install_rolls_back_to_the_previous_commit(repos):
    seed, user = repos
    before = _git(user, "rev-parse", "HEAD")
    (seed / "BREAK").write_text("x", encoding="utf-8")
    _git(seed, "add", "BREAK")
    _git(seed, "commit", "-q", "-m", "break the installer")
    _git(seed, "push", "-q", "origin", "main")

    result = _run(user, "--apply")
    assert result.returncode == 1
    assert "restoring" in result.stderr
    assert _git(user, "rev-parse", "HEAD") == before
    assert not (user / "BREAK").exists()


def test_apply_refuses_a_dirty_tree(repos):
    seed, user = repos
    _publish(seed)
    (user / "install.sh").write_text(
        STUB_INSTALL + "# local edit\n", encoding="utf-8", newline="\n"
    )
    before = _git(user, "rev-parse", "HEAD")
    result = _run(user, "--apply")
    assert result.returncode == 1
    assert "local changes" in result.stderr
    assert _git(user, "rev-parse", "HEAD") == before


def test_diverged_history_is_an_error_not_a_merge(repos):
    seed, user = repos
    _publish(seed, "upstream change")
    _commit(user, "mine.txt", "local", "local only")
    result = _run(user, "--apply")
    assert result.returncode == 1
    assert "diverged" in result.stderr


def test_failed_doctor_rolls_back_and_reinstalls_the_previous_commit(repos, fake_hermes):
    seed, user = repos
    before = _git(user, "rev-parse", "HEAD")
    _publish(seed)
    (fake_hermes / "hermes.fail").write_text("x", encoding="utf-8")

    result = _run(user, "--apply")
    assert result.returncode == 1
    assert _git(user, "rev-parse", "HEAD") == before
    assert not (user / "feature.txt").exists()
    # one install for the new commit, one restoring the old one
    assert len((user / "install.log").read_text().splitlines()) == 2


# --- scripts/update.ps1 (native Windows installer's update path) -------------

STUB_INSTALL_PS1 = """param([switch]$Yes, [string]$HermesHome)
Add-Content -LiteralPath (Join-Path $PSScriptRoot 'install.log') -Value "yes=$Yes home=$HermesHome"
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'BREAK')) { throw 'stub install failure' }
"""


def _powershell():
    for exe in (shutil.which("pwsh"), shutil.which("powershell")):
        if exe:
            return exe
    pytest.skip("update.ps1 needs PowerShell")


def _run_ps(user, *args):
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(user / "scripts" / "update.ps1"),
            *args,
        ],
        cwd=user,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def ps_repos(repos, fake_hermes):
    seed, user = repos
    shutil.copyfile(ROOT / "scripts" / "update.ps1", seed / "scripts" / "update.ps1")
    (seed / "install.ps1").write_text(STUB_INSTALL_PS1, encoding="utf-8", newline="\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "powershell stubs")
    _git(seed, "push", "-q", "origin", "main")
    _git(user, "pull", "-q", "--ff-only")
    if os.name == "nt":
        (fake_hermes / "bin" / "hermes.cmd").write_text(
            "@echo off\r\n"
            'echo %* >> "%FAKE_HERMES_LOG%"\r\n'
            'if exist "%FAKE_HERMES_FAIL%" exit /b 1\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
            newline="",
        )
        (fake_hermes / "bin" / "hermes").unlink()
    return seed, user


def test_ps1_check_then_apply_updates_and_verifies(ps_repos, fake_hermes):
    seed, user = ps_repos
    _publish(seed, "ps feature")
    before = _git(user, "rev-parse", "HEAD")

    checked = _run_ps(user)
    assert checked.returncode == 10, checked.stdout + checked.stderr
    assert "ps feature" in checked.stdout
    assert _git(user, "rev-parse", "HEAD") == before

    applied = _run_ps(user, "-Apply", "-HermesHome", "H")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert _git(user, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")
    assert (user / "install.log").read_text().split() == ["yes=True", "home=H"]
    assert "mnemosyne doctor --no-fix" in (fake_hermes / "hermes.log").read_text()


def test_ps1_failed_install_rolls_back(ps_repos):
    seed, user = ps_repos
    before = _git(user, "rev-parse", "HEAD")
    (seed / "BREAK").write_text("x", encoding="utf-8")
    _git(seed, "add", "BREAK")
    _git(seed, "commit", "-q", "-m", "break")
    _git(seed, "push", "-q", "origin", "main")

    result = _run_ps(user, "-Apply")
    assert result.returncode == 1, result.stdout + result.stderr
    assert _git(user, "rev-parse", "HEAD") == before
    assert not (user / "BREAK").exists()
