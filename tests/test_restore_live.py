"""Restoring a store must coordinate with existing SQLite connections."""

import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mnemosyne_lite.cli import cmd_restore  # noqa: E402
from mnemosyne_lite.storage import PythonMemoryStorage  # noqa: E402


class LiveRestoreTests(unittest.TestCase):
    def test_restore_with_open_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            live = str(Path(directory) / "live.db")
            backup = str(Path(directory) / "backup.db")
            # Exercise a legacy store with a different SQLite page size.
            # A `with sqlite3.connect(...)` block commits but does not close, so
            # the handle must be released explicitly or Windows keeps the file
            # locked and the temp-directory cleanup fails.
            conn = sqlite3.connect(live)
            try:
                conn.execute("PRAGMA page_size=4096")
                conn.execute("VACUUM")
                conn.commit()
            finally:
                conn.close()
            old = PythonMemoryStorage(live)
            old.remember("old content", "ns", 5)
            source = PythonMemoryStorage(backup)
            source.remember("restored content", "ns", 5)
            source.close()
            self.assertEqual(
                cmd_restore(Namespace(backup=backup, db_path=live, yes=True, format="json")), 0
            )
            old.remember("after restore", "ns", 5)
            fresh = PythonMemoryStorage(live)
            try:
                contents = {row["content"] for row in fresh.list_memories()}
                self.assertEqual(len(fresh.recall("restored")), 1)
                self.assertEqual(contents, {"restored content", "after restore"})
            finally:
                fresh.close()
            # Windows refuses to remove the temp directory while a handle is open.
            old.close()


if __name__ == "__main__":
    unittest.main()
