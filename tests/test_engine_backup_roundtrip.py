"""vec0-safe engine backup/restore: the audited dr/recovery.py patch, behaviourally.

The pre-patch engine serialized backups with iterdump() (SQL text). sqlite-vec
vec0 virtual tables store vectors in opaque shadow blobs that iterdump cannot
represent, so every restore silently lost the vector index while reporting
success. The patch gzips the raw page image from sqlite3.backup() instead;
restore sniffs the payload and still accepts legacy SQL-text dumps.

Engine-backed lane: skips without the engine, fails under
MNEMOSYNE_REQUIRE_ENGINE=1 like the other engine contract gates.
"""

import gzip
import os
import sqlite3
import tempfile
from pathlib import Path


def _require_engine():
    try:
        from mnemosyne.dr import recovery
        return recovery
    except ImportError:
        if os.environ.get("MNEMOSYNE_REQUIRE_ENGINE") == "1":
            raise
        return None


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _build_store(path: Path):
    """A WAL-mode store with rows and (when possible) a vec0 table.

    The connection stays open with uncheckpointed WAL frames so the backup
    must fold them, exactly like a live store under write load.
    """
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, content TEXT)")
    con.executemany(
        "INSERT INTO items (content) VALUES (?)",
        [("alpha",), ("beta",), ("gamma",)],
    )
    has_vec = False
    if _sqlite_vec_available():
        import sqlite_vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.execute("CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[4])")
        for i in range(3):
            con.execute(
                "INSERT INTO vec_items (rowid, embedding) VALUES (?, ?)",
                (i + 1, sqlite_vec.serialize_float32([float(i)] * 4)),
            )
        has_vec = True
    con.commit()
    return con, has_vec


def test_backup_payload_is_a_raw_sqlite_image():
    recovery = _require_engine()
    if recovery is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mnemosyne.db"
        con, _ = _build_store(db)
        try:
            result = recovery.create_backup(db, Path(tmp) / "backups")
            payload = gzip.decompress(Path(result["backup_path"]).read_bytes())
            assert payload[:16] == b"SQLite format 3\x00", (
                "backup must be a raw page image; SQL-text dumps lose vec0 "
                f"shadow tables (payload starts {payload[:24]!r})"
            )
            assert not payload.startswith(b"BEGIN TRANSACTION")
        finally:
            con.close()


def test_roundtrip_preserves_rows_vectors_and_wal_frames():
    recovery = _require_engine()
    if recovery is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mnemosyne.db"
        con, has_vec = _build_store(db)
        try:
            # One more write left in the WAL, uncheckpointed.
            con.execute("INSERT INTO items (content) VALUES ('delta')")
            con.commit()
            result = recovery.create_backup(db, Path(tmp) / "backups")
        finally:
            con.close()
        target = Path(tmp) / "restored.db"
        report = recovery.restore_backup(Path(result["backup_path"]), target)
        assert report["restored"] is True
        assert report["integrity_check"] is True
        check = sqlite3.connect(str(target))
        try:
            rows = check.execute(
                "SELECT content FROM items ORDER BY id"
            ).fetchall()
            assert [r[0] for r in rows] == ["alpha", "beta", "gamma", "delta"]
            if has_vec:
                # Querying a vec0 table needs the module on this connection;
                # the restored shadow data itself is extension-independent.
                import sqlite_vec
                check.enable_load_extension(True)
                sqlite_vec.load(check)
                vec_count = check.execute(
                    "SELECT count(*) FROM vec_items"
                ).fetchone()[0]
                assert vec_count == 3, (
                    f"vec0 rows must survive the round trip, got {vec_count}"
                )
        finally:
            check.close()


def test_legacy_sql_dump_backups_still_restore():
    recovery = _require_engine()
    if recovery is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mnemosyne.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, content TEXT)")
        con.executemany(
            "INSERT INTO items (content) VALUES (?)", [("one",), ("two",)]
        )
        con.commit()
        legacy = Path(tmp) / "legacy.db.gz"
        with gzip.open(legacy, "wb") as f:
            for line in con.iterdump():
                f.write((line + "\n").encode("utf-8"))
        con.close()
        target = Path(tmp) / "restored.db"
        report = recovery.restore_backup(legacy, target)
        assert report["restored"] is True
        assert report["integrity_check"] is True
        check = sqlite3.connect(str(target))
        try:
            rows = check.execute(
                "SELECT content FROM items ORDER BY id"
            ).fetchall()
            assert [r[0] for r in rows] == ["one", "two"]
        finally:
            check.close()


def test_restore_missing_backup_raises():
    recovery = _require_engine()
    if recovery is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        try:
            recovery.restore_backup(
                Path(tmp) / "nope.db.gz", Path(tmp) / "out.db"
            )
        except FileNotFoundError:
            return
        raise AssertionError("missing backup must raise FileNotFoundError")


if __name__ == "__main__":
    tests = [
        test_backup_payload_is_a_raw_sqlite_image,
        test_roundtrip_preserves_rows_vectors_and_wal_frames,
        test_legacy_sql_dump_backups_still_restore,
        test_restore_missing_backup_raises,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
