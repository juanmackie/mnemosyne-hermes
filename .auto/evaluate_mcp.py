#!/usr/bin/env python3
"""Evaluate recall through the public MCP stdio surface."""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def load_items(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def relevant(result: dict, targets: list[str]) -> bool:
    text = " ".join(
        str(result.get(field, "")) for field in ("id", "summary", "content")
    ).lower()
    return any(target.strip().lower() in text for target in targets if target.strip())


def one_query(
    binary: Path,
    db: Path,
    namespace: str,
    item: dict,
    limit: int,
    hierarchical: bool,
    query_index: int,
    temp_dir: Path,
) -> dict:
    query_db = temp_dir / f"query-{query_index}.db"
    shutil.copy2(db, query_db)
    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": 2,
        "params": {
            "name": "mnemosyne_recall",
            "arguments": {
                "query": item["query"],
                "namespace": namespace,
                "max_results": limit,
                "hierarchical": hierarchical,
            },
        },
    }
    started = time.perf_counter()
    proc = subprocess.run(
        [str(binary), "--db-path", str(query_db), "mcp"],
        input=(json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1})
               + "\n"
               + json.dumps(request)
               + "\n"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if proc.returncode:
        raise RuntimeError(f"MCP recall failed for {item['query']!r}: {proc.stderr[-1000:]}")

    payload = None
    for line in proc.stdout.splitlines():
        try:
            response = json.loads(line)
            if response.get("id") == 2:
                text = response["result"]["content"][0]["text"]
                payload = json.loads(text)
                break
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
    if payload is None:
        raise RuntimeError(f"MCP recall returned no JSON payload for {item['query']!r}")

    results = []
    for result in payload.get("results", []):
        memory = result.get("memory", result)
        results.append(
            {
                "id": memory.get("id", ""),
                "summary": memory.get("summary", ""),
                "content": memory.get("content", ""),
            }
        )
    rank = None
    for index, result in enumerate(results, 1):
        if relevant(result, item.get("relevant", [])):
            rank = index
            break
    return {"rank": rank, "latency_ms": elapsed_ms, "count": len(results)}


def summarize(rows: list[dict]) -> dict:
    ranks = [row["rank"] for row in rows]
    return {
        "queries": len(rows),
        "hit1": sum(rank == 1 for rank in ranks) / len(rows) if rows else 0.0,
        "hit5": sum(rank is not None and rank <= 5 for rank in ranks) / len(rows) if rows else 0.0,
        "mrr": sum((1.0 / rank) if rank else 0.0 for rank in ranks) / len(rows) if rows else 0.0,
        "latency_p50_ms": statistics.median(row["latency_ms"] for row in rows) if rows else 0.0,
        "latency_p95_ms": statistics.quantiles(
            [row["latency_ms"] for row in rows], n=20, method="inclusive"
        )[18]
        if len(rows) >= 2
        else (rows[0]["latency_ms"] if rows else 0.0),
        "empty": sum(row["count"] == 0 for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--namespace", default="project:personal-agent-eval")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--mode", choices=("flat", "hierarchical", "both"), default="both")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    items = load_items(args.dataset)
    modes = [False, True] if args.mode == "both" else [args.mode == "hierarchical"]
    output = {"dataset": str(args.dataset), "items": len(items), "modes": {}}
    for hierarchical in modes:
        label = "hierarchical" if hierarchical else "flat"
        rows: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="mnemosyne-mcp-eval-") as temp_name:
            temp_dir = Path(temp_name)
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = [
                    pool.submit(
                        one_query,
                        args.binary,
                        args.db,
                        args.namespace,
                        item,
                        args.limit,
                        hierarchical,
                        index,
                        temp_dir,
                    )
                    for index, item in enumerate(items)
                ]
                for future in as_completed(futures):
                    rows.append(future.result())
        output["modes"][label] = summarize(rows)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
