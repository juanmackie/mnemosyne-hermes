"""Contract checks for the retrieval fixtures and the replay harness.

Offline and keyless: nothing here imports the engine, downloads a model or
touches a database. The paraphrase split is checked as data — schema, ground
truth and lexical distance from the target document.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL = ROOT / "bench" / "retrieval"
TOKEN_RE = re.compile(r"[a-z0-9]+")

# The engine's own gate: `_is_meaningful_recall_token` in `core/beam.py` accepts
# tokens of three or more characters that are neither digits nor in
# `_FACT_MATCH_STOPWORDS`. A query with no such match scores 0.0 lexical
# relevance and is dropped before the dense score is consulted.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
        "again",
        "into",
        "not",
        "please",
        "somewhere",
        "supposed",
        "them",
        "then",
        "they",
        "whatever",
        "ask",
        "asking",
        "query",
        "question",
        "questions",
        "search",
        "retrieve",
        "recall",
    ]
)


def meaningful_tokens(text: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(text.lower()) if _is_meaningful(token)}


def _is_meaningful(token: str) -> bool:
    return len(token) >= 3 and token not in STOPWORDS and not token.isdigit()


def load_rows(name: str) -> list[dict]:
    text = (RETRIEVAL / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_run_eval():
    """Load the harness by path: it is a bench script, not an installed module."""
    path = RETRIEVAL / "run_eval.py"
    spec = importlib.util.spec_from_file_location("retrieval_run_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def target_content(row: dict, corpus: list[dict]) -> str | None:
    """The single corpus row a query's `relevant` substrings point at."""
    matches = [
        candidate["content"]
        for candidate in corpus
        if any(needle.lower() in candidate["content"].lower() for needle in row["relevant"])
    ]
    return matches[0] if len(matches) == 1 else None


def coverage(query: str, content: str) -> float:
    """Share of the query's meaningful tokens that occur in the target row."""
    tokens = meaningful_tokens(query)
    return len(tokens & meaningful_tokens(content)) / len(tokens)


def test_paraphrase_split_uses_the_dev_schema():
    dev = load_rows("eval_dev.jsonl")
    paraphrase = load_rows("eval_paraphrase.jsonl")
    assert paraphrase
    assert {tuple(sorted(row)) for row in paraphrase} == {tuple(sorted(row)) for row in dev}
    assert {row["category"] for row in dev} != {row["category"] for row in paraphrase}
    for row in paraphrase:
        assert isinstance(row["query"], str) and row["query"].strip()
        assert isinstance(row["category"], str) and row["category"].strip()
        assert isinstance(row["relevant"], list)
        assert all(isinstance(needle, str) and needle.strip() for needle in row["relevant"])


def test_paraphrase_split_is_large_enough_and_uniquely_identified():
    paraphrase = load_rows("eval_paraphrase.jsonl")
    # The query text is the identifier: `run_eval.py` keys nothing else.
    queries = [row["query"] for row in paraphrase]
    assert len(queries) == len(set(queries))
    assert 30 <= len(queries) <= 40
    answerable = [row for row in paraphrase if row["relevant"]]
    assert len(answerable) >= 30


def test_every_relevant_substring_names_exactly_one_corpus_row():
    corpus = load_rows("corpus.jsonl")
    for name in ("eval_paraphrase.jsonl",):
        for row in load_rows(name):
            for needle in row["relevant"]:
                hits = [c["content"] for c in corpus if needle.lower() in c["content"].lower()]
                assert len(hits) == 1, f"{name}: {needle!r} matches {len(hits)} corpus rows"


def test_unanswerable_queries_are_labelled_and_carry_no_targets():
    paraphrase = load_rows("eval_paraphrase.jsonl")
    unanswerable = [row for row in paraphrase if not row["relevant"]]
    assert 0 < len(unanswerable) <= 5
    assert {row["category"] for row in unanswerable} == {"unanswerable"}
    assert all(row["relevant"] for row in paraphrase if row["category"] != "unanswerable")


def test_paraphrase_queries_are_not_lexical_copies():
    corpus = load_rows("corpus.jsonl")
    paraphrase = [row for row in load_rows("eval_paraphrase.jsonl") if row["relevant"]]
    dev = load_rows("eval_dev.jsonl")
    for row in paraphrase:
        content = target_content(row, corpus)
        assert content is not None, f"no single target row for {row['query']!r}"
        shared = meaningful_tokens(row["query"]) & meaningful_tokens(content)
        # One generic anchor token keeps the row admissible (the lexical gate
        # needs a match) without letting the keyword index identify it: the
        # anchor occurs in other rows too, so the dense score must separate.
        assert len(shared) == 1, f"{row['query']!r} shares {sorted(shared)}"
        document_frequency = sum(
            1 for candidate in corpus if shared <= meaningful_tokens(candidate["content"])
        )
        assert document_frequency >= 2, f"{row['query']!r} anchor is distinctive"

    paraphrase_mean = sum(
        coverage(row["query"], target_content(row, corpus) or "") for row in paraphrase
    ) / len(paraphrase)
    dev_mean = sum(coverage(row["query"], target_content(row, corpus) or "") for row in dev) / len(
        dev
    )
    assert paraphrase_mean < 0.25, paraphrase_mean
    assert paraphrase_mean < 0.6 * dev_mean, (paraphrase_mean, dev_mean)


def test_run_eval_discovers_the_paraphrase_split():
    run_eval = load_run_eval()
    assert run_eval.SPLITS["paraphrase"] == "eval_paraphrase.jsonl"
    for filename in run_eval.SPLITS.values():
        assert (RETRIEVAL / filename).is_file(), filename

    selected = run_eval.select_datasets(False, ["paraphrase"], None)
    assert selected == [RETRIEVAL / "eval_paraphrase.jsonl"]
    assert run_eval.select_datasets(False, None, None) == [RETRIEVAL / "eval_dev.jsonl"]
    custom = RETRIEVAL / "eval_heldout_b.jsonl"
    assert run_eval.select_datasets(False, None, custom) == [custom]
    assert run_eval.select_datasets(True, None, None) == [
        RETRIEVAL / filename for filename in run_eval.SPLITS.values()
    ]

    parser = run_eval.build_parser()
    assert parser.parse_args(["--split", "paraphrase"]).split == ["paraphrase"]
    assert parser.parse_args([]).split is None
    assert parser.parse_args(["--all"]).all is True


def test_summary_carries_latency_percentiles_and_peak_rss():
    run_eval = load_run_eval()
    report = run_eval.summarize(
        [
            {"rank": 1, "latency_ms": 10.0, "count": 3, "category": "paraphrase_entity"},
            {"rank": None, "latency_ms": 30.0, "count": 0, "category": "unanswerable"},
        ]
    )
    assert report["latency_p50_ms"] == 20.0
    assert report["latency_p95_ms"] == 30.0
    assert run_eval.percentile([], 0.95) == 0.0
    assert run_eval.percentile([5.0], 0.95) == 5.0
    rss = run_eval.peak_rss_mb()
    assert rss is None or rss > 0.0
