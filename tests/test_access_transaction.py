"""A failed best-effort access batch must not leak into a later write."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mnemosyne_lite.storage import PythonMemoryStorage


class AccessTransactionTests(unittest.TestCase):
    def test_failed_batch_does_not_commit_partial_updates_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PythonMemoryStorage(str(Path(directory) / "store.db"))
            first = store.remember("first", "ns", 5)["id"]
            second = store.remember("second", "ns", 5)["id"]
            conn = store._conn()
            conn.execute("CREATE TEMP TRIGGER reject_access BEFORE UPDATE OF access_count ON memories "
                         "WHEN OLD.id = '%s' BEGIN SELECT RAISE(ABORT, 'injected'); END" % second)
            store._pending().append((first, second))
            store._flush_accesses()
            store.remember("later write", "ns", 5)
            count = conn.execute("SELECT access_count FROM memories WHERE id = ?", (first,)).fetchone()[0]
            self.assertEqual(count, 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
