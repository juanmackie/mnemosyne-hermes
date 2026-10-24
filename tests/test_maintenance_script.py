"""Maintenance-suite regressions: quiet success, loud failure, safe snapshots.

Every test runs the real script in a subprocess against a throwaway database, so
what is asserted is the exit code and the filesystem effect a cron job sees —
not an in-process call that could pass while the CLI is broken.
"""

from __future__ import annotations

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
