"""Loader-level smoke test for the vendored Hermes provider.

Two cases, because both have failed in production:

1. **No engine** (a pip-less or gateway venv where `mnemosyne-memory` was never
   installed). The provider must still import, must report `is_available()` False
   *with a reason*, and must register exactly one provider named `mnemosyne`.
   Before the local patches the module raised ImportError here, the loader
   returned None, and the provider could not even say "unavailable".
2. **Engine present**. Exactly one provider registers, and it reports available.

Case 1 runs everywhere (the engine is blocked in-process). Case 2 is skipped
when the engine is not importable, so this file is safe in a bare CI venv.

    python tests/test_provider_loader.py
    pytest tests/test_provider_loader.py
"""
import importlib.util
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROVIDER_DIR = ROOT / "integrations" / "hermes-provider" / "hermes_memory_provider"


def _load_as_loader_does(module_name):
    """Import the plugin the way hermes-agent/plugins/memory does."""
    spec = importlib.util.spec_from_file_location(
        module_name, str(PROVIDER_DIR / "__init__.py"),
        submodule_search_locations=[str(PROVIDER_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    for child in sorted(PROVIDER_DIR.glob("*.py")):
        if child.name == "__init__.py":
            continue
        sub_name = f"{module_name}.{child.stem}"
        if sub_name not in sys.modules:
            sub = importlib.util.spec_from_file_location(sub_name, str(child))
            sub_mod = importlib.util.module_from_spec(sub)
            sys.modules[sub_name] = sub_mod
            try:
                sub.loader.exec_module(sub_mod)
            except Exception:
                # Optional extras (sync/persona schemas) degrade to [] upstream.
                pass
    spec.loader.exec_module(mod)
    return mod


class _Collector:
    """Fake plugin context, mirroring the loader's _ProviderCollector."""

    def __init__(self):
        self.providers = []
        self.cli = []

    def register_memory_provider(self, provider):
        self.providers.append(provider)

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs.get("name"))

    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass


def test_module_imports_and_reports_unavailable_without_engine():
    """Run in a subprocess whose `mnemosyne` import is blocked."""
    code = f'''
import sys, importlib.util
sys.path.insert(0, {str(ROOT / "integrations" / "hermes-provider")!r})

class _BlockEngine:
    """Make `mnemosyne` unimportable, whatever the environment provides."""
    def find_spec(self, name, path=None, target=None):
        if name == "mnemosyne" or name.startswith("mnemosyne."):
            raise ImportError("blocked for the bare-venv smoke test")
        return None

sys.meta_path.insert(0, _BlockEngine())
sys.modules.pop("mnemosyne", None)

provider_dir = {str(PROVIDER_DIR)!r}
spec = importlib.util.spec_from_file_location(
    "_hermes_user_memory.mnemosyne", provider_dir + "/__init__.py",
    submodule_search_locations=[provider_dir])
mod = importlib.util.module_from_spec(spec)
sys.modules["_hermes_user_memory.mnemosyne"] = mod
spec.loader.exec_module(mod)          # must NOT raise

collected = []
class Ctx:
    def register_memory_provider(self, p): collected.append(p)
    def register_cli_command(self, **kw): pass
    def register_tool(self, *a, **k): pass
    def register_hook(self, *a, **k): pass

mod.register(Ctx())
assert len(collected) == 1, f"expected one provider, got {{len(collected)}}"
p = collected[0]
assert p.name == "mnemosyne", p.name
assert p.is_available() is False, "must not claim availability without the engine"
reason = p.unavailable_reason()
assert "mnemosyne-memory" in reason and "3.15" in reason, reason

# `hermes mnemosyne doctor` must FAIL, not print a smiling report. The Hermes
# CLI ignores a handler's return value, so doctor signals failure by raising
# SystemExit (T4).
import types
from hermes_memory_provider import cli
try:
    rc = cli.mnemosyne_command(types.SimpleNamespace(
        mnemosyne_cmd="doctor", no_fix=True, dry_run=True))
except SystemExit as exc:
    rc = exc.code
assert rc == 1, f"doctor must fail without the engine, got {{rc}}"
print("OK", reason)
'''
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (
        f"bare-venv import/registration failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "OK mnemosyne-memory engine not importable" in proc.stdout, proc.stdout


def test_engine_present_registers_one_available_provider():
    # Guard on the ENGINE, not on `import mnemosyne`: this repo's own
    # src/mnemosyne package shadows the engine's `mnemosyne` whenever src/ is on
    # sys.path (e.g. after tests/test_python_hardening.py has imported lib), and
    # the engine is what the provider actually needs. That shadowing is a real
    # packaging collision — see integrations/hermes-provider/README.md
    # ("Do not install this repo's mnemosyne package into the Hermes venv").
    try:
        import mnemosyne.core.beam  # noqa: F401
    except ImportError:
        print("skip: engine not importable in this process (bare-venv case covers it)")
        return

    mod = _load_as_loader_does("_hermes_user_memory.mnemosyne_engine")
    ctx = _Collector()
    mod.register(ctx)
    assert len(ctx.providers) == 1, f"expected exactly one provider, got {len(ctx.providers)}"
    assert ctx.cli == ["mnemosyne"], ctx.cli
    provider = ctx.providers[0]
    assert provider.name == "mnemosyne"
    assert provider.is_available() is True, provider.unavailable_reason()
    # A second registration path must not produce a second provider.
    ctx2 = _Collector()
    mod.register_memory_provider(ctx2)
    assert len(ctx2.providers) == 1


def test_patched_hook_signatures_and_accept_and_store():
    """F1/F2 local patches: the hooks accept what Hermes offers, and store it.

    Hermes introspects both signatures (MemoryManager._provider_sync_accepts_messages,
    _provider_memory_write_metadata_mode) and only passes the richer payload to a
    provider that declares it — upstream declared neither, so `messages` was
    never sent and `metadata` was dropped.
    """
    import inspect
    mod = _load_as_loader_does("_hermes_user_memory.mnemosyne_hooks")
    provider = mod.MnemosyneMemoryProvider()

    # 1. Signatures the manager introspects.
    assert "messages" in inspect.signature(provider.sync_turn).parameters
    assert "metadata" in inspect.signature(provider.on_memory_write).parameters

    # 2. When hermes-agent is importable, assert against its real helpers.
    try:
        from agent.memory_manager import MemoryManager
    except ImportError:
        print("skip: hermes-agent not importable for the introspection cross-check")
    else:
        assert MemoryManager._provider_sync_accepts_messages(provider) is True
        assert MemoryManager._provider_memory_write_metadata_mode(provider) == "keyword"

    class FakeBeam:
        def __init__(self):
            self.calls = []

        def remember(self, **kwargs):
            self.calls.append(kwargs)
            return "fake-id"

    # 3. F2 stores metadata instead of dropping it.
    provider._beam = FakeBeam()
    provider.on_memory_write("add", "user", "prefers local storage", {"origin": "builtin"})
    assert provider._beam.calls[-1]["metadata"] == {"origin": "builtin"}

    # 4. F1 stores tool turns only when the operator opts in via sync_roles.
    turns = [
        {"role": "user", "content": "run the tests"},
        {"role": "tool", "name": "bash", "content": "21 tests OK"},
        {"role": "tool", "name": "bash", "content": "   "},
    ]
    provider._beam = FakeBeam()
    provider._sync_roles = {"user"}
    provider.sync_turn("run the tests", "they passed", session_id="s", messages=turns)
    assert "conversation_tool" not in [c["source"] for c in provider._beam.calls]

    provider._beam = FakeBeam()
    provider._sync_roles = {"tool"}          # isolates the patched branch
    provider.sync_turn("run the tests", "they passed", session_id="s", messages=turns)
    stored = provider._beam.calls
    assert [c["source"] for c in stored] == ["conversation_tool"], stored
    assert stored[0]["content"] == "[TOOL] 21 tests OK"
    assert stored[0]["metadata"] == {"role": "tool", "name": "bash"}


if __name__ == "__main__":
    tests = [test_module_imports_and_reports_unavailable_without_engine,
             test_engine_present_registers_one_available_provider,
             test_patched_hook_signatures_and_accept_and_store]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
