#!/usr/bin/env python3
"""Score the memory-behaviour scoreboard (membench).

Each query names `relevant` substrings and optionally `distractor` substrings.
Per-category score (0..1, higher is better):

  temporal_latest_wins  MRR of the current fact
  always_on             MRR of a fact no query can be semantically close to
  reference_noise       MRR of the personal fact when reference docs share its vocabulary
  expired_facts         MRR of the durable fact when an expired rival shares the topic
  multihop_derived      coverage@k of ALL hops an answer needs (joint retrieval)

membench_score = mean of the five category scores, so one weak family cannot be
hidden by a strong one. Distractor ranks are reported as diagnostics: they are
how a fix can look like a win on the target while quietly promoting stale,
expired or reference-only content.

Every query gets its own copy of the DB, because recall writes access counts and
hotness from earlier queries would otherwise leak into later rankings.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CATEGORIES = ("temporal_latest_wins", "always_on", "reference_noise",
              "expired_facts", "multihop_derived")


def matches(result: dict, needles: list[str]) -> bool:
    rid = str(result.get("id", "")).lower()
    haystack = (str(result.get("summary", "")) + "\n"
                + str(result.get("content", ""))).lower()
    for needle in needles:
        cleaned = needle.strip().lower()
        if cleaned and (cleaned == rid or cleaned in haystack):
            return True
    return False


def profile_entries(payload: dict) -> list[dict]:
    """Always-on content the tool returns alongside, not inside, `results`.

    The recall contract puts standing profile content *before* the retrieved
    memories (see the `[always-on]` block in the MCP compact text), so the
    scoreboard scores the always-on class over `profile + results` in that order
    and over nothing else - a profile cannot inflate any other category, and
    because it never occupies a semantic slot it cannot shift a frozen-corpus rank.
    """
    entries = []
    for item in payload.get("profile") or []:
        if isinstance(item, str):
            entries.append({"content": item})
        elif isinstance(item, dict):
            entries.append(item.get("memory", item))
    return entries


def first_rank(results: list[dict], needles: list[str], limit: int) -> int | None:
    for index, result in enumerate(results[:limit], 1):
        if matches(result, needles):
            return index
    return None


def one_query(binary: Path, db: Path, namespace: str, item: dict,
              limit: int, index: int, tmp: Path) -> dict:
    query_db = tmp / f"membench-{index}.db"
    shutil.copy2(db, query_db)
    cmd = [str(binary), "--db-path", str(query_db), "recall",
           "--query", item["query"], "--namespace", namespace,
           "--limit", str(limit), "--format", "json"]
    # Documentation lives in its own recall lane; a query about a vendor manual
    # has to be asked in that lane or it measures suppression, not ranking.
    if item.get("scope"):
        cmd += ["--scope", item["scope"]]
    started = time.perf_counter()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode:
        raise RuntimeError(f"recall failed for {item['query']!r}: {proc.stderr[-800:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bad JSON for {item['query']!r}: {proc.stdout[-400:]}") from exc
    results = payload.get("results", [])
    profile = profile_entries(payload)

    targets = item.get("relevant", [])
    category = item.get("category", "uncategorized")
    row = {"category": category, "latency_ms": elapsed_ms, "count": len(results),
           "profile_items": len(profile)}
    if category == "multihop_derived":
        ranks = [first_rank(results, [needle], limit) for needle in targets]
        row["coverage"] = sum(1 for rank in ranks if rank is not None) / len(ranks) if ranks else 0.0
        row["score"] = row["coverage"]
        row["hop_hit5"] = row["coverage"]
    else:
        haystack = profile + results if category == "always_on" else results
        rank = first_rank(haystack, targets, limit + len(profile))
        row["rank"] = rank
        row["score"] = (1.0 / rank) if rank else 0.0
    distractors = item.get("distractor", [])
    row["distractor_rank"] = first_rank(results, distractors, limit) if distractors else None
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--workers", type=int, default=1,
                    help="keep 1: concurrent query processes make the local encoder\n" \
                         "non-reproducible (same code, +-0.02 score). Serial is exact,\n" \
                         "16 queries take ~40s, and it matches how Hermes calls recall")
    ap.add_argument("--prefix", required=True, help="metric prefix, e.g. membench_heldout")
    args = ap.parse_args()

    # The embeddings baked into the DB were produced by one encoder. Recalling
    # with a different one still returns confident, stable, meaningless numbers -
    # observed as a reproducible 0.6383 where the pinned stack scores 0.6583.
    # Refuse instead of scoring a different system.
    model = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "bge-small-en-v1.5")
    provenance = Path(str(args.db) + ".model")
    if not provenance.exists():
        print(f"ERROR: {args.db} has no encoder provenance ({provenance}); "
              "rebuild it with membench_setup.py", file=sys.stderr)
        return 2
    built_with = provenance.read_text().strip()
    if built_with != model:
        print(f"ERROR: encoder mismatch. {args.db} embeddings were built with "
              f"{built_with!r} but this run recalls with {model!r}. Export "
              f"MNEMOSYNE_EMBEDDING_MODEL={built_with} or rebuild the DB; a "
              "mismatch is reproducible and scores the wrong system.", file=sys.stderr)
        return 2
    print(f"NOTE encoder={model} db={args.db.name}")

    items = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(lambda pair: one_query(
                args.binary, args.db, args.namespace, pair[1], args.limit,
                pair[0], tmp), enumerate(items)))

    by_category: dict[str, list[dict]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)

    category_scores: dict[str, float] = {}
    for name, group in sorted(by_category.items()):
        score = statistics.mean(r["score"] for r in group)
        category_scores[name] = score
        leaks = [r for r in group if r.get("distractor_rank") is not None
                 and (r.get("rank") is None or r["distractor_rank"] <= r["rank"])]
        print(f"METRIC {args.prefix}_{name}={score:.6f}", file=sys.stderr)
        print(f"note {name}: n={len(group)} score={score:.4f} "
              f"distractor_outranks_target={len(leaks)}/{len(group)}", file=sys.stderr)
    for name in CATEGORIES:
        category_scores.setdefault(name, 0.0)

    overall = statistics.mean(category_scores[name] for name in CATEGORIES)
    base = args.prefix
    print(f"METRIC {base}_score={overall:.6f}")
    for name, value in sorted(category_scores.items()):
        print(f"METRIC {base}_{name}={value:.6f}")
    print(f"METRIC {base}_latency_p95_ms="
          f"{sorted(r['latency_ms'] for r in rows)[int(0.95 * (len(rows) - 1))]:.3f}")
    print(f"METRIC {base}_empty={sum(1 for r in rows if r['count'] == 0)}")
    # The profile is capped on purpose: an unbounded "put everything in the
    # profile" would trivially win the always-on class, so its size stays watched.
    print(f"METRIC {base}_profile_items="
          f"{statistics.mean(r.get('profile_items', 0) for r in rows):.3f}")
    # Full per-query detail for post-mortems.
    detail = args.dataset.with_suffix(".detail.jsonl")
    detail.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
