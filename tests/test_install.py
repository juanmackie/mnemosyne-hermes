"""Exercise installer activation in an isolated repo with no package or DB writes.

Only dependency installation, the engine and Hermes are fixtures; the actual
installer runs through plugin deployment and verifies the selected config.
"""

import os
import pathlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _bash():
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.insert(0, "C:/Program Files/Git/bin/bash.exe")
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            probe = subprocess.run(
                [candidate, "-c", "command -v dirname && command -v cp"],
                capture_output=True,
                timeout=10,
            )
            if probe.returncode == 0:
                return candidate
    pytest.skip("installer regression needs Bash with coreutils")


def _script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\nset -eu\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    import sys

    bash = _bash()
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copyfile(ROOT / "install.sh", repo / "install.sh")
    provider = repo / "integrations/hermes-provider/hermes_memory_provider"
    provider.mkdir(parents=True)
    (provider / "__init__.py").write_text(
        "class Provider:\n"
        "    name = 'mnemosyne'\n"
        "    def is_available(self): return True\n"
        "def register(ctx):\n"
        "    ctx.register_memory_provider(Provider())\n"
        "    ctx.register_cli_command(name='mnemosyne', setup_fn=lambda: None, "
        "handler_fn=lambda: None)\n",
        encoding="utf-8",
    )
    (provider / "cli.py").write_text(
        # A relative sibling import mirrors the real cli.py (P31 added one).
        # Loading this module as a bare file raises "attempted relative import
        # with no known parent package", which is how uninstall silently broke.
        "from . import sibling\n"
        "def register_cli(): pass\ndef mnemosyne_command(): pass\n"
        "def _verified_copy(destination, expected_source=None):\n"
        "    import base64, hashlib, json\n"
        "    from pathlib import Path\n"
        "    destination = Path(destination)\n"
        "    try:\n"
        "        meta = json.loads((destination / 'PROVENANCE.json').read_text(encoding='utf-8'))\n"
        "        files = {p.relative_to(destination).as_posix(): base64.urlsafe_b64encode(hashlib.sha256(p.read_bytes()).digest()).decode().rstrip('=') for p in destination.rglob('*') if p.is_file() and p.name != 'PROVENANCE.json' and '__pycache__' not in p.parts}\n"
        "        valid = meta['copied_from'] == str(Path(expected_source).resolve()) and files == meta['files']\n"
        "        return (valid, 'valid' if valid else 'provenance mismatch')\n"
        "    except Exception as exc: return (False, str(exc))\n",
        encoding="utf-8",
    )
    (provider / "sibling.py").write_text("VALUE = 1\n", encoding="utf-8")
    patches = repo / "scripts/apply_engine_patches.py"
    patches.parent.mkdir()
    patches.write_text("# Engine patching is a fixture; no installed source is touched.\n")
    modules = tmp_path / "modules"
    engine = modules / "mnemosyne/core"
    engine.mkdir(parents=True)
    (engine.parent / "__init__.py").touch()
    metadata = modules / "mnemosyne_memory-3.15.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: mnemosyne-memory\nVersion: 3.15.1\n")
    (engine / "__init__.py").touch()
    (engine / "beam.py").write_text(
        "def _default_db_path(): return 'unused-fixture.db'\n"
        "class BeamMemory:\n"
        "    def __init__(self, seed_config=True, **kwargs): pass\n"
    )
    (engine / "memory.py").write_text(
        "class Mnemosyne:\n    def __init__(self, seed_config=True, **kwargs): pass\n"
    )
    # Keep the test stdlib-only, including the installer's YAML verification.
    # The fixture accepts just the simple YAML that the fake Hermes writes.
    (modules / "yaml.py").write_text(
        "def safe_load(text):\n"
        "    if hasattr(text, 'read'): text = text.read()\n"
        "    value = text.split('provider:', 1)[1].splitlines()[0].strip()\n"
        "    return {'memory': {'provider': value}}\n",
        encoding="utf-8",
    )
    loader = modules / "plugins/memory/__init__.py"
    loader.parent.mkdir(parents=True)
    (modules / "plugins/__init__.py").write_text("", encoding="utf-8")
    loader.write_text(
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
        "        providers = []\n"
        "        def register_memory_provider(self, provider): self.providers.append(provider)\n"
        "        def register_cli_command(self, **kwargs): pass\n"
        "    ctx = Ctx()\n"
        "    module.register(ctx)\n"
        "    return ctx.providers[0] if len(ctx.providers) == 1 else None\n",
        encoding="utf-8",
    )
    venv = tmp_path / "venv"
    _script(
        venv / "bin/python",
        'if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then exit 99; fi\n'
        # -S keeps the host interpreter's site-packages out of the fixture venv.
        # The engine-contract lane really installs mnemosyne-hermes-provider, so
        # without isolation distribution_status finds it and the stubbed uv
        # cannot remove it, making uninstall report "still appears installed".
        'export PYTHONPATH="$TEST_MODULES"\nexec "$TEST_PYTHON" -S "$@"\n',
    )
    (venv / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
    commands = tmp_path / "commands"
    _script(commands / "uv", 'printf "%s\\n" "$*" >> "$TEST_INSTALL_LOG"\n')
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("memory:\n  provider: local\n", encoding="utf-8")
    install_log = tmp_path / "installs.log"
    hermes_log = tmp_path / "hermes.log"
    env = os.environ.copy()
    for name in ("MNEMOSYNE_DB_PATH", "MNEMOSYNE_DATA_DIR", "HERMES_VENV"):
        env.pop(name, None)
    env.update(
        TEST_MODULES=str(modules),
        TEST_PYTHON=sys.executable.replace("\\", "/"),
        TEST_INSTALL_LOG=str(install_log),
        TEST_HERMES_LOG=str(hermes_log),
        TEST_COMMANDS=commands.as_posix(),
        TEST_BASH=bash.replace("\\", "/"),
    )
    # Obtain Bash's native PATH separator/conversion, then remove live Hermes.
    shell_path = subprocess.run(
        [bash, "-c", 'printf "%s" "$PATH"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    shell_path = ":".join(p for p in shell_path.split(":") if "hermes" not in p.lower())
    env["TEST_SHELL_PATH"] = shell_path

    def run(*args, fail_commands=()):
        # Break a coreutils command with an exported shell function instead of a
        # PATH stub: MSYS bash re-prepends /usr/bin when it is re-entered through
        # its Windows path, so a stub script in TEST_COMMANDS is shadowed there.
        # An exported function is inherited by the exec'd installer on every
        # platform, including MSYS.
        prelude = "".join(
            f"function {name} {{ return 73; }}\nexport -f {name}\n" for name in fail_commands
        )
        script = (
            'export PATH="$(cd "$TEST_COMMANDS" && pwd):$TEST_SHELL_PATH"\n'
            + prelude
            + 'exec "$TEST_BASH" "$@"'
        )
        return subprocess.run(
            [
                bash,
                "-c",
                script,
                "installer-fixture",
                (repo / "install.sh").as_posix(),
                "--venv",
                venv.as_posix(),
                "--hermes-home",
                home.as_posix(),
                "--db-path",
                (home / "unused.db").as_posix(),
                "--yes",
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def hermes(path, *, noop=False, fail=False):
        action = ""
        if fail:
            action = "exit 7\n"
        elif not noop:
            action = (
                'if [[ "${1:-}" == "config" && "${2:-}" == "set" ]]; then '
                'printf "memory:\\n  provider: %s\\n" "$4" > "$HERMES_HOME/config.yaml"; fi\n'
            )
        _script(
            path,
            'printf "%s\\n" "$0 $*" >> "$TEST_HERMES_LOG"\n'
            'if [[ "${1:-}" == "pm" && "${2:-}" == "status" ]]; then exit 3; fi\n' + action,
        )

    run.provider_source = provider / "__init__.py"
    run.engine = engine
    run.modules = modules
    run.repo = repo
    run.env = env
    return run, hermes, venv, commands, home, install_log, hermes_log


def test_install_rejects_unpatched_engine_and_restores_provider(installer):
    run, hermes, venv, _, home, _, _ = installer
    hermes(venv / "bin/hermes")
    (run.engine / "memory.py").write_text("class Mnemosyne:\n    def __init__(self): pass\n")
    result = run()
    assert result.returncode != 0
    assert "seed_config" in result.stderr
    assert not (home / "plugins/mnemosyne").exists()
    assert "provider: local" in (home / "config.yaml").read_text()


def test_install_rejects_shadow_engine(installer, tmp_path):
    run, hermes, venv, _, home, _, _ = installer
    hermes(venv / "bin/hermes")
    # The package on sys.path is valid, but resolves outside the distribution.
    shadow = tmp_path / "shadow"
    shutil.copytree(run.modules / "mnemosyne", shadow / "mnemosyne")
    run.env["TEST_MODULES"] = os.pathsep.join((str(shadow), str(run.modules)))
    result = run()
    assert result.returncode != 0
    assert "differs from installed package" in result.stderr
    assert not (home / "plugins/mnemosyne").exists()


def test_selected_venv_cli_activates_provider_without_path_cli(installer):
    run, hermes, venv, _, home, installs, calls = installer
    hermes(venv / "bin/hermes")
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "provider: mnemosyne" in (home / "config.yaml").read_text()
    assert "bin/hermes config set memory.provider mnemosyne" in calls.read_text()
    invocations = installs.read_text().splitlines()
    assert len(invocations) == 1
    assert "hermes-provider" in invocations[0]
    assert "mnemosyne-memory[embeddings]>=3.15.1,<3.16" in invocations[0]


def test_selected_venv_cli_wins_over_path_cli(installer):
    run, hermes, venv, commands, _, _, calls = installer
    hermes(venv / "bin/hermes")
    hermes(commands / "hermes", fail=True)
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "commands/hermes" not in calls.read_text().replace("\\", "/")


def test_missing_cli_refuses_existing_config_before_mutations(installer):
    run, _, _, _, home, installs, _ = installer
    config = home / "config.yaml"
    before = config.read_bytes()
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert config.read_bytes() == before
    assert not installs.exists()
    assert not (home / "plugins").exists()
    assert "Installed." not in result.stdout


def test_noop_config_setter_cannot_report_success(installer):
    run, hermes, venv, _, home, _, _ = installer
    hermes(venv / "bin/hermes", noop=True)
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert "provider: local" in (home / "config.yaml").read_text()
    assert "Installed." not in result.stdout


def test_path_cli_fallback_activates_provider(installer):
    run, hermes, _, commands, home, _, _ = installer
    hermes(commands / "hermes")
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "provider: mnemosyne" in (home / "config.yaml").read_text()


def test_fresh_config_fallback_without_cli(installer):
    run, _, _, _, home, _, _ = installer
    config = home / "config.yaml"
    config.unlink()
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_text() == "memory:\n  provider: mnemosyne\n"


def test_dry_run_does_not_mutate_without_cli(installer):
    run, _, _, _, home, installs, _ = installer
    before = (home / "config.yaml").read_bytes()
    result = run("--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "config.yaml").read_bytes() == before
    assert not installs.exists()
    assert not (home / "plugins").exists()


def test_package_manager_owns_provider_dependencies_without_uv_injection(installer):
    run, hermes, venv, _, home, installs, hermes_calls = installer
    hermes_path = venv / "bin/hermes"
    _script(
        hermes_path,
        'printf "%s\\n" "$0 $*" >> "$TEST_HERMES_LOG"\n'
        'if [[ "${1:-}" == "pm" && "${2:-}" == "status" ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "pm" && "${2:-}" == "install" ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "config" && "${2:-}" == "set" ]]; then '
        'printf "memory:\\n  provider: %s\\n" "$4" > "$HERMES_HOME/config.yaml"; exit 0; fi\n',
    )

    # The fake PM environment uses the fixture venv itself as its selected
    # generation, matching the current install record's verified layout.
    (venv.parent / "pm").mkdir()
    (venv.parent / "pm/environments.py").write_text(
        "# fake Hermes PM install root\n", encoding="utf-8"
    )
    install_key = __import__("hashlib").sha256(str(venv.parent.resolve()).encode()).hexdigest()[:16]
    state = home / "installs" / install_key
    environment = state / "environments" / "gen-fixture"
    # The installer reads the interpreter from the platform's venv layout.
    selected_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    selected_python.parent.mkdir(parents=True)
    shutil.copy(venv / "bin/python", selected_python)  # keeps the exec bit
    (environment / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
    import json

    (state / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(environment)}}}),
        encoding="utf-8",
    )
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not installs.exists(), "Hermes PM owns dependency installation"
    calls = hermes_calls.read_text(encoding="utf-8")
    assert "config set memory.provider mnemosyne" in calls
    assert "pm install" in calls
    assert "provider: mnemosyne" in (home / "config.yaml").read_text()
    assert "Hermes loader selected deployed provider class" in result.stdout

    # A bundled same-name plugin wins in Hermes. The installer must refuse to
    # claim success when the actual loader would use that class instead.
    bundled = home / "hermes-agent/plugins/memory/mnemosyne"
    bundled.mkdir(parents=True)
    (bundled / "__init__.py").write_text("class WrongProvider: pass\n", encoding="utf-8")
    collision = run()
    assert collision.returncode != 0, collision.stdout + collision.stderr
    assert "loader does not resolve mnemosyne" in collision.stdout + collision.stderr


def test_pm_rebuild_reapplies_audited_patches_to_selected_generation(installer):
    """Issue #55 regression: a Hermes PM dependency rebuild selects a fresh
    generation whose engine wheel does not contain the audited patches; the
    supported workflow (re-running ./install.sh) must re-apply the audited
    patch set to the newly selected generation before reporting ready."""
    import difflib
    import hashlib
    import json

    run, hermes, venv, _, home, installs, hermes_calls = installer
    repo = run.repo

    # Give the fixture the real audited applier and a fixture patch set: the
    # upstream fixture engine lacks seed_config, the audited patch adds it —
    # the same role the shipped manifest plays for the engine's P31.
    shutil.copyfile(
        ROOT / "scripts/apply_engine_patches.py",
        repo / "scripts/apply_engine_patches.py",
    )
    patches = repo / "integrations/engine-patches"
    patches.mkdir()
    engine_memory = run.engine / "memory.py"
    original = b"class Mnemosyne:\n    def __init__(self):\n        pass\n"
    patched = engine_memory.read_bytes()

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    delta = "".join(
        difflib.unified_diff(
            original.decode().splitlines(True),
            patched.decode().splitlines(True),
            fromfile="a/core/memory.py",
            tofile="b/core/memory.py",
        )
    ).encode()
    (patches / "core-memory.py.patch").write_bytes(delta)
    manifest = {
        "distribution": "mnemosyne-memory",
        "version": "3.15.1",
        "files": {
            "core/memory.py": {
                "original_sha256": digest(original),
                "patched_sha256": digest(patched),
                "patch": "core-memory.py.patch",
                "patch_sha256": digest(delta),
            }
        },
    }
    (patches / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Fake Hermes PM: status succeeds, install admits the union, config set works.
    hermes_path = venv / "bin/hermes"
    _script(
        hermes_path,
        'printf "%s\\n" "$0 $*" >> "$TEST_HERMES_LOG"\n'
        'if [[ "${1:-}" == "pm" && "${2:-}" == "status" ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "pm" && "${2:-}" == "install" ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "config" && "${2:-}" == "set" ]]; then '
        'printf "memory:\\n  provider: %s\\n" "$4" > "$HERMES_HOME/config.yaml"; exit 0; fi\n',
    )
    (venv.parent / "pm").mkdir()
    (venv.parent / "pm/environments.py").write_text(
        "# fake Hermes PM install root\n", encoding="utf-8"
    )
    install_key = hashlib.sha256(str(venv.parent.resolve()).encode()).hexdigest()[:16]
    state = home / "installs" / install_key
    environment = state / "environments" / "gen-fixture"
    selected_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    selected_python.parent.mkdir(parents=True)
    shutil.copy(venv / "bin/python", selected_python)
    (environment / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
    (state / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(environment)}}}),
        encoding="utf-8",
    )

    first = run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert not installs.exists(), "Hermes PM owns dependency installation"
    assert "already patched: core/memory.py" in first.stdout

    # The rebuild: PM admits a fresh generation shipping the upstream wheel.
    engine_memory.write_bytes(original)
    assert "seed_config" not in engine_memory.read_text(encoding="utf-8")

    rebuilt = run()
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    assert "Installed." in rebuilt.stdout
    # The newly selected generation is patched before the installer reports ready.
    assert "patch: core/memory.py" in rebuilt.stdout
    assert "seed_config" in engine_memory.read_text(encoding="utf-8")
    backup = engine_memory.with_name("memory.py.mnemosyne-hermes-original")
    assert backup.read_bytes() == original


def test_failed_copy_preserves_existing_verified_provider(installer):
    run, hermes, venv, _commands, home, _installs, _calls = installer
    hermes(venv / "bin/hermes")
    first = run("--copy")
    assert first.returncode == 0, first.stdout + first.stderr
    installed = home / "plugins" / "mnemosyne"
    old_init = (installed / "__init__.py").read_bytes()
    old_provenance = (installed / "PROVENANCE.json").read_bytes()

    second = run("--copy", fail_commands=("cp",))
    assert second.returncode != 0
    assert (installed / "__init__.py").read_bytes() == old_init
    assert (installed / "PROVENANCE.json").read_bytes() == old_provenance
    assert not list((home / "plugins").glob(".mnemosyne-install.*"))


def test_failed_post_promotion_verification_restores_previous_provider_and_config(installer):
    run, hermes, venv, _commands, home, _installs, _calls = installer
    hermes(venv / "bin/hermes")
    first = run("--copy")
    assert first.returncode == 0, first.stdout + first.stderr
    installed = home / "plugins" / "mnemosyne"
    previous_init = (installed / "__init__.py").read_bytes()
    previous_provenance = (installed / "PROVENANCE.json").read_bytes()

    (home / "config.yaml").write_text("memory:\n  provider: local\n", encoding="utf-8")
    source_init = run.provider_source
    source_init.write_text(
        source_init.read_text(encoding="utf-8").replace(
            "def is_available(self): return True", "def is_available(self): return False"
        ),
        encoding="utf-8",
    )
    result = run("--copy")

    assert result.returncode != 0, result.stdout + result.stderr
    assert (installed / "__init__.py").read_bytes() == previous_init
    assert (installed / "PROVENANCE.json").read_bytes() == previous_provenance
    assert "provider: local" in (home / "config.yaml").read_text(encoding="utf-8")
    assert not list((home / "plugins").glob(".mnemosyne-install.*"))


def test_copied_install_uninstalls_cleanly(installer):
    """A copied install must be removable.

    verify_installed_copy loads the provider's cli.py to call _verified_copy.
    cli.py imports its siblings relatively, so loading it as a bare file raised
    "attempted relative import with no known parent package" and the uninstall
    refused every copied install ("not an intact copy created by this
    installer"). The relative import in the fixture reproduces that.
    """
    run, hermes, venv, _commands, home, _installs, _calls = installer
    hermes(venv / "bin/hermes")
    installed = run("--copy")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    plugin = home / "plugins" / "mnemosyne"
    assert (plugin / "PROVENANCE.json").is_file()

    result = run("--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not plugin.exists()
    assert "removed verified plugin copy" in result.stdout


def test_live_installer_lock_refuses_without_mutating_home(installer):
    run, hermes, venv, _commands, home, installs, _calls = installer
    hermes(venv / "bin/hermes")
    lock = home / ".mnemosyne-installer.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()), encoding="ascii")
    before = (home / "config.yaml").read_bytes()
    result = run()
    assert result.returncode != 0
    assert "another Mnemosyne installer is running" in result.stderr
    assert (home / "config.yaml").read_bytes() == before
    assert not installs.exists()
    assert not (home / "plugins").exists()
    assert lock.is_dir()


def test_installer_recovers_dead_owner_lock(installer):
    run, hermes, venv, _commands, home, _installs, _calls = installer
    hermes(venv / "bin/hermes")
    lock = home / ".mnemosyne-installer.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999999", encoding="ascii")
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not lock.exists()
    assert (home / "plugins" / "mnemosyne" / "__init__.py").is_file()


def test_concurrent_installs_serialize_the_shared_hermes_home(installer):
    run, hermes, venv, _commands, home, _installs, _calls = installer
    hermes(venv / "bin/hermes")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: run("--copy"), range(2)))
    assert all(
        result.returncode == 0 or "another Mnemosyne installer is running" in result.stderr
        for result in results
    )
    installed = home / "plugins" / "mnemosyne"
    assert (installed / "__init__.py").is_file()
    assert (installed / "PROVENANCE.json").is_file()
    assert not (home / ".mnemosyne-installer.lock").exists()
