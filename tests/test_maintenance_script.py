"""Maintenance-suite regressions: quiet success, loud failure, safe snapshots.

Every test runs the real script in a subprocess against a throwaway database, so
what is asserted is the exit code and the filesystem effect a cron job sees —
not an in-process call that could pass while the CLI is broken.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "maintenance" / "mnemosyne-maintain.py"


def make_db(path: pathlib.Path, rows: int = 200) -> pathlib.Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    con.executemany(
        "INSERT INTO memories (content) VALUES (?)", [(f"memory {i}" * 40,) for i in range(rows)]
    )
    con.commit()
    con.close()
    return path


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_verify_ok_is_silent(tmp_path):
    db = make_db(tmp_path / "mnemosyne.db")
    result = run("verify", "--db-path", str(db))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_verify_missing_db_fails_without_creating_it(tmp_path):
    db = tmp_path / "absent.db"
    result = run("verify", "--db-path", str(db))
    assert result.returncode == 1
    assert "no database" in result.stderr
    assert not db.exists(), "a read-only command must never invent a database"


def test_backup_is_self_contained_and_row_identical(tmp_path):
    db = make_db(tmp_path / "mnemosyne.db", rows=300)
    backups = tmp_path / "backups"
    result = run("backup", "--db-path", str(db), "--backup-dir", str(backups))
    assert result.returncode == 0
    assert result.stdout.strip() == ""

    made = sorted(backups.iterdir())
    assert len(made) == 1
    snapshot = made[0]
    assert snapshot.suffix == ".db"
    assert not list(backups.glob("*-wal")), "snapshot must not depend on a sidecar WAL"

    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 300
    finally:
        con.close()


def test_backup_missing_db_is_a_silent_no_op(tmp_path):
    result = run(
        "backup", "--db-path", str(tmp_path / "absent.db"), "--backup-dir", str(tmp_path / "bk")
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_backup_retention_keeps_newest_and_touches_nothing_else(tmp_path):
    db = make_db(tmp_path / "mnemosyne.db", rows=5)
    backups = tmp_path / "backups"
    backups.mkdir()
    keep = backups / "manually-placed.db"
    keep.write_bytes(b"not ours")

    for _ in range(4):
        result = run("backup", "--db-path", str(db), "--backup-dir", str(backups), "--keep", "2")
        assert result.returncode == 0, result.stderr

    remaining = [p for p in backups.iterdir() if p.name.startswith("mnemosyne-")]
    assert len(remaining) == 2
    assert keep.exists(), "pruning must only ever remove snapshots this tool wrote"


def test_same_second_snapshots_do_not_overwrite(tmp_path):
    """Two backups in the same wall-clock second must both survive."""
    db = make_db(tmp_path / "mnemosyne.db", rows=5)
    backups = tmp_path / "backups"
    for _ in range(3):
        result = run("backup", "--db-path", str(db), "--backup-dir", str(backups), "--no-prune")
        assert result.returncode == 0, result.stderr
    made = sorted(p.name for p in backups.iterdir())
    assert len(made) == 3, made
    assert len(set(made)) == 3
    for name in made:
        assert name.endswith(".db") and ".partial" not in name


def test_compact_skips_below_threshold_silently(tmp_path):
    db = make_db(tmp_path / "mnemosyne.db")
    result = run("compact", "--db-path", str(db), "--backup-dir", str(tmp_path / "bk"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_compact_force_reclaims_space_and_keeps_integrity(tmp_path):
    db = make_db(tmp_path / "mnemosyne.db", rows=2000)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM memories WHERE id % 2 = 0")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    before = db.stat().st_size

    backups = tmp_path / "bk"
    result = run("compact", "--db-path", str(db), "--backup-dir", str(backups), "--force")
    assert result.returncode == 0, result.stderr
    assert "->" in result.stdout
    assert db.stat().st_size < before

    checkpoint = list(backups.glob("*-precompact.db"))
    assert len(checkpoint) == 1, "compaction must leave a verified pre-VACUUM checkpoint"
    con = sqlite3.connect(f"file:{checkpoint[0]}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1000
    finally:
        con.close()


def test_engine_commands_refuse_a_non_standard_filename(tmp_path):
    db = make_db(tmp_path / "custom.db")
    result = run("consolidate", "--db-path", str(db))
    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert not (tmp_path / "mnemosyne.db").exists(), (
        "engine guard must prevent an invented database"
    )


def test_engine_commands_refuse_to_create_a_missing_database(tmp_path):
    db = tmp_path / "mnemosyne.db"
    result = run("hygiene", "--db-path", str(db))
    assert result.returncode == 0
    assert not db.exists()


def test_report_is_one_line_with_integrity(tmp_path):
    db = make_db(tmp_path / "mnemosyne.db", rows=42)
    result = run("report", "--db-path", str(db))
    assert result.returncode == 0
    assert result.stdout.count("\n") == 1
    assert "integrity=ok" in result.stdout
    assert "memories=42" in result.stdout


def test_config_db_path_is_honoured_without_a_yaml_library(tmp_path):
    home = tmp_path / "hermes"
    (home / "mnemosyne" / "data").mkdir(parents=True)
    db = make_db(home / "mnemosyne" / "data" / "configured.db", rows=7)
    (home / "config.yaml").write_text(
        "memory:\n"
        "  provider: mnemosyne\n"
        "  mnemosyne:\n"
        f'    db_path: "{db}"\n'
        "delegation:\n"
        "  db_path: /wrong/place.db\n",
        encoding="utf-8",
    )
    result = run("report", "--hermes-home", str(home))
    assert result.returncode == 0
    assert str(db) in result.stdout
    assert "memories=7" in result.stdout


def test_cli_db_path_beats_config(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    configured = make_db(home / "configured.db", rows=1)
    explicit = make_db(tmp_path / "explicit.db", rows=9)
    (home / "config.yaml").write_text(
        f"memory:\n  mnemosyne:\n    db_path: {configured}\n", encoding="utf-8"
    )
    result = run("report", "--hermes-home", str(home), "--db-path", str(explicit))
    assert result.returncode == 0
    assert str(explicit) in result.stdout
    assert "memories=9" in result.stdout


def test_corrupt_database_fails_verify(tmp_path):
    db = tmp_path / "mnemosyne.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200 + b"garbage" * 100)
    result = run("verify", "--db-path", str(db))
    assert result.returncode == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_unique_snapshot_path_reservation_is_atomic_under_threads(tmp_path):
    import importlib.util
    import threading

    spec = importlib.util.spec_from_file_location("mnemosyne_maintain", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    target = tmp_path / "same-second.db"
    barrier = threading.Barrier(12)
    paths = []
    errors = []

    def reserve():
        try:
            barrier.wait()
            paths.append(module.unique_path(target))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=reserve) for _ in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert not errors
    assert len(set(paths)) == 12
    assert all(path.is_file() for path in paths)


# --------------------------------------------------------------------------- #
# Config safety: never silently act on a different database
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("quote", ['"', "'"])
def test_config_db_path_keeps_hash_inside_quotes(tmp_path, quote):
    """A quoted path containing '#' is data; only the trailing comment is dropped."""
    home = tmp_path / "hermes"
    store = home / "db#archive"
    store.mkdir(parents=True)
    db = make_db(store / "mnemosyne.db", rows=5)
    (home / "config.yaml").write_text(
        f"memory:\n  mnemosyne:\n    db_path: {quote}{db}{quote}  # set by the installer\n",
        encoding="utf-8",
    )
    result = run("report", "--hermes-home", str(home))
    assert result.returncode == 0, result.stderr
    assert str(db) in result.stdout
    assert "memories=5" in result.stdout


def test_config_db_path_keeps_unquoted_hash_without_a_space(tmp_path):
    """YAML starts a comment only after whitespace, so a plain a#b.db survives."""
    home = tmp_path / "hermes"
    home.mkdir()
    db = make_db(home / "a#b.db", rows=3)
    (home / "config.yaml").write_text(
        f"memory:\n  mnemosyne:\n    db_path: {db}   # trailing comment\n", encoding="utf-8"
    )
    result = run("report", "--hermes-home", str(home))
    assert result.returncode == 0, result.stderr
    assert str(db) in result.stdout
    assert "memories=3" in result.stdout
    assert not (home / "a").exists(), "a '#': path must not retarget the connection"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
