#!/usr/bin/env python3
"""Evaluate real-query retrieval on fixed JSONL sets via the public CLI.

Reports Hit@1/Hit@5, MRR, latency percentiles, and a per-category MRR
breakdown so ranking regressions can be localized. Relevance labels are
query-independent substrings; never edited by the optimization loop.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def load_items(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def relevant(result: dict, targets: list[str]) -> bool:
    rid = str(result.get("id", "")).lower()
    summary = str(result.get("summary", "")).lower()
    content = str(result.get("content", "")).lower()
    for target in targets:
        needle = target.strip().lower()
        if not needle:
            continue
        if needle == rid or needle in summary or needle in content:
            return True
    return False


def one_query(binary: Path, db: Path, namespace: str, item: dict,
              limit: int, query_index: int, temp_dir: Path) -> dict:
    # Recall increments access counts. Isolate every query so hotness from
    # earlier queries cannot leak into later rankings.
    query_db = temp_dir / f"query-{query_index}.db"
    shutil.copy2(db, query_db)
    cmd = [
        str(binary), "--db-path", str(query_db), "recall",
        "--query", item["query"], "--namespace", namespace,
        "--limit", str(limit), "--format", "json",
    ]
    started = time.perf_counter()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode:
        raise RuntimeError(
            f"recall failed for {item['query']!r}: {proc.stderr[-1000:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON for {item['query']!r}: {proc.stdout[-500:]}") from exc
    results = payload.get("results", [])
    rank = None
    for index, result in enumerate(results, 1):
        if relevant(result, item.get("relevant", [])):
            rank = index
            break
    return {"rank": rank, "latency_ms": elapsed_ms, "count": len(results),
            "category": item.get("category", "uncategorized")}


def summarize(rows: list[dict]) -> dict:
    ranks = [row["rank"] for row in rows]
    out = {
        "queries": len(rows),
        "hit1": sum(rank == 1 for rank in ranks) / len(rows) if rows else 0.0,
        "hit5": sum(rank is not None and rank <= 5 for rank in ranks) / len(rows) if rows else 0.0,
        "mrr": sum((1.0 / rank) if rank else 0.0 for rank in ranks) / len(rows) if rows else 0.0,
        "latency_p50_ms": statistics.median(row["latency_ms"] for row in rows) if rows else 0.0,
        "latency_p95_ms": statistics.quantiles([row["latency_ms"] for row in rows], n=20, method="inclusive")[18] if len(rows) >= 2 else (rows[0]["latency_ms"] if rows else 0.0),
        "empty": sum(row["count"] == 0 for row in rows),
        "by_category": {},
    }
    categories = sorted({row["category"] for row in rows})
    for category in categories:
        subset = [row for row in rows if row["category"] == category]
        cranks = [row["rank"] for row in subset]
        out["by_category"][category] = {
            "queries": len(subset),
            "mrr": sum((1.0 / r) if r else 0.0 for r in cranks) / len(cranks),
            "hit5": sum(r is not None and r <= 5 for r in cranks) / len(cranks),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--namespace", default="project:personal-agent-eval")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    items = load_items(args.dataset)
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="mnemosyne-eval-") as temp_name:
        temp_dir = Path(temp_name)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [
                pool.submit(one_query, args.binary, args.db, args.namespace,
                            item, args.limit, index, temp_dir)
                for index, item in enumerate(items)
            ]
            for future in as_completed(futures):
                rows.append(future.result())
    output = {"dataset": str(args.dataset), "items": len(items),
              "mode": "flat"}
    output.update(summarize(rows))
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        raise SystemExit(1)
