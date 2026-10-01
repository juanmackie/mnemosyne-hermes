"""What recall() matches, and what it deliberately does not.

Round 1 of the recall-quality hillclimb changed the FTS5 query from
space-joined terms (an implicit AND) to OR-ed terms with stopwords dropped. The
old rule meant a question only matched if the corpus happened to contain every
one of its words, so every natural-language query returned nothing. These tests
pin the new contract, because the old test suite asserted the AND behaviour
incidentally rather than on purpose: an assertion like `recall("dark mode") == []`
passed for the wrong reason - `mode` was still in the row, and only the AND kept
it out of the result.

A later round may tighten matching (a minimum-match floor, stemming). If one of
these tests has to change, that is the signal: the contract moved, and whoever
moves it says so in the round's change.md.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mnemosyne_lite.storage import PythonMemoryStorage  # noqa: E402


class RecallMatchingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PythonMemoryStorage(str(Path(self._tmp.name) / "m.db"))
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def ids(self, *args, **kwargs):
        return [r["id"] for r in self.store.recall(*args, **kwargs)]

    def test_a_multi_word_question_matches_on_one_of_its_words(self):
        """The regression this suite exists for."""
        mid = self.store.remember("My email is alex@example.dev", "ns", 5)["id"]
        # None of these words are in the memory except "email": under the old
        # AND rule this returned nothing at all.
        self.assertEqual([mid], self.ids("what's my email address again?"))

    def test_a_single_token_still_matches_exactly(self):
        mid = self.store.remember("note about widgets number 3", "ns", 5)["id"]
        self.assertEqual([mid], self.ids("3"))
        self.store.remember("another widgets note", "ns", 5)
        self.assertEqual([mid], self.ids("3", max_results=10))

    def test_stopwords_do_not_widen_the_match(self):
        """An OR over "my"/"i"/"what" would match most of the corpus."""
        self.store.remember("the cat sat on the mat", "ns", 5)
        other = self.store.remember("unrelated note about kayaking", "ns", 5)["id"]
        self.assertNotIn(other, self.ids("what is the cat doing now?"))

    def test_hyphenated_and_dotted_terms_split_like_the_tokenizer(self):
        mid = self.store.remember("hermes-dashboard deploys to fly.io", "ns", 5)["id"]
        self.assertEqual([mid], self.ids("hermes-dashboard"))
        self.assertEqual([mid], self.ids("fly.io"))

    def test_fts_operators_in_user_text_are_queries_not_syntax(self):
        mid = self.store.remember("release notes for version 2", "ns", 5)["id"]
        # Unquoted, a bare OR/NEAR/* would be parsed as FTS5 syntax and could
        # raise or silently match the whole index.
        for query in ("release OR notes", 'release "quoted"', "notes AND version", "notes*"):
            with self.subTest(query=query):
                self.assertIn(mid, self.ids(query))

    def test_a_query_with_no_letters_falls_back_to_a_literal_scan(self):
        mid = self.store.remember("ticket %s is urgent", "ns", 5)["id"]
        self.assertEqual([mid], self.ids("%s"))

    def test_filters_still_apply(self):
        self.store.remember("shared word in ns one", "one", 5)
        two = self.store.remember("shared word in ns two", "two", 5)["id"]
        self.assertEqual([two], self.ids("shared word", namespace="two"))
        self.assertEqual([], self.ids("shared word", namespace="three"))

    def test_an_empty_query_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.store.recall("   ")


if __name__ == "__main__":
    unittest.main()