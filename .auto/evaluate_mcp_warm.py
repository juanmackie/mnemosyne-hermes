#!/usr/bin/env python3
"""Steady-state recall latency through ONE warm MCP stdio server.

`evaluate_mcp.py` spawns a fresh `mnemosyne mcp` process per query, so its
latency is dominated by process start (binary load, DB open, migrations check,
ONNX model load). That is what a shell hook pays — but the Hermes agent keeps a
single MCP server alive for the whole session, so what *it* waits on is the
per-call cost inside one process. This script measures that: one server, one DB,
queries fed serially over the same stdin (like a real MCP client), timing only
request-write → response-read.

Reuses the relevance/summary logic from evaluate_mcp.py so quality numbers stay
comparable across surfaces.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate_mcp import relevant, summarize  # noqa: E402


def drain_until_id(proc, wanted: int, timeout: float = 120.0) -> dict | None:
    """Read stdout lines until the JSON-RPC response with `wanted` id arrives."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == wanted:
            return payload
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--namespace", default="project:personal-agent-eval")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=3)
    args = parser.parse_args()

    items = [json.loads(line) for line in args.dataset.read_text().splitlines()
             if line.strip() and not line.startswith("#")]

    with tempfile.TemporaryDirectory() as tmp:
        work_db = Path(tmp) / "warm.db"
        shutil.copy2(args.db, work_db)
        proc = subprocess.Popen(
            [str(args.binary), "--db-path", str(work_db), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)

        def send(payload: dict) -> None:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "method": "initialize", "id": 1})
        if drain_until_id(proc, 1) is None:
            proc.kill()
            err = proc.stderr.read()[-1500:]
            print(f"WARM MCP server failed to initialize: {err}", file=sys.stderr)
            return 2

        rows: list[dict] = []
        next_id = 2
        for index, item in enumerate(items):
            request_id = next_id
            next_id += 1
            started = time.perf_counter()
            send({"jsonrpc": "2.0", "method": "tools/call", "id": request_id,
                  "params": {"name": "mnemosyne_recall", "arguments": {
                      "query": item["query"], "namespace": args.namespace,
                      "max_results": args.limit}}})
            response = drain_until_id(proc, request_id)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if response is None:
                proc.kill()
                print(f"WARM MCP server died on query {item['query']!r}: "
                      f"{proc.stderr.read()[-1500:]}", file=sys.stderr)
                return 2
            try:
                payload = json.loads(response["result"]["content"][0]["text"])
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                payload = {}
            results = []
            for result in payload.get("results", []):
                memory = result.get("memory", result)
                results.append({"id": memory.get("id", ""),
                                "summary": memory.get("summary", ""),
                                "content": memory.get("content", "")})
            rank = None
            for position, result in enumerate(results, 1):
                if relevant(result, item.get("relevant", [])):
                    rank = position
                    break
            if index >= args.warmup:  # first calls pay page-cache + lazy init
                rows.append({"rank": rank, "latency_ms": elapsed_ms,
                             "count": len(results),
                             "category": item.get("category", "uncategorized")})

        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    out = summarize(rows)
    out["dataset"] = args.dataset.name
    out["surface"] = "mcp_warm"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
