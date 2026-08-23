#!/usr/bin/env python3
"""Build the fixed, query-independent corpus used by autoresearch."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "target" / "release" / "mnemosyne"
DATA_DIR = ROOT / ".auto" / "data"
DB = DATA_DIR / "memory.db"
CORPUS = ROOT / ".auto" / "corpus.jsonl"
NAMESPACE = "project:personal-agent-eval"


def main() -> int:
    if not BIN.exists():
        print(f"missing binary: {BIN}; build with cargo build --release --bin mnemosyne", file=sys.stderr)
        return 2
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB) + suffix)
        if path.exists():
            path.unlink()

    rows = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]
    for index, row in enumerate(rows, 1):
        cmd = [
            str(BIN), "--db-path", str(DB), "remember",
            "--content", row["content"],
            "--namespace", NAMESPACE,
            "--importance", str(row.get("importance", 5)),
            "--memory-type", row.get("memory_type", "insight"),
            "--tags", row.get("tags", ""),
            "--no-enrich", "--format", "json",
        ]
        result = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            print(f"failed corpus row {index}: {result.stderr[-1000:]}", file=sys.stderr)
            return result.returncode
        if index % 25 == 0 or index == len(rows):
            print(f"ingested {index}/{len(rows)}", file=sys.stderr)
    print(DB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
