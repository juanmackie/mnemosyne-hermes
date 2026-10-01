#!/usr/bin/env python3
"""Diagnostic: would this eval detect a win? Run the same cases through OR semantics.

The baseline scores 0.000 on all 81 positives. Before that number is believed,
the grader has to be shown *live*: push the identical cases through a retriever
that gets them right and confirm the score moves. If both are 0.000 the grader
is broken, not the retriever.

`_fts_match_expression` joins every token as a quoted phrase, and space-joined
FTS5 terms are an implicit AND. This script keeps the quoting identical and only
switches the connective to OR, which is the shape the pre-pull substring search
had (plus its progressive relaxation). Same corpus, same labels, same grader —
imported from runner.py, not reimplemented.

    python3 diagnose_or_semantics.py
"""

import json
import re
import sqlite3
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runner import grade, wilson, read_jsonl, CORPUS_DB, CASES  # noqa: E402

TOKEN = re.compile(r"[^\W_]+")


def or_match(query):
    tokens = TOKEN.findall(query)
    if not tokens:
        return None
    return " OR ".join('"' + t + '"' for t in tokens)


def run(conn, case, k):
    match = or_match(case["prompt"])
    if match is None:
        return []
    return [
        dict(row)
        for row in conn.execute(
            "SELECT m.* FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts), m.importance DESC, "
            "m.created_at DESC, m.id LIMIT ?",
            (match, k),
        )
    ]


def main():
    k = 5
    conn = sqlite3.connect(str(CORPUS_DB))
    conn.row_factory = sqlite3.Row
    cases = read_jsonl(CASES)
    groups = {}
    try:
        for case in cases:
            rows = run(conn, case, k)
            scores, rank, _ = grade(case, rows, k)
            split = ("POSITIVE (train)" if case["split"] == "train" else "POSITIVE (test)") \
                if case["expect"] == "hit" else "NEGATIVE (test)"
            groups.setdefault(split, []).append((case, scores, rank, len(rows)))
    finally:
        conn.close()

    print(f"OR-joined MATCH, k={k} — diagnostic only, not a shipped variant\n")
    for label in ("POSITIVE (train)", "POSITIVE (test)", "NEGATIVE (test)"):
        items = groups.get(label, [])
        if not items:
            continue
        n = len(items)
        for metric in ("hit_at_5", "hit_at_1", "mrr", "precision_at_5"):
            mean = sum(s[metric] for _, s, _, _ in items) / n
            print(f"  {label:18s} {metric:14s} {mean:.3f}")
        passes = sum(1 for _, s, _, _ in items if s["hit_at_5"] >= 0.5)
        p, lo, hi = wilson(passes, n)
        print(f"  {label:18s} hit@5 {passes}/{n} = {p:.3f} [95% CI {lo:.3f}-{hi:.3f}]")
        empty = sum(1 for *_, n_rows in items if n_rows == 0)
        print(f"  {label:18s} returned zero rows for {empty}/{n} cases\n")

    print("Cases still missed under OR (first 10), with what was returned:")
    shown = 0
    for case, scores, rank, n_rows in groups.get("POSITIVE (test)", []) + groups.get("POSITIVE (train)", []):
        if scores["hit_at_5"] < 0.5 and shown < 10:
            print(f"  {case['id']:14s} {case['prompt'][:52]:54s} rows={n_rows}")
            shown += 1


if __name__ == "__main__":
    main()