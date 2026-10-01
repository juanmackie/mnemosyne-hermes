#!/usr/bin/env python3
"""Pre-screen candidate MATCH strategies on the TRAIN split only.

Spending a hillclimb round to find out that a change is a no-op is wasteful, and
the train split is small (noise floor ~13.6 points at n=27, 2 reps). So: evaluate
candidate query-expression strategies directly in SQL, against the same corpus
and the same `grade()` the runner uses, and only on train. The held-out split is
not touched here — it stays held out.

This is a screen, not a variant. Nothing it prints is a result; it says which
change is worth spending a round on.

    python3 prescreen.py
"""

import json
import pathlib
import re
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runner import CASES, grade, read_jsonl, wilson  # noqa: E402

DB = HERE / "data" / "baseline" / "corpus.db"

# Words that carry no retrieval signal in a question. Deliberately short: a
# stopword list is a product decision, and a long one can delete real signal
# ("all", "no", "not", "only" are all content-bearing in memory).
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "am", "be", "been", "being",
    "do", "does", "did", "doing", "have", "has", "had", "i", "me", "my", "we",
    "our", "you", "your", "it", "its", "that", "this", "these", "those", "what",
    "which", "who", "whom", "whose", "when", "where", "why", "how", "again",
    "still", "now", "currently", "please", "and", "or", "for", "to", "of", "in",
    "on", "at", "by", "with", "about", "from", "s", "t",
}

TOKEN = re.compile(r"[^\W_]+")
RANK = "ORDER BY bm25(memories_fts), m.importance DESC, m.created_at DESC, m.id"


def quote(token):
    return '"' + token.replace('"', '""') + '"'


def expr_current(query):
    """What ships today: every token, space-joined => implicit AND."""
    return " ".join(quote(t) for t in query.split())


def expr_or(query):
    return " OR ".join(quote(t) for t in TOKEN.findall(query))


def expr_and_content(query):
    """AND over tokens that are not stopwords. Falls back to AND over all when
    the query is nothing but stopwords."""
    toks = [t for t in TOKEN.findall(query) if t.lower() not in STOPWORDS]
    if not toks:
        toks = TOKEN.findall(query)
    return " AND ".join(quote(t) for t in toks) if len(toks) > 1 else (quote(toks[0]) if toks else None)


def expr_relax(query):
    """Progressive relaxation: AND first, OR only if AND found nothing."""
    return expr_and_content(query), expr_or(query)


def matched_token_count(row_content, query):
    """How many distinct query tokens the row actually accounts for.

    Prefix-matched, so "deploy" counts a row saying "deploys" - the tokenizer
    has no stemmer and a query word is rarely the exact stored inflection.
    """
    hay = set(TOKEN.findall(row_content.lower()))
    hits = 0
    for t in {x.lower() for x in TOKEN.findall(query) if x.lower() not in STOPWORDS}:
        if any(word.startswith(t) for word in hay):
            hits += 1
    return hits


def expr_or_content(query):
    """OR over the query's CONTENT tokens only.

    Stopwords like "my", "i", "what" occur in a large share of memories, so an OR
    that includes them matches almost everything - which is exactly what wrecks
    specificity on negatives.
    """
    toks = [t for t in TOKEN.findall(query) if t.lower() not in STOPWORDS]
    if not toks:
        toks = TOKEN.findall(query)
    return " OR ".join(quote(t) for t in toks)


def expr_min_matches(query, minimum=2):
    """OR for recall, then keep only rows satisfying at least `minimum` of the
    query's content tokens - specificity without giving up the candidate set."""
    toks = [t for t in TOKEN.findall(query) if t.lower() not in STOPWORDS] or TOKEN.findall(query)
    return (" OR ".join(quote(t) for t in toks), minimum)


STRATEGIES = {
    "current (shipped)": expr_current,
    "OR over all tokens": expr_or,
    "OR over content tokens": expr_or_content,
    "AND over content tokens": expr_and_content,
    "AND then OR fallback": expr_relax,
    "OR content, >=2 match": expr_min_matches,
}


def fetch(conn, match, k):
    return [dict(r) for r in conn.execute(
        f"SELECT m.* FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
        f"WHERE memories_fts MATCH ? {RANK} LIMIT ?", (match, k))]


def score(cases, conn, k, strategy):
    fn = STRATEGIES[strategy]
    groups = {"pos": [], "neg": []}
    for case in cases:
        if case["split"] != "train":
            continue
        built = fn(case["prompt"])
        minimum = None
        if isinstance(built, tuple) and len(built) == 2 and isinstance(built[1], int):
            built, minimum = built
            pairs = (built,)
        else:
            pairs = built if isinstance(built, tuple) else (built,)
        rows = []
        for match in pairs:
            if match is None:
                continue
            rows = fetch(conn, match, k)
            if rows:
                break
        if minimum is not None:
            # Same threshold for negatives as for positives: a negative query
            # matching one token is still a false positive - the answer does not
            # exist in the corpus, so the row returned is wrong either way.
            rows = [r for r in rows
                    if matched_token_count(r.get("content") or "", case["prompt"]) >= minimum]
        scores, rank, _ = grade(case, rows, k)
        key = "neg" if case["expect"] == "empty" else "pos"
        groups[key].append(scores)
    out = {}
    for key, items in groups.items():
        if not items:
            continue
        n = len(items)
        out[key] = {
            "n": n,
            "hit@5": sum(s["hit_at_5"] for s in items) / n,
            "mrr": sum(s["mrr"] for s in items) / n,
            "no-junk@5": sum(s["precision_at_5"] for s in items) / n,
        }
    return out


def main():
    k = 5
    cases = read_jsonl(CASES)
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    try:
        print(f"train split only ({sum(1 for c in cases if c['split']=='train')} cases), k={k}\n")
        header = f"{'strategy':24s} {'pos hit@5':>10s} {'pos MRR':>8s} {'pos junk':>9s} | {'neg spec':>9s} (n)"
        print(header); print("-" * len(header))
        for name in STRATEGIES:
            r = score(cases, conn, k, name)
            p, n_ = r["pos"], r["neg"]
            print(f"{name:24s} {p['hit@5']:10.3f} {p['mrr']:8.3f} {p['no-junk@5']:9.3f} |"
                  f" {n_['hit@5']:9.3f} ({n_['n']})")
    finally:
        conn.close()
    print("\nA screen, not a result. The winner is the candidate worth a round.")


if __name__ == "__main__":
    main()