def test_unreadable_config_fails_loudly(tmp_path):
    """An unreadable config is a failure, not a silent fall back to another DB."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    home = tmp_path / "hermes"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text(
        f"memory:\n  mnemosyne:\n    db_path: {tmp_path / 'elsewhere.db'}\n", encoding="utf-8"
    )
    config.chmod(0o000)
    try:
        result = run("verify", "--hermes-home", str(home))
    finally:
        config.chmod(0o600)
    assert result.returncode == 2
    assert "cannot read" in result.stderr
    assert "config.yaml" in result.stderr


def test_unterminated_quoted_db_path_fails_loudly(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        'memory:\n  mnemosyne:\n    db_path: "' + str(home / "half.db") + "\n",
        encoding="utf-8",
    )
    result = run("report", "--hermes-home", str(home))
    assert result.returncode == 2
    assert "unterminated quoted db_path" in result.stderr


# --------------------------------------------------------------------------- #
# Health telemetry: read-only, count-only, optional per store
# --------------------------------------------------------------------------- #
def make_engine_db(
    path: pathlib.Path,
    *,
    working: int = 200,
    embedded: int = 150,
    consolidated: int = 10,
    vec_episodes: bool = False,
) -> pathlib.Path:
    """A store shaped like mnemosyne-memory 3.15.1's BEAM schema, without the engine."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE working_memory (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT, timestamp TEXT,
            session_id TEXT DEFAULT 'default', importance REAL DEFAULT 0.5,
            metadata_json TEXT, veracity TEXT DEFAULT 'unknown',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, consolidated_at TEXT
        );
        CREATE TABLE episodic_memory (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL, source TEXT, timestamp TEXT,
            session_id TEXT DEFAULT 'default', importance REAL DEFAULT 0.5,
            metadata_json TEXT, summary_of TEXT DEFAULT '',
            veracity TEXT DEFAULT 'unknown',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tier INTEGER DEFAULT 1, degraded_at TEXT
        );
        CREATE TABLE memory_embeddings (
            memory_id TEXT PRIMARY KEY, embedding_json TEXT NOT NULL, model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO working_memory (id, content, consolidated_at) VALUES (?, ?, ?)",
        [
            (
                f"wm-{index}",
                f"note {index}",
                "2026-01-01T00:00:00Z" if index < consolidated else None,
            )
            for index in range(working)
        ],
    )
    con.executemany(
        "INSERT INTO memory_embeddings (memory_id, embedding_json) VALUES (?, '[0.0]')",
        [(f"wm-{index}",) for index in range(embedded)],
    )
    con.executemany(
        "INSERT INTO episodic_memory (id, content, source, metadata_json) VALUES (?, ?, ?, ?)",
        [
            ("em-1", "summary one", "sleep_consolidation", '{"verified": false}'),
            ("em-2", "summary two", "sleep_consolidation", '{"verified": false}'),
            ("em-3", "summary three", "sleep_consolidation", '{"verified": true}'),
            ("em-4", "summary four", "sleep_consolidation", "not json at all"),
            ("em-5", "a user note", "user_note", '{"verified": false}'),
        ],
    )
    con.commit()
    if vec_episodes:
        # A real vec0 table is unreadable without the extension loaded; the
        # telemetry probe keys on the name in sqlite_master (virtual tables are
        # type='table' there), so a plain table stands in for it here.
        con.execute("CREATE TABLE vec_episodes (rowid INTEGER PRIMARY KEY, embedding BLOB)")
        con.commit()
    con.close()
    return path


