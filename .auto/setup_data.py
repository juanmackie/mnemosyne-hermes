#!/usr/bin/env python3
"""Build the fixed corpus DB used by autoresearch.

- Ingests .auto/corpus.jsonl through the public `mnemosyne remember` CLI.
- Backdates created_at/updated_at per row (`age_days`) to mimic a real store.
- Applies explicit supersession pairs (`supersedes` -> superseded_by) via SQL.
- Caches the built DB by fingerprint(corpus + embedding model) so iterations
  that only change ranking code do not pay re-ingest cost.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "target" / "release" / "mnemosyne"
DATA_DIR = ROOT / ".auto" / "data"
DB = DATA_DIR / "template.db"
CORPUS = ROOT / ".auto" / "corpus.jsonl"
NAMESPACE = "project:personal-agent-eval"
MODEL = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "bge-small-en-v1.5")


def fingerprint() -> str:
    payload = CORPUS.read_bytes() + f"|model={MODEL}|v3".encode()
    return hashlib.sha256(payload).hexdigest()


def rebuild(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB) + suffix)
        if path.exists():
            path.unlink()

    id_rows: list[tuple[str, int]] = []
    supersede_pairs: list[tuple[str, str]] = []  # (old_id, new_id)

    content_to_id: dict[str, str] = {}
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
        env = dict(os.environ)
        result = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        if result.returncode:
            print(f"failed corpus row {index}: {result.stderr[-1000:]}", file=sys.stderr)
            raise SystemExit(result.returncode)
        memory_id = json.loads(result.stdout)["id"]
        content_to_id[row["content"]] = memory_id
        id_rows.append((memory_id, int(row.get("age_days", 30))))

    # Backdate timestamps so recency mirrors a realistic store.
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(DB))
    with conn:
        for memory_id, age_days in id_rows:
            ts = (now - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE memories SET created_at = ?, updated_at = ?,"
                " last_accessed_at = ? WHERE id = ?",
                (ts, ts, ts, memory_id),
            )
        # Explicit supersession edges from the corpus metadata.
        for row in rows:
            old_content = row.get("supersedes")
            if not old_content:
                continue
            new_id = content_to_id[row["content"]]
            old_id = content_to_id[old_content]
            conn.execute(
                "UPDATE memories SET superseded_by = ? WHERE id = ?",
                (new_id, old_id),
            )
            supersede_pairs.append((old_id, new_id))
    conn.close()
    print(f"ingested={len(id_rows)} superseded_pairs={len(supersede_pairs)}",
          file=sys.stderr)


def main() -> int:
    if not BIN.exists():
        print(f"missing binary: {BIN}", file=sys.stderr)
        return 2
    fp = fingerprint()
    marker = DATA_DIR / "fingerprint.txt"
    if DB.exists() and marker.exists() and marker.read_text().strip() == fp:
        print(DB)
        return 0
    rows = [json.loads(line) for line in CORPUS.read_text().splitlines()
            if line.strip()]
    rebuild(rows)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(fp)
    print(DB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
