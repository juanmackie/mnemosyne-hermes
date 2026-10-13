"""Exercise the weekly latest-engine workflow step with local command stubs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "upstream-drift.yml"
LATEST_ENGINE = "3.15.9"
BASH = (
    Path(r"C:\Program Files\Git\bin\bash.exe")
    if os.name == "nt" and Path(r"C:\Program Files\Git\bin\bash.exe").exists()
    else Path(shutil.which("bash") or "bash")
)


def contract_step(workflow_text: str) -> str:
    lines = workflow_text.splitlines()
    step = next(index for index, line in enumerate(lines) if line.strip() == "id: contract")
    run = next(index for index in range(step, len(lines)) if lines[index].strip() == "run: |")
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:] if line.startswith("          ") else "")
    script = textwrap.dedent("\n".join(body))
    return re.sub(r"\$\{\{\s*steps\.latest\.outputs\.version\s*\}\}", LATEST_ENGINE, script)


def create_command_stubs(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"

    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        'printf "uv %s\\n" "$*" >> "$COMMAND_LOG"\n'
        "echo UV_SENTINEL\n"
        'uv_call=$(grep -c "^uv " "$COMMAND_LOG")\n'
        'if [[ "$uv_call" == "${UV_FAIL_AT:-0}" ]]; then exit "${UV_EXIT:-0}"; fi\n'
        'if [[ "$1" == venv ]]; then\n'
        "  mkdir -p .venv/bin\n"
        "  cat > .venv/bin/python <<'PY'\n"
        "#!/bin/bash\n"
        'printf "python %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'case "$*" in\n'
        '  *scripts/apply_engine_patches.py) echo APPLIER_SENTINEL; exit "${PATCH_EXIT:-0}" ;;\n'
        "  *tests/test_provider_engine_contract.py)\n"
        '    [[ "${MNEMOSYNE_REQUIRE_ENGINE:-}" == 1 ]] || exit 91\n'
        '    echo CONTRACT_SENTINEL; exit "${CONTRACT_EXIT:-0}" ;;\n'
        "  *) exit 92 ;;\n"
        "esac\n"
        "PY\n"
        "  chmod +x .venv/bin/python\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir, log


def run_contract_step(
    script: str,
    tmp_path: Path,
    *,
    uv_fail_at: int = 0,
    patch_exit: int = 0,
    contract_exit: int = 0,
    uv_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str, str, str]:
    bin_dir, log = create_command_stubs(tmp_path)
    github_output = tmp_path / "github-output"
    github_env = tmp_path / "github-env"
    github_output.touch()
    github_env.touch()
    if os.name == "nt":
        cygpath = BASH.parent.parent / "usr" / "bin" / "cygpath.exe"

        def shell_path(path: Path) -> str:
            return subprocess.check_output([cygpath, "-u", str(path)], text=True).strip()

        command_path = shell_path(bin_dir)
        log_path = shell_path(log)
        output_path = shell_path(github_output)
        github_env_path = shell_path(github_env)
        shell_path_env = f"{command_path}:/usr/bin:/bin"
    else:
        command_path = str(bin_dir)
        log_path = str(log)
        output_path = str(github_output)
        github_env_path = str(github_env)
        shell_path_env = command_path + os.pathsep + os.environ["PATH"]
    env = os.environ | {
        "PATH": shell_path_env,
        "COMMAND_LOG": log_path,
        "GITHUB_OUTPUT": output_path,
        "GITHUB_ENV": github_env_path,
        "UV_FAIL_AT": str(uv_fail_at),
        "UV_EXIT": str(uv_exit),
        "PATCH_EXIT": str(patch_exit),
        "CONTRACT_EXIT": str(contract_exit),
    }
    result = subprocess.run(
        [str(BASH), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    report = (tmp_path / "contract-latest-engine.md").read_text(encoding="utf-8")
    return result, log.read_text(encoding="utf-8"), github_output.read_text(), report


@pytest.mark.skipif(not BASH.exists(), reason="weekly workflow runs on Ubuntu")
def test_weekly_latest_engine_setup_applies_patches_before_strict_contract(tmp_path):
    script = contract_step(WORKFLOW.read_text(encoding="utf-8"))

    result, calls, output, report = run_contract_step(script, tmp_path)

    assert result.returncode == 0, result.stderr
    assert f"mnemosyne-memory[embeddings]=={LATEST_ENGINE}" in calls
    assert "uv pip install" in calls
    assert "python scripts/apply_engine_patches.py" in calls
    assert "python tests/test_provider_engine_contract.py" in calls
    assert calls.index("python scripts/apply_engine_patches.py") < calls.index(
        "python tests/test_provider_engine_contract.py"
    )
    assert "status=0" in output
    assert "Exit status: 0" in report
    assert "UV_SENTINEL" in report
    assert "APPLIER_SENTINEL" in report
    assert "CONTRACT_SENTINEL" in report


@pytest.mark.parametrize(
    ("patch_exit", "contract_exit", "expected_status", "expect_contract"),
    [(8, 0, 8, False), (0, 7, 7, True)],
)
@pytest.mark.skipif(not BASH.exists(), reason="weekly workflow runs on Ubuntu")
def test_weekly_latest_engine_step_reports_failure_and_preserves_status(
    tmp_path, patch_exit, contract_exit, expected_status, expect_contract
):
    script = contract_step(WORKFLOW.read_text(encoding="utf-8"))

    result, calls, output, report = run_contract_step(
        script, tmp_path, patch_exit=patch_exit, contract_exit=contract_exit
    )

    assert result.returncode == expected_status
    assert f"status={expected_status}" in output
    assert f"Exit status: {expected_status}" in report
    assert "APPLIER_SENTINEL" in report
    assert ("python tests/test_provider_engine_contract.py" in calls) is expect_contract
    assert ("CONTRACT_SENTINEL" in report) is expect_contract


@pytest.mark.parametrize("failed_uv_call", [1, 2, 3])
@pytest.mark.skipif(not BASH.exists(), reason="weekly workflow runs on Ubuntu")
def test_weekly_latest_engine_step_stops_after_setup_or_install_failure(tmp_path, failed_uv_call):
    script = contract_step(WORKFLOW.read_text(encoding="utf-8"))

    result, calls, output, report = run_contract_step(
        script, tmp_path, uv_fail_at=failed_uv_call, uv_exit=6
    )

    assert result.returncode == 6
    assert "status=6" in output
    assert "Exit status: 6" in report
    assert "UV_SENTINEL" in report
    assert calls.count("uv ") == failed_uv_call
    assert "scripts/apply_engine_patches.py" not in calls
    assert "tests/test_provider_engine_contract.py" not in calls
