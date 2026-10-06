"""Describe + cards regression (Leviathan-inspired agent layer).

Red: these fail until `describe`, `--format cards`, and card `text`
exist. They pin the additive contracts:

* `describe` reports what an agent needs before searching and never
  fabricates a store at a missing path.
* `recall --format cards` keeps `text` (table) and `json` intact and adds
  a capped, cited card view with `shown N of M` so "no match" stays
  distinct from "no data". Truncation is always marked.
* MCP search/prefetch keep `structuredContent` and add card `text`.
"""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mnemosyne_lite.cli as CLI  # noqa: E402
from mnemosyne_lite.storage import PythonMemoryStorage  # noqa: E402


def _run(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = CLI.main(argv)
    finally:
        sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


def test_describe_json_reports_counts_and_examples():
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "m.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        s = PythonMemoryStorage(db)
        s.remember("the user prefers local storage", "ns-a", 8)
        s.remember("unrelated note", "ns-b", 3)
        s.close()
        code, out, err = _run(["--db-path", db, "describe", "--format", "json"])
        assert code == 0, (code, err)
        doc = json.loads(out)
        assert doc["memory_count"] == 2, doc
        assert doc["namespaces"] == {"ns-a": 1, "ns-b": 1}, doc
        assert "example_calls" in doc and doc["example_calls"], doc


def test_describe_refuses_a_missing_store():
    with tempfile.TemporaryDirectory() as d:
        missing = str(pathlib.Path(d) / "nope.db")
        code, _, err = _run(["--db-path", missing, "describe"])
        assert code == 1, (code, err)
        assert "nothing was created" in err, err
        assert not os.path.exists(missing)


def test_recall_cards_format_marks_shown_and_truncation():
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "m.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        long_content = "storage " + ("x" * 2000)
        assert _run(["--db-path", db, "remember", "--content", long_content])[0] == 0
        code, out, err = _run(
            ["--db-path", db, "recall", "--query", "storage", "--format", "cards"]
        )
        assert code == 0, (code, err)
        assert "shown 1 of 1" in out, out
        assert "match:" in out, out
        # Truncation must be marked, never silent.
        assert "…" in out, out


def test_recall_cards_zero_hits_is_not_an_error():
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "m.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        code, out, err = _run(
            ["--db-path", db, "recall", "--query", "zzzqqqnomatch", "--format", "cards"]
        )
        assert code == 0, (code, err)
        assert "shown 0 of 0" in out, out


def test_text_and_json_recall_contracts_still_hold():
    """The new format must not drift the frozen text/JSON shapes."""
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "m.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        assert _run(["--db-path", db, "remember", "--content", "json please"])[0] == 0
        code, out, err = _run(["--db-path", db, "recall", "--query", "json"])
        assert code == 0, (code, err)
        lines = out.splitlines()
        assert len(lines) == 3 and "CONTENT" in lines[0], out
        code, out, err = _run(["--db-path", db, "recall", "--query", "json", "--format", "json"])
        assert code == 0, (code, err)
        assert isinstance(json.loads(out), list), out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
