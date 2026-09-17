"""Freshness/semantics regression tests; runnable with stdlib unittest."""
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lib.storage import PythonMemoryStorage


class RecallFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'memory.db')
        self.reader = PythonMemoryStorage(self.path)
        self.reader.remember('existing amber marker', 'ns', 5)
        self.reader.recall('freshness marker')
        self.writer = PythonMemoryStorage(self.path)

    def tearDown(self):
        self.writer.close()
        self.reader.close()
        self.tmp.cleanup()

    def assert_sql_equivalent(self, query, namespace=None, limit=10, importance=None):
        conn = self.reader._conn()
        sql = 'SELECT * FROM memories WHERE instr(content_lower, ?) > 0'
        params = [query.translate(PythonMemoryStorage.ASCII_LOWER)]
        if namespace:
            sql += ' AND namespace = ?'
            params.append(namespace)
        if importance is not None:
            sql += ' AND importance >= ?'
            params.append(importance)
        sql += ' ORDER BY importance DESC, created_at DESC LIMIT ?'
        params.append(max(1, min(100, limit)))
        expected = [self.reader._row_to_dict(row) for row in conn.execute(sql, params)]
        actual = self.reader.recall(query, namespace, limit, importance)
        self.assertEqual(expected, actual)

    def test_second_instance_insert_update_delete(self):
        mid = self.writer.remember('freshness marker', 'ns', 8)['id']
        self.assertEqual([mid], [r['id'] for r in self.reader.recall('freshness marker')])
        self.assert_sql_equivalent('freshness marker')
        conn = self.writer._conn()
        conn.execute("UPDATE memories SET content='changed zebra marker', content_lower='changed zebra marker', importance=9 WHERE id=?", (mid,))
        conn.commit()
        self.assert_sql_equivalent('freshness marker')
        self.assert_sql_equivalent('zebra marker')
        conn.execute('DELETE FROM memories WHERE id=?', (mid,))
        conn.commit()
        self.assert_sql_equivalent('zebra marker')

    def test_preference_content_is_fresh(self):
        mid = self.writer.remember('I prefer dark mode', 'ns', 8)['id']
        self.assertEqual('I prefer dark mode', self.reader.recall('I prefer')[0]['content'])
        conn = self.writer._conn()
        conn.execute("UPDATE memories SET content=?, content_lower=? WHERE id=?",
                     ('I prefer light mode', 'i prefer light mode', mid))
        conn.commit()
        self.assertEqual('I prefer light mode', self.reader.recall('I prefer')[0]['content'])
        self.assertEqual([], self.reader.recall('dark mode'))
        self.assertEqual(mid, self.reader.recall('light mode')[0]['id'])

    def test_same_connection_changes_and_reopen(self):
        self.reader.remember('local freshness marker', 'ns', 8)
        self.assert_sql_equivalent('freshness marker')
        conn = self.reader._conn()
        conn.execute("UPDATE memories SET content_lower='updated freshness marker', content='updated freshness marker'")
        self.assert_sql_equivalent('updated freshness')  # uncommitted writes
        conn.commit()
        self.assert_sql_equivalent('updated freshness')
        self.reader.close()
        self.writer.remember('reopened freshness marker', 'ns', 9)
        self.assert_sql_equivalent('reopened freshness')

    def test_thread_writer_and_access_metadata(self):
        errors = []
        def write():
            try:
                self.reader.remember('thread freshness marker', 'ns', 8)
                self.reader.close()
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=write)
        thread.start()
        thread.join()
        self.assertEqual([], errors)
        self.assert_sql_equivalent('thread freshness')
        self.reader.list_memories(sort_by='access')
        self.assert_sql_equivalent('thread freshness')
        row = self.reader.recall('thread freshness')[0]
        row['content'] = 'caller mutation'
        self.assert_sql_equivalent('thread freshness')

    def test_write_between_candidate_selection_and_fetch(self):
        original = self.reader._search_candidates
        def candidates(conn, query):
            result = original(conn, query)
            self.writer.remember('racing freshness marker', 'ns', 9)
            return result
        self.reader._search_candidates = candidates
        self.assertEqual(1, len(self.reader.recall('racing freshness')))

    def test_equal_rank_order(self):
        for i in range(20):
            self.writer.remember(f'tied rank marker {i}', 'ns', 5)
        conn = self.writer._conn()
        conn.execute("UPDATE memories SET created_at=1, importance=5")
        conn.commit()
        self.assert_sql_equivalent('tied rank marker', limit=7)

    def test_namespace_uses_sql_without_snapshot(self):
        def unexpected_snapshot(*args):
            self.fail('namespace-filtered recall must not scan the global snapshot')
        self.reader._search_candidates = unexpected_snapshot
        self.writer.remember('namespace marker', 'ns', 8)
        self.assert_sql_equivalent('namespace marker', namespace='ns')
        self.assert_sql_equivalent('namespace marker', namespace='other')

    def test_distinct_queries_filters_and_literal_matching(self):
        rng = random.Random(42)
        terms = ['alpha', 'beta', 'gamma', 'Café', 'MÜNCHEN', '100%', 'under_score', 'back\\slash']
        for i in range(120):
            text = ' '.join(rng.sample(terms, 3)) + f' distinct-{i:04d}'
            self.writer.remember(text, 'ns' if i % 2 else 'other', i % 10 + 1)
        for i in range(120):
            self.assert_sql_equivalent(f'distinct-{i:04d}', 'ns' if i % 3 else '', 1 + i % 20, i % 8)
        for query in terms + ['ALPHA', 'münchen', '%', '_', 'al', 'absent-marker']:
            self.assert_sql_equivalent(query)
        with self.assertRaises(ValueError):
            self.reader.recall(' ')


if __name__ == '__main__':
    unittest.main()
