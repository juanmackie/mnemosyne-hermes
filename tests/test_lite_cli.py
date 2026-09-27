"""CLI contract tests for the lite surface.

Each test is the regression for one reported bug:

* `backup` crashed with `TypeError` when neither `--db-path` nor
  `MNEMOSYNE_DB_PATH` was set, because it read `args.db_path` (None) directly.
* `bootstrap`'s facts/policies/guardrails/skills lists were always empty: they
  filtered on a `memory_type` column this store does not have.
* `restore` overwrote the live store with no confirmation, no safety copy and no
  check that the source was a Mnemosyne store, and replayed a `.gz` dump
  straight onto the destination, colliding with its tables.
* the CLI reported the *engine's* version (it fell back to the `mnemosyne`
  distribution) and the MCP server announced itself as `mnemosyne` 2.4.0.

These drive `cli.main(argv)` end to end against real files on disk, so they
exercise the argument parser, path resolution, the store and the output.

Runs two ways:

    python tests/test_lite_cli.py
    pytest tests/test_lite_cli.py
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mnemosyne_lite  # noqa: E402
from lib.storage import PythonMemoryStorage  # noqa: E402


def _load_cli():
    """Load src/mnemosyne_lite/cli.py the way the console script entry point does."""
    spec = importlib.util.spec_from_file_location(
        "mnemosyne_lite_cli_under_test", str(ROOT / "src" / "mnemosyne_lite" / "cli.py")
    )
    assert spec is not None and spec.loader is not None, "no module spec for the CLI"
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


CLI = _load_cli()


@contextlib.contextmanager
def _env(**pairs):
    """Set environment variables for the block, then put them back."""
    saved = {k: os.environ.get(k) for k in pairs}
    for key, value in pairs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(argv, stdin_text=""):
    """Call the CLI; return (exit code, stdout, stderr).

    stdin is an empty stream, so a confirmation prompt reads EOF and answers
    "no" unless `--yes` is passed or `stdin_text` supplies an answer.
    """
    out, err = io.StringIO(), io.StringIO()
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = CLI.main(argv)
    finally:
        sys.stdin = saved_stdin
    return code, out.getvalue(), err.getvalue()


def _contents(db):
    s = PythonMemoryStorage(db)
    try:
        return [m["content"] for m in s.list_memories(limit=100)]
    finally:
        s.close()


def _foreign_store(path):
    """A SQLite file with a `memories` table this project must refuse."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, created_at REAL)")
        con.execute("INSERT INTO memories VALUES ('a', 'not ours', 1.0)")
        con.commit()
    finally:
        con.close()
    return path


def test_backup_without_db_path_reports_a_missing_store():
    """`backup` used to die with TypeError when no path was configured."""
    with tempfile.TemporaryDirectory() as d:
        missing = str(pathlib.Path(d) / "nope.db")
        with _env(MNEMOSYNE_DB_PATH=missing):
            code, _, err = _run(["backup"])
    assert code == 1, (code, err)
    assert "no Mnemosyne database" in err, err
    assert "NoneType" not in err, err


def test_backup_writes_a_usable_copy():
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "memory.db")
        dest = str(pathlib.Path(d) / "copy.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        assert (
            _run(["--db-path", db, "remember", "--content", "backup me", "--namespace", "ns"])[0]
            == 0
        )
        code, _, err = _run(["--db-path", db, "backup", "--output", dest])
        assert code == 0, (code, err)
        assert _contents(dest) == ["backup me"]


def test_bootstrap_does_not_print_categories_it_cannot_answer():
    """The four category lists filtered on a column that does not exist."""
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "memory.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        assert (
            _run(["--db-path", db, "remember", "--content", "a fact", "--namespace", "ns"])[0] == 0
        )
        code, out, err = _run(["--db-path", db, "bootstrap", "--namespace", "ns"])
        assert code == 0, (code, err)
        for absent in ("facts", "policies", "guardrails", "skills"):
            assert absent not in out, out
        assert "provenance" in out, out
        assert "abstentions" in out, out


