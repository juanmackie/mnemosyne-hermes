#!/usr/bin/env python3
"""Warm-server recall latency: ONE `mnemosyne mcp` process, serial calls,
first 3 dropped as warm-up. This is the latency a live Hermes agent waits
on; evaluate_mcp.py's per-query spawn measures cold process cost instead.
Prints `METRIC <metric-name>=<p95_ms>` (and p50 to stderr for logs)."""
import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--dataset", type=Path, action="append", required=True)
    ap.add_argument("--namespace", default="project:personal-agent-eval")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--metric", default="warm_mcp_p95_ms")
    args = ap.parse_args()

    items = []
    for ds in args.dataset:
        for line in Path(ds).read_text().splitlines():
            if line.strip():
                items.append(json.loads(line))

    with tempfile.TemporaryDirectory(prefix="mnemosyne-warm-mcp-") as tmp:
        db = Path(tmp) / "eval.db"
        shutil.copy2(args.db, db)  # writes (trace/bookkeeping) stay in tmp
        proc = subprocess.Popen(
            [str(args.binary), "--db-path", str(db), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        try:
            def send(payload: dict) -> None:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()

            def await_id(req_id: int) -> None:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        raise RuntimeError("MCP server closed stdout")
                    try:
                        if json.loads(line).get("id") == req_id:
                            return
                    except json.JSONDecodeError:
                        continue

            send({"jsonrpc": "2.0", "method": "initialize", "id": 0})
            await_id(0)

            latencies = []
            for index, item in enumerate(items):
                started = time.perf_counter()
                send({
                    "jsonrpc": "2.0", "method": "tools/call", "id": index + 1,
                    "params": {"name": "mnemosyne_recall", "arguments": {
                        "query": item["query"], "namespace": args.namespace,
                        "max_results": args.limit, "compact": False,
                    }},
                })
                await_id(index + 1)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if index >= args.warmup:
                    latencies.append(elapsed_ms)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    p95 = (statistics.quantiles(latencies, n=20, method="inclusive")[18]
           if len(latencies) >= 2 else max(latencies))
    print(f"METRIC {args.metric}={p95:.3f}")
    print(f"warm-mcp n={len(latencies)} "
          f"p50={statistics.median(latencies):.3f} p95={p95:.3f}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
