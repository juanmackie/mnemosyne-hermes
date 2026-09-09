#!/usr/bin/env python3
"""Build a membench template DB from a membench corpus file.

Generalises .auto/setup_data.py for the memory-behaviour scoreboard:

  age_days        backdate created_at/updated_at/last_accessed_at
  supersedes      explicit superseded_by edge to another row's content
  expires_in_days expires_at = now + N days (negative = already expired)
  kind            "reference" rows keep memory_type=reference; the ingest-split
                  port decides whether retrieval honours that, the corpus only
                  states the intent.

Rows are ingested through the public CLI with --no-enrich, so the build stays
keyless and deterministic. The DB is cached by fingerprint(corpus+model+label).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "bge-small-en-v1.5")


def fingerprint(corpus: Path, label: str) -> str:
    payload = corpus.read_bytes() + f"|model={MODEL}|label={label}|membench-v1".encode()
    return hashlib.sha256(payload).hexdigest()


def rebuild(corpus: Path, db: Path, namespace: str) -> None:
    rows = [json.loads(line) for line in corpus.read_text().splitlines() if line.strip()]
    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        if path.exists():
            path.unlink()

    binary = Path(os.environ.get("MNEMOSYNE_EVAL_BIN", "")
                  or str(ROOT / "target" / "release" / "mnemosyne"))
    if not binary.exists():
        print(f"missing binary: {binary}", file=sys.stderr)
        raise SystemExit(2)

    content_to_id: dict[str, str] = {}
    dated: list[tuple[str, int, int | None]] = []
    for index, row in enumerate(rows, 1):
        cmd = [str(binary), "--db-path", str(db), "remember",
               "--content", row["content"],
               "--namespace", namespace,
               "--importance", str(row.get("importance", 5)),
               "--memory-type", row.get("memory_type", "insight"),
               "--tags", row.get("tags", ""),
               "--no-enrich", "--format", "json"]
        proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        if proc.returncode:
            print(f"corpus row {index} failed: {proc.stderr[-800:]}", file=sys.stderr)
            raise SystemExit(proc.returncode)
        memory_id = json.loads(proc.stdout)["id"]
        content_to_id[row["content"]] = memory_id
        dated.append((memory_id, int(row.get("age_days", 30)), row.get("expires_in_days")))

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(db))
    with conn:
        for memory_id, age_days, expires_in_days in dated:
            ts = (now - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            expires_at = (None if expires_in_days is None else
                          (now + timedelta(days=int(expires_in_days))).strftime("%Y-%m-%dT%H:%M:%SZ"))
            conn.execute(
                "UPDATE memories SET created_at = ?, updated_at = ?, last_accessed_at = ?,"
                " expires_at = ? WHERE id = ?",
                (ts, ts, ts, expires_at, memory_id))
        pairs = 0
        for row in rows:
            old = row.get("supersedes")
            if not old:
                continue
            conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?",
                         (content_to_id[row["content"]], content_to_id[old]))
            pairs += 1
    conn.close()

    # Evaluators copy the main DB file only, so fold the WAL back in.
    checkpoint = sqlite3.connect(str(db))
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    expired = sum(1 for _, _, e in dated if e is not None and int(e) < 0)
    print(f"ingested={len(dated)} superseded_pairs={pairs} expired_rows={expired}",
          file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    fp = fingerprint(args.corpus, args.label)
    marker = args.db.with_suffix(args.db.suffix + ".fingerprint")
    if args.db.exists() and marker.exists() and marker.read_text().strip() == fp \
            and not Path(str(args.db) + "-wal").exists():
        print(args.db)
        return 0
    rebuild(args.corpus, args.db, args.namespace)
    marker.write_text(fp)
    print(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