def test_restore_refuses_a_non_store_and_leaves_the_destination_alone():
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "memory.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        assert (
            _run(["--db-path", db, "remember", "--content", "keep me", "--namespace", "ns"])[0] == 0
        )
        before = pathlib.Path(db).read_bytes()

        junk = _foreign_store(str(pathlib.Path(d) / "junk.db"))
        code, _, err = _run(["--db-path", db, "restore", "--backup", junk, "--yes"])
        assert code == 1, (code, err)
        assert "not a Mnemosyne store" in err, err
        assert pathlib.Path(db).read_bytes() == before, "the live store was modified"
        assert _contents(db) == ["keep me"]


def test_restore_needs_confirmation_and_yes_skips_it():
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "memory.db")
        source = str(pathlib.Path(d) / "source.db")
        assert _run(["--db-path", source, "init"])[0] == 0
        assert (
            _run(["--db-path", source, "remember", "--content", "restored", "--namespace", "ns"])[0]
            == 0
        )
        assert _run(["--db-path", db, "init"])[0] == 0
        assert (
            _run(["--db-path", db, "remember", "--content", "original", "--namespace", "ns"])[0]
            == 0
        )

        # No --yes and an empty stdin: the prompt reads EOF and must cancel.
        code, out, _ = _run(["--db-path", db, "restore", "--backup", source])
        assert code == 1, code
        assert "Cancelled" in out, out
        assert _contents(db) == ["original"]

        # An explicit "y" is enough.
        code, _, err = _run(["--db-path", db, "restore", "--backup", source], stdin_text="y\n")
        assert code == 0, (code, err)
        assert _contents(db) == ["restored"]

        # And --yes skips the prompt entirely.
        assert (
            _run(["--db-path", db, "remember", "--content", "second", "--namespace", "ns"])[0] == 0
        )
        code, _, err = _run(["--db-path", db, "restore", "--backup", source, "--yes"])
        assert code == 0, (code, err)
        assert _contents(db) == ["restored"]


def test_restore_takes_a_gzipped_dump_and_writes_a_safety_copy():
    import gzip

    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "memory.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        assert (
            _run(["--db-path", db, "remember", "--content", "original", "--namespace", "ns"])[0]
            == 0
        )

        dump = pathlib.Path(d) / "dump.sql.gz"
        with gzip.open(dump, "wt", encoding="utf-8") as fh:
            fh.write("BEGIN;\n")
            fh.write(
                "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, "
                "namespace TEXT NOT NULL, importance INTEGER DEFAULT 5, context TEXT, "
                "summary TEXT, keywords TEXT, created_at REAL DEFAULT 0, "
                "access_count INTEGER DEFAULT 0, last_accessed REAL);\n"
            )
            fh.write(
                "INSERT INTO memories (id, content, namespace, importance, created_at) "
                "VALUES ('r1', 'from the dump', 'ns', 5, 1.0);\n"
            )
            fh.write("COMMIT;\n")

        code, out, err = _run(["--db-path", db, "restore", "--backup", str(dump), "--yes"])
        assert code == 0, (code, err)
        assert _contents(db) == ["from the dump"]
        assert "Safety backup:" in out, out
        safety = [p for p in pathlib.Path(d).glob("memory.db.pre-restore.*")]
        assert safety, "no safety copy was written"
        assert _contents(str(safety[0])) == ["original"], "the safety copy is not the old store"
        # The staged temp file must not survive.
        assert not list(pathlib.Path(d).glob("memory.db.restore-tmp.*"))


def test_version_labels_come_from_this_package():
    assert CLI._package_version() == mnemosyne_lite.__version__
    assert CLI._package_version() != "unknown"


def test_mcp_announces_itself_as_mnemosyne_lite():
    from mnemosyne_lite.mcp import serve

    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "memory.db")
        assert _run(["--db-path", db, "init"])[0] == 0
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        out = io.StringIO()
        assert serve(db, stdin=io.StringIO(request + "\n"), stdout=out) == 0
        info = json.loads(out.getvalue())["result"]["serverInfo"]
        assert info["name"] == "mnemosyne-lite", info
        assert info["version"] == mnemosyne_lite.__version__, info


if __name__ == "__main__":
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