def test_report_surfaces_telemetry_in_one_line(tmp_path):
    db = make_engine_db(tmp_path / "mnemosyne.db")
    result = run("report", "--db-path", str(db))
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    assert "working-embeddings=150/200(75%)" in result.stdout
    assert "unconsolidated=190" in result.stdout
    assert "unverified-sleep=2" in result.stdout, "malformed metadata must not crash the scan"
    assert "episodic-embeddings=0/5(0%)" in result.stdout, "counts are truthful, not filtered"


def test_report_omits_telemetry_it_cannot_answer(tmp_path):
    """A lite-only/minimal store has none of the engine tables: skip, never crash."""
    db = make_db(tmp_path / "mnemosyne.db", rows=4)
    result = run("report", "--db-path", str(db))
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    assert "embeddings=" not in result.stdout
    assert "unconsolidated=" not in result.stdout
    assert "unverified-sleep=" not in result.stdout


def test_verify_is_quiet_on_healthy_telemetry(tmp_path):
    db = make_engine_db(tmp_path / "mnemosyne.db", embedded=200, consolidated=200)
    result = run("verify", "--db-path", str(db))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_verify_warns_on_partial_embedding_coverage(tmp_path):
    db = make_engine_db(tmp_path / "mnemosyne.db", embedded=150)
    result = run("verify", "--db-path", str(db))
    assert result.returncode == 0, "a coverage warning is delivered output, not a failure"
    assert "working_memory embeddings cover 150/200 rows (75%)" in result.stdout


def test_verify_stays_quiet_when_no_vectors_exist_at_all(tmp_path):
    """Keyless installs legitimately have no embeddings; that is not drift."""
    db = make_engine_db(tmp_path / "mnemosyne.db", embedded=0)
    result = run("verify", "--db-path", str(db))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_episodic_coverage_is_omitted_when_sqlite_vec_owns_the_vectors(tmp_path):
    """With vec_episodes present the engine skips memory_embeddings for episodic rows.

    Counting the fallback table there reads a healthy store as 0/N, or as partial
    coverage when a few stale rows remain — so the metric must be omitted and no
    verify warning may fire.
    """
    db = make_engine_db(tmp_path / "mnemosyne.db", embedded=200, vec_episodes=True)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO memory_embeddings (memory_id, embedding_json) VALUES (?, '[0.0]')",
        [("em-1",), ("em-2",)],
    )
    con.commit()
    con.close()

    report = run("report", "--db-path", str(db))
    assert report.returncode == 0, report.stderr
    assert "working-embeddings=200/200(100%)" in report.stdout
    assert "episodic-embeddings" not in report.stdout, "unreadable vec0 rows are not a count"

    verify = run("verify", "--db-path", str(db))
    assert verify.returncode == 0, verify.stderr
    assert verify.stdout.strip() == "", "no false episodic coverage warning"
