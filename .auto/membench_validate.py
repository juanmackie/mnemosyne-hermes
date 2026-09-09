#!/usr/bin/env python3
"""Validate membench labels before they are scored.

A scoreboard can only rank as well as its labels. Two failure modes were seen
in this session: a filler row that happened to contain a gold phrase, and a
gold phrase that matched several rows, so a "miss" said nothing about retrieval.
This script asserts the labels identify exactly what they claim to, and reports
the queries whose labels are too weak to measure anything.

Usage:
  python3 .auto/membench_validate.py --corpus .auto/membench/corpus_heldout.jsonl \
      --queries .auto/membench/queries_heldout.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STOP = set(
    """a an the of to in on at for and or is are was were be been it its this that with
    by from as i me my you your do does did what when where who how why say said tell
    told have has had not no so about into over under again once which there here they
    them their""".split()
)


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--queries", type=Path, required=True)
    ap.add_argument("--strict", action="store_true", help="fail (exit 2) on weak labels too")
    args = ap.parse_args()

    corpus = [json.loads(l) for l in args.corpus.read_text().splitlines() if l.strip()]
    queries = [json.loads(l) for l in args.queries.read_text().splitlines() if l.strip()]
    contents = [row["content"] for row in corpus]
    # Summary text is searched by the retriever too; a label that matches only the
    # summary is a weaker claim than one that matches the content.
    errors: list[str] = []
    weak: list[str] = []

    for index, item in enumerate(queries):
        label = f"{item.get('category','?')}#{index} {item['query'][:48]!r}"
        golds = item.get("relevant") or []
        if not golds:
            errors.append(f"{label}: no relevant entry")
            continue
        for gold in golds:
            matches = [c for c in contents if gold.lower() in c.lower()]
            if not matches:
                errors.append(f"{label}: gold {gold!r} matches no row")
            elif len(matches) > 1:
                errors.append(
                    f"{label}: gold {gold!r} matches {len(matches)} rows, so rank is ambiguous"
                )
        if item.get("scope") not in (None, "memory", "reference", "all"):
            errors.append(f"{label}: unknown scope {item['scope']!r}")
        for distractor in item.get("distractor") or []:
            if not any(distractor.lower() in c.lower() for c in contents):
                errors.append(f"{label}: distractor {distractor!r} matches no row")
        # A query whose only content word also opens the gold row is a lexical
        # lookup, not a recall test; those are the labels that hid real misses.
        gold_words = set.union(*(words(g) for g in golds))
        if not (words(item["query"]) - gold_words):
            weak.append(f"{label}: every content word is inside the gold phrase")

    categories: dict[str, int] = {}
    for item in queries:
        categories[item.get("category", "?")] = categories.get(item.get("category", "?"), 0) + 1

    for line in errors:
        print(f"ERROR {line}", file=sys.stderr)
    for line in weak:
        print(f"WEAK {line}", file=sys.stderr)
    counts = " ".join(f"{k}={v}" for k, v in sorted(categories.items()))
    print(f"NOTE validated queries={len(queries)} errors={len(errors)} weak={len(weak)} {counts}")
    if errors:
        return 2
    return 2 if (args.strict and weak) else 0


if __name__ == "__main__":
    raise SystemExit(main())
