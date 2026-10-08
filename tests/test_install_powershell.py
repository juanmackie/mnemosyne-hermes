"""Native PowerShell installer dry-run safety checks."""

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _powershell():
    shells = _powershells()
    if shells:
        return shells[0]
    pytest.skip("native installer checks need PowerShell")


def _powershells():
    shells = []
    for executable in (shutil.which("pwsh"), shutil.which("powershell")):
        if executable:
            shells.append(executable)
    return shells


@pytest.fixture
def fake_windows_install(tmp_path):
    """A self-contained install tree with stubbed Python package mutations."""
    repo = tmp_path / "repo"
    repo.mkdir()
    installer_source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    # The test environment itself has Hermes PM. Force legacy detection for
    # this fixture, and report no installed distributions so purge never calls
    # into the real selected environment.
    start = installer_source.index("$HermesPM = $false")
    end = installer_source.index("\nif (-not $DbPath)", start)
    installer_source = installer_source[:start] + "$HermesPM = $false\n" + installer_source[end:]
    pkg_start = installer_source.index("function Test-PackageInstalled(")
    pkg_end = installer_source.index("\n}\n", pkg_start) + 3
    installer_source = (
        installer_source[:pkg_start]
        + "function Test-PackageInstalled([string]$PackageName) { return $false }\n"
        + installer_source[pkg_end:]
    )
    legacy_start = installer_source.index("function Install-LegacyPackages {")
    legacy_end = installer_source.index("\n}\n", legacy_start) + 3
    installer_source = (
        installer_source[:legacy_start]
        + (
            "function Install-LegacyPackages {\n"
            "    Add-Content -LiteralPath $env:INSTALL_LOG -Value ('uv pip install --python {0} {1} {2}' -f $VenvPython, $ProviderSource, $EnginePin)\n"
            "}\n"
        )
        + installer_source[legacy_end:]
    )
    installer_source = installer_source.replace(
        "$selectedHermes = Join-Path $Venv 'Scripts/hermes.exe'",
        "$selectedHermes = Join-Path $HermesHome 'missing/hermes.exe'",
    )
    (repo / "install.ps1").write_text(installer_source, encoding="utf-8")
    provider = repo / "integrations/hermes-provider/hermes_memory_provider"
    provider.mkdir(parents=True)
    (provider / "__init__.py").write_text(
        "class Provider:\n"
        "    name = 'mnemosyne'\n"
        "    def is_available(self): return True\n"
        "    def unavailable_reason(self): return ''\n"
        "def register(ctx):\n"
        "    ctx.register_memory_provider(Provider())\n"
        "    ctx.register_cli_command(name='mnemosyne', setup_fn=lambda: None, handler_fn=lambda: None)\n",
        encoding="utf-8",
    )
    (provider / "cli.py").write_text(
        "def register_cli(): pass\ndef mnemosyne_command(): pass\n", encoding="utf-8"
    )
    (provider / "pyproject.toml").write_text(
        "# LOCAL PATCH: Hermes PM dependency declaration fixture\n"
        '[project]\nname = "mnemosyne-fixture"\nversion = "0.1.0"\n'
        'dependencies = ["mnemosyne-memory[embeddings]>=3.15.1,<3.16"]\n',
        encoding="utf-8",
    )
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "apply_engine_patches.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    modules = tmp_path / "python-modules"
    (modules / "mnemosyne/core").mkdir(parents=True)
    (modules / "mnemosyne/__init__.py").write_text("__version__ = 'fixture'\n", encoding="utf-8")
    (modules / "mnemosyne/core/__init__.py").write_text("", encoding="utf-8")
    (modules / "mnemosyne/core/beam.py").write_text(
        "def _default_db_path(): return 'unused-fixture.db'\n", encoding="utf-8"
    )
    (modules / "yaml.py").write_text(
        "def safe_load(text):\n"
        "    if hasattr(text, 'read'): text = text.read()\n"
        "    values = [line.strip() for line in text.splitlines()]\n"
        "    for index, value in enumerate(values):\n"
        "        if value.startswith('provider:'): return {'memory': {'provider': value.split(':', 1)[1].strip()}}\n"
        "        if value == 'provider:' and index + 1 < len(values): return {'memory': {'provider': values[index + 1]}}\n"
        "    return {}\n",
        encoding="utf-8",
    )
    (modules / "plugins/memory").mkdir(parents=True)
    (modules / "plugins/__init__.py").write_text("", encoding="utf-8")
    (modules / "plugins/memory/__init__.py").write_text(
        "import importlib.util, os\n"
        "from pathlib import Path\n"
        "def find_provider_dir(name):\n"
        "    home = Path(os.environ['HERMES_HOME'])\n"
        "    bundled = home / 'hermes-agent/plugins/memory' / name\n"
        "    user = home / 'plugins' / name\n"
        "    return bundled if (bundled / '__init__.py').exists() else user if (user / '__init__.py').exists() else None\n"
        "def load_memory_provider(name):\n"
        "    path = find_provider_dir(name)\n"
        "    spec = importlib.util.spec_from_file_location('_fixture_loaded_provider', path / '__init__.py')\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    import sys; sys.modules[spec.name] = module\n"
        "    spec.loader.exec_module(module)\n"
        "    class Ctx:\n"
        "        def __init__(self): self.providers = []\n"
        "        def register_memory_provider(self, provider): self.providers.append(provider)\n"
        "        def register_cli_command(self, **kwargs): pass\n"
        "    ctx = Ctx()\n"
        "    module.register(ctx)\n"
        "    return ctx.providers[0] if len(ctx.providers) == 1 else None\n",
        encoding="utf-8",
    )

    commands = tmp_path / "commands"
    commands.mkdir()
    home = tmp_path / "hermes-home"
    home.mkdir()
    command_log = tmp_path / "commands.log"
    install_log = tmp_path / "uv.log"
    real_python = pathlib.Path(sys.executable).resolve()
    hermes = commands / "hermes.cmd"
    hermes.write_text(
        "@echo off\r\n"
        f'echo %*>>"{command_log}"\r\n'
        'if "%1"=="pm" if "%2"=="status" exit /b 3\r\n'
        'if "%1"=="pm" if "%2"=="install" exit /b 0\r\n'
        'if "%1"=="config" if "%2"=="set" (\r\n'
        '  >"%HERMES_HOME%\\config.yaml" echo memory:\r\n'
        '  >>"%HERMES_HOME%\\config.yaml" echo   provider: %4\r\n'
        "  exit /b 0\r\n"
        ")\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    uv = commands / "uv.cmd"
    uv.write_text(f'@echo off\r\necho %*>>"{install_log}"\r\nexit /b 0\r\n', encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = str(commands) + os.pathsep + env.get("PATH", "")
    env["PATHEXT"] = ".CMD;.BAT;.EXE;.COM;.PS1"
    env["HERMES_HOME"] = str(home)
    env["INSTALL_LOG"] = str(install_log)
    env["PYTHONPATH"] = str(modules) + os.pathsep + env.get("PYTHONPATH", "")

    def run(*args, env_overrides=None, shell=None):
        call_env = env.copy()
        if env_overrides:
            call_env.update(env_overrides)
        return subprocess.run(
            [
                shell or _powershell(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(repo / "install.ps1"),
                "-Yes",
                "-Python",
                str(real_python),
                "-HermesHome",
                str(home),
                *args,
            ],
            cwd=repo,
            env=call_env,
            capture_output=True,
            text=True,
            timeout=45,
        )

    return run, home, provider, command_log, install_log


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 is a Windows-native entrypoint")
def test_install_dry_run_does_not_copy_provider_or_change_config(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    python = pathlib.Path(sys.executable).resolve()
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "install.ps1"),
            "-DryRun",
            "-Yes",
            "-Python",
            str(python),
            "-HermesHome",
            str(home),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-DryRun: no changes made." in result.stdout
    assert not (home / "plugins").exists()
    assert not (home / "config.yaml").exists()
    assert not (home / "mnemosyne").exists()


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 is a Windows-native entrypoint")
def test_uninstall_dry_run_keeps_memory(tmp_path):
    home = tmp_path / "hermes-home"
    memory = home / "mnemosyne/data/mnemosyne.db"
    memory.parent.mkdir(parents=True)
    memory.write_bytes(b"memory")
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "install.ps1"),
            "-Uninstall",
            "-Purge",
            "-DryRun",
            "-Yes",
            "-Python",
            sys.executable,
            "-HermesHome",
            str(home),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert memory.read_bytes() == b"memory"


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 is a Windows-native entrypoint")
@pytest.mark.parametrize("shell", _powershells(), ids=lambda path: pathlib.Path(path).stem)
def test_full_copy_install_verifies_config_engine_registration_then_purges(
    fake_windows_install, shell
):
    run, home, source, command_log, install_log = fake_windows_install
    installed = home / "plugins/mnemosyne"
    result = run("-Copy", shell=shell)
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed.joinpath("PROVENANCE.json").is_file()
    assert (
        installed.joinpath("pyproject.toml").read_bytes()
        == source.joinpath("pyproject.toml").read_bytes()
    )
    raw_provenance = installed.joinpath("PROVENANCE.json").read_bytes()
    assert not raw_provenance.startswith(b"\xef\xbb\xbf")
    provenance = json.loads(raw_provenance.decode("utf-8"))
    assert provenance["copied_from"] == str(source.resolve())
    assert "pip install --python" in install_log.read_text(encoding="utf-8")
    assert "mnemosyne-memory[embeddings]>=3.15.1,<3.16" in install_log.read_text(encoding="utf-8")
    assert "config set memory.provider mnemosyne" in command_log.read_text(encoding="utf-8")

    bundled = home / "hermes-agent/plugins/memory/mnemosyne"
    bundled.mkdir(parents=True)
    bundled.joinpath("__init__.py").write_text("class BundledProvider: pass\n", encoding="utf-8")
    collision = run(shell=shell)
    assert collision.returncode != 0, collision.stdout + collision.stderr
    assert "loader does not resolve mnemosyne" in collision.stdout + collision.stderr

    data = home / "mnemosyne/data/mnemosyne.db"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"fixture memory")
    removed = run("-Uninstall", "-Purge", shell=shell)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not installed.exists()
    assert not data.parent.parent.exists()
    assert "config set memory.provider " in command_log.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 is a Windows-native entrypoint")
@pytest.mark.parametrize("shell", _powershells(), ids=lambda path: pathlib.Path(path).stem)
def test_package_manager_path_uses_hermes_pm_without_uv_mutation(fake_windows_install, shell):
    run, home, source, command_log, install_log = fake_windows_install
    repo_root = home / "hermes-agent"
    (repo_root / "pm").mkdir(parents=True)
    (repo_root / "pm/cli.py").write_text("# managed Hermes PM fixture\n", encoding="utf-8")
    (repo_root / "pm/environments.py").write_text(
        "# managed environment contract fixture\n", encoding="utf-8"
    )
    # Model current Hermes' documented bundled-first loader resolution. The
    # installer must refuse success if a bundled same-name provider wins.
    fake_loader = home.parent / "fake-hermes-loader"
    (fake_loader / "plugins/memory").mkdir(parents=True)
    (fake_loader / "plugins/__init__.py").write_text("", encoding="utf-8")
    (fake_loader / "plugins/memory/__init__.py").write_text(
        "from pathlib import Path\n"
        "def find_provider_dir(name): return Path(__file__).parent / name\n"
        "def load_memory_provider(name): return None\n",
        encoding="utf-8",
    )
    bundled = fake_loader / "plugins/memory/mnemosyne"
    bundled.mkdir()
    (bundled / "__init__.py").write_text("", encoding="utf-8")
    # This test copy bypasses only the interpreter locator: the production
    # locator is separately boundary checked against PM facts.json.
    # PM fixtures have no native selected generation; inject a test-only helper
    # implementation into a disposable script copy while retaining main flow.
    original = (ROOT / "install.ps1").read_text(encoding="utf-8")
    marker = "function Resolve-PmPython {"
    start = original.index(marker)
    end = original.index("\n}\n", start) + 3
    original = (
        original[:start] + "function Resolve-PmPython { return $VenvPython }\n" + original[end:]
    )
    original = original.replace(
        "$selectedHermes = Join-Path $Venv 'Scripts/hermes.exe'",
        "$selectedHermes = Join-Path $HermesHome 'missing/hermes.exe'",
    )
    test_script = repo_root / "install.ps1"
    test_script.write_text(original, encoding="utf-8")
    # Run with fixture provider code and a stubbed audited patch command.
    provider_target = repo_root / "integrations/hermes-provider/hermes_memory_provider"
    provider_target.parent.mkdir(parents=True)
    shutil.copytree(source, provider_target)
    shutil.copytree(ROOT / "scripts", repo_root / "scripts")
    (repo_root / "scripts/apply_engine_patches.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["PATH"] = str(home.parent / "commands") + os.pathsep + env.get("PATH", "")
    env["HERMES_HOME"] = str(home)
    env["PATHEXT"] = ".CMD;.BAT;.EXE;.COM;.PS1"
    fixture_modules = home.parent / "python-modules"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(fake_loader), str(fixture_modules), env.get("PYTHONPATH", "")) if part
    )
    python_cmd = pathlib.Path(sys.executable).resolve()
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
            "-Yes",
            "-Python",
            str(python_cmd),
            "-HermesHome",
            str(home),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "loader does not resolve mnemosyne" in result.stdout + result.stderr
    calls = command_log.read_text(encoding="utf-8")
    assert "config set memory.provider mnemosyne" in calls
    assert "pm install" in calls
    assert not install_log.exists(), "PM owns the selected environment; uv must not mutate it"
