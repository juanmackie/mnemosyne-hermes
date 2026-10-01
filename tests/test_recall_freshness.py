"""Freshness/semantics regression tests; runnable with stdlib unittest.

Two things are pinned here:

* **Freshness** — recall() must reflect the current database state, including
  writes made by another connection, another thread, or the same connection
  before a commit.
* **Matching** — recall() is full-text search (FTS5, BM25-ranked). It matches
  *tokens*, not substrings, and a query with no letters or digits falls back to
  a literal substring scan because FTS5 would have no token to match.
"""

import random
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mnemosyne_lite.storage import PythonMemoryStorage


class RecallFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "memory.db")
        self.reader = PythonMemoryStorage(self.path)
        self.reader.remember("existing amber marker", "ns", 5)
        self.reader.recall("freshness marker")
        self.writer = PythonMemoryStorage(self.path)

    def tearDown(self):
        self.writer.close()
        self.reader.close()
        self.tmp.cleanup()

    def assert_sql_equivalent(self, query, namespace=None, limit=10, importance=None):
        """recall() must agree with a direct SQL read of the same predicate.

        Compared as an id -> row mapping, not a list: what this asserts is
        *freshness* (the result reflects the current database state), not the
        ranking order, which `test_recall_ranks_best_match_first` pins.

        The predicate mirrors recall()'s: a row matches when it contains ANY of
        the query's tokens (see `_fts_match_expression` - space-joined terms were
        an implicit AND, which made every natural-language query match nothing).
        Matching is whole-token on both sides, as FTS5's tokenizer does it: a
        substring test would call `100` a match inside `distinct-0100`, which is
        not the query anyone wrote. The reference is computed in Python from a
        direct row read, so it stays an independent check of the predicate rather
        than a second call into the FTS index recall already uses. Case folding
        is Python's Unicode-aware lower(); FTS5 additionally folds diacritics, so
        a query on an accented word would need the reference spelled that way -
        none in this file is. When `limit` is below the match count the two sides
        can pick different rows, because this helper does not model ranking; see
        the branch below.
        """
        conn = self.reader._conn()
        want = {t.lower() for t in re.findall(r"[^\W_]+", query)} or {query.lower()}

        def content_tokens(text):
            return {w.lower() for w in re.findall(r"[^\W_]+", text)}

        sql = "SELECT id, content FROM memories WHERE 1=1"
        params = []
        if namespace:
            sql += " AND namespace = ?"
            params.append(namespace)
        if importance is not None:
            sql += " AND importance >= ?"
            params.append(importance)
        sql += " ORDER BY importance DESC, created_at DESC, id"
        matched = {r["id"]: r["content"] for r in conn.execute(sql, params)
                   if content_tokens(r["content"]) & want}
        # Compared on id and content, not on the whole row dict: recall()
        # records the access (access_count / last_accessed change as a side
        # effect of searching), so those fields are a write-back rather than the
        # searchable state this helper checks. Freshness is "the right rows,
        # with the right text, right now".
        actual = {r["id"]: r["content"]
                  for r in self.reader.recall(query, namespace, limit, importance)}
        if len(matched) <= limit:
            # Both sides return every match, so identity is the right assertion.
            self.assertEqual(dict(list(matched.items())[:limit]), actual)
        else:
            # The cut is decided by ranking, and this helper deliberately does
            # not model ranking - it orders by importance/created_at/id while
            # recall orders by BM25 first. Comparing the two top-N sets would be
            # asserting a ranking claim this test exists to avoid. What still
            # holds, and what this asserts, is the guarantee that matters here:
            # recall returns only rows a direct read of the same predicate would
            # return - so no stale row, and no row the namespace or importance
            # filter excluded.
            self.assertLessEqual(
                set(actual), set(matched),
                f"recall returned {len(set(actual) - set(matched))} row(s) a direct "
                "read of the same predicate would not: stale, or filter-violating",
            )

    def test_second_instance_insert_update_delete(self):
        mid = self.writer.remember("freshness marker", "ns", 8)["id"]
        # setUp seeded "existing amber marker", which contains "marker" - so
        # recall("freshness marker") is a union of both rows under the OR rule.
        # The intent is that the new write is visible through the second
        # instance, and that it outranks a row matching only one of the query's
        # terms; that is what is asserted here.
        found = [r["id"] for r in self.reader.recall("freshness marker")]
        self.assertIn(mid, found)
        self.assertEqual(mid, found[0], "the row matching both terms should rank first")
        self.assert_sql_equivalent("freshness marker")
        conn = self.writer._conn()
        conn.execute(
            "UPDATE memories SET content='changed zebra marker', importance=9 WHERE id=?",
            (mid,),
        )
        conn.commit()
        self.assert_sql_equivalent("freshness marker")
        self.assert_sql_equivalent("zebra marker")
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
        conn.commit()
        self.assert_sql_equivalent("zebra marker")

    def test_preference_content_is_fresh(self):
        mid = self.writer.remember("I prefer dark mode", "ns", 8)["id"]
        self.assertEqual("I prefer dark mode", self.reader.recall("I prefer")[0]["content"])
        conn = self.writer._conn()
        conn.execute(
            "UPDATE memories SET content=? WHERE id=?",
            ("I prefer light mode", mid),
        )
        conn.commit()
        self.assertEqual("I prefer light mode", self.reader.recall("I prefer")[0]["content"])
        # The updated token must be gone from the index. Asserted on "dark"
        # alone rather than on "dark mode": recall() ORs the query's tokens, and
        # "mode" is still in the row, so the pair legitimately matches now.
        # Under the old AND-every-token rule this assertion passed for the wrong
        # reason - `mode` alone would have kept the row visible too.
        self.assertEqual([], self.reader.recall("dark"))
        self.assertEqual(mid, self.reader.recall("light mode")[0]["id"])

    def test_same_connection_changes_and_reopen(self):
        self.reader.remember("local freshness marker", "ns", 8)
        self.assert_sql_equivalent("freshness marker")
        conn = self.reader._conn()
        conn.execute("UPDATE memories SET content='updated freshness marker'")
        self.assert_sql_equivalent("updated freshness")  # uncommitted writes
        conn.commit()
        self.assert_sql_equivalent("updated freshness")
        self.reader.close()
        self.writer.remember("reopened freshness marker", "ns", 9)
        self.assert_sql_equivalent("reopened freshness")

    def test_thread_writer_and_access_metadata(self):
        errors = []

        def write():
            try:
                self.reader.remember("thread freshness marker", "ns", 8)
                self.reader.close()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=write)
        thread.start()
        thread.join()
        self.assertEqual([], errors)
        self.assert_sql_equivalent("thread freshness")
        self.reader.list_memories(sort_by="access")
        self.assert_sql_equivalent("thread freshness")
        row = self.reader.recall("thread freshness")[0]
        row["content"] = "caller mutation"
        self.assert_sql_equivalent("thread freshness")

    def test_recall_memo_is_per_thread(self):
        """A second thread's write must not be answered from the first's memo.

        An entry is validated against a version built from the calling thread's
        own connection (`PRAGMA data_version` plus that connection's
        `total_changes`), so it is only meaningful beside the connection that
        wrote it. With one dict shared by the instance, a second connection
        whose two numbers happened to agree read the first thread's rows and
        recall missed a row that was already committed.
        """
        self.reader.remember("perthread alpha one", "ns", 5)
        # Warm this thread's memo against a one-row state.
        self.assertEqual(1, len(self.reader.recall("perthread alpha")))
        seen, errors = [], []

        def other_thread():
            try:
                self.reader.remember("perthread alpha two", "ns", 5)
                seen.extend(r["content"] for r in self.reader.recall("perthread alpha"))
            except Exception as exc:  # surfaced by the assert below
                errors.append(exc)
            finally:
                self.reader.close()  # this thread's own connection (Windows holds the file)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        self.assertEqual([], errors)
        self.assertEqual(2, len(seen), seen)
        self.assert_sql_equivalent("perthread alpha")

    def test_recall_ranks_best_match_first(self):
        """Full-text ranking: the better match comes first, not the importanter one.

        The old substring scan ordered by importance alone, so an unrelated but
        important row could sit above the row actually asked for.
        """
        long_id = self.writer.remember("alpha beta gamma delta epsilon zeta", "ns", 10)["id"]
        short_id = self.writer.remember("alpha", "ns", 1)["id"]
        ranked = [r["id"] for r in self.reader.recall("alpha", max_results=10)]
        self.assertEqual(2, len(ranked), ranked)
        # BM25 normalizes by document length, so the denser document wins even
        # though its importance is lower.
        self.assertEqual(short_id, ranked[0])
        self.assertEqual(long_id, ranked[1])

    def test_recall_matches_tokens_not_substrings(self):
        """Matching is per token; only a query with no letters/digits is literal.

        `al` used to find `alpha` because recall was a substring scan. It does
        not now, which is the point of using full-text search. `%` and `_` have
        no token at all, so they keep the old literal behaviour.
        """
        self.writer.remember("alpha beta", "ns", 5)
        self.writer.remember("100% cotton", "ns", 5)
        self.writer.remember("under_score", "ns", 5)
        self.assertEqual([], self.reader.recall("al"))
        self.assertEqual(1, len(self.reader.recall("alpha")))
        self.assertEqual(1, len(self.reader.recall("100%")))
        self.assertEqual(1, len(self.reader.recall("%")))
        self.assertEqual(1, len(self.reader.recall("_")))

    def test_equal_rank_order_is_deterministic(self):
        """Equally-ranked rows must come back in the same order every time.

        BM25 ties are the normal case for identical text, so without a unique
        final key in the ORDER BY a `LIMIT` could drop a different row between
        two identical queries.
        """
        for i in range(20):
            self.writer.remember(f"tied rank marker {i}", "ns", 5)
        conn = self.writer._conn()
        conn.execute("UPDATE memories SET created_at=1, importance=5")
        conn.commit()
        first = [r["id"] for r in self.reader.recall("tied rank marker", max_results=7)]
        self.assertEqual(7, len(first), first)
        self.assertEqual(
            first, [r["id"] for r in self.reader.recall("tied rank marker", max_results=7)]
        )
        self.assert_sql_equivalent("tied rank marker", limit=7)

    def test_namespace_filter_is_applied(self):
        self.writer.remember("namespace marker", "ns", 8)
        self.assert_sql_equivalent("namespace marker", namespace="ns")
        self.assert_sql_equivalent("namespace marker", namespace="other")
        self.assertEqual([], self.reader.recall("namespace marker", namespace="other"))

    def test_distinct_queries_filters_and_token_matching(self):
        rng = random.Random(42)
        terms = ["alpha", "beta", "gamma", "Café", "MÜNCHEN", "100%", "under_score", "back\\slash"]
        for i in range(120):
            text = " ".join(rng.sample(terms, 3)) + f" distinct-{i:04d}"
            self.writer.remember(text, "ns" if i % 2 else "other", i % 10 + 1)
        # One row per distinct-NNNN token, with the namespace and importance
        # filters applied. limit is high so the two orders cannot diverge.
        for i in range(120):
            self.assert_sql_equivalent(f"distinct-{i:04d}", "ns" if i % 3 else "", 100, i % 8)
        for query in ["alpha", "ALPHA", "gamma", "100%"]:
            self.assert_sql_equivalent(query, limit=100)
        # FTS5's unicode61 tokenizer folds case and diacritics, so these match.
        # That is a deliberate change from the old ASCII-only substring scan.
        self.assertTrue(self.reader.recall("cafe"))
        self.assertTrue(self.reader.recall("munchen"))
        # A query with no letter or digit has no token, so it stays literal.
        self.assertTrue(self.reader.recall("%"))
        self.assertTrue(self.reader.recall("_"))
        # "quokka" is absent from every row in this class, so the query is
        # genuinely absent: recall() ORs a query's tokens, so a name like
        # "absent-marker" would match the seeded "existing amber marker" row on
        # the word "marker" and prove nothing about absence.
        self.assertEqual([], self.reader.recall("quokka-token"))
        with self.assertRaises(ValueError):
            self.reader.recall(" ")


if __name__ == "__main__":
    unittest.main()
