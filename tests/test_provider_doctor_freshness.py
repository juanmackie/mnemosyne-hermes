"""P56 doctor freshness note: engine-free coverage of the staleness warning.

The note is informational — it must never change doctor's exit status, and
every unreadable input must fail soft to silence. The pure mtime comparison
is tested directly; the /proc wrapper is exercised against this live test
process on Linux and skipped elsewhere.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integrations/hermes-provider"))

from hermes_memory_provider import cli  # noqa: E402


def test_mtime_comparison_and_pycache_exclusion():
    with tempfile.TemporaryDirectory() as tmp:
        engine = Path(tmp)
        src = engine / "core.py"
        src.write_text("x = 1\n")
        now = time.time()
        os.utime(src, (now - 500, now - 500))
        assert cli._engine_newer_than_boot(engine, now) is False
        os.utime(src, (now + 500, now + 500))
        assert cli._engine_newer_than_boot(engine, now) is True
        # __pycache__ churn from imports must not trigger the note.
        os.utime(src, (now - 500, now - 500))
        cache = engine / "__pycache__"
        cache.mkdir()
        pyc = cache / "core.cpython-313.pyc"
        pyc.write_bytes(b"\x00")
        os.utime(pyc, (now + 500, now + 500))
        assert cli._engine_newer_than_boot(engine, now) is False
        # An empty directory has no engine sources to be stale.
        assert cli._engine_newer_than_boot(Path(tmp) / "missing", now) is False


def test_missing_or_garbage_gateway_record_is_silent():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        engine = home / "engine"
        engine.mkdir()
        (engine / "core.py").write_text("x = 1\n")
        assert cli._gateway_engine_freshness_note(home, engine) is None
        (home / "gateway.pid").write_text("not json")
        assert cli._gateway_engine_freshness_note(home, engine) is None
        (home / "gateway.pid").write_text(json.dumps({"pid": 0}))
        assert cli._gateway_engine_freshness_note(home, engine) is None
        (home / "gateway.pid").write_text(json.dumps({"pid": -5}))
        assert cli._gateway_engine_freshness_note(home, engine) is None


def test_live_process_note_on_linux():
    if not Path("/proc/self/stat").exists():
        return  # non-Linux: the wrapper fails soft, covered above
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        engine = home / "engine"
        engine.mkdir()
        src = engine / "core.py"
        src.write_text("x = 1\n")
        (home / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}))
        # Engine touched now; this test process booted earlier.
        note = cli._gateway_engine_freshness_note(home, engine)
        assert note is not None and "restart" in note, note
        # Engine older than any live process: no note.
        os.utime(src, (1, 1))
        assert cli._gateway_engine_freshness_note(home, engine) is None


def test_dead_pid_is_silent():
    if not Path("/proc/self/stat").exists():
        return
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        engine = home / "engine"
        engine.mkdir()
        (engine / "core.py").write_text("x = 1\n")
        # PID 2^22 is above the default pid_max; virtually never alive.
        (home / "gateway.pid").write_text(json.dumps({"pid": 4194304}))
        assert cli._gateway_engine_freshness_note(home, engine) is None


if __name__ == "__main__":
    tests = [
        test_mtime_comparison_and_pycache_exclusion,
        test_missing_or_garbage_gateway_record_is_silent,
        test_live_process_note_on_linux,
        test_dead_pid_is_silent,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
