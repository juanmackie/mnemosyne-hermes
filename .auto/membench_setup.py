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
    payload = corpus.read_bytes() + f"|model={MODEL}|label={label}|membench-v2".encode()
    return hashlib.sha256(payload).hexdigest()


def resolve(target: str, content_to_id: dict[str, str], owner: str) -> str:
    """Map a corpus reference to one memory id, or fail rather than guess."""
    if target in content_to_id:
        return content_to_id[target]
    partial = [content for content in content_to_id if target in content]
    if len(partial) == 1:
        return content_to_id[partial[0]]
    raise SystemExit(
        f"link from {owner!r} names {target!r} which matches {len(partial)} corpus rows")


def add_links(conn, rows, content_to_id: dict[str, str]) -> dict[str, int]:
    """Store the corpus-declared graph edges. Returns requested counts by type."""
    wanted: dict[str, int] = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows:
        source = content_to_id[row["content"]]
        for link in row.get("links", []):
            link_type = link["type"]
            # created_at is written explicitly: the column DEFAULT is
            # CURRENT_TIMESTAMP, whose "YYYY-MM-DD HH:MM:SS" form the Rust reader
            # rejects, and a rejected link timestamp fails the whole recall.
            conn.execute(
                "INSERT INTO memory_links (source_id, target_id, link_type, strength,"
                " reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (source, resolve(link["to"], content_to_id, row["content"]), link_type,
                 float(link.get("strength", 1.0)), "membench fixture", stamp))
            wanted[link_type] = wanted.get(link_type, 0) + 1
    return wanted


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
        wanted = add_links(conn, rows, content_to_id)

    # Read the edges back through the same schema the server will use. INSERT OR
    # IGNORE is how production lost three link types silently, so the fixture
    # inserts strictly and then counts: a rejected type fails the rebuild.
    stored = dict(conn.execute("SELECT link_type, COUNT(*) FROM memory_links GROUP BY link_type"))
    missing = {t: n for t, n in wanted.items() if stored.get(t, 0) != n}
    if missing:
        raise SystemExit(f"links were not stored (schema rejects these types): {missing}")
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
    edges = sum(wanted.values())
    print(f"ingested={len(dated)} superseded_pairs={pairs} expired_rows={expired}"
          f" links={edges} link_types={sorted(wanted)}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    fp = fingerprint(args.corpus, args.label)
    marker = args.db.with_suffix(args.db.suffix + ".fingerprint")
    # Which encoder produced the vectors in this DB. A fingerprint hit already
    # proves the model matches, so the sidecar is written on both paths; the
    # evaluator refuses to score a DB whose encoder it cannot confirm.
    sidecar = Path(str(args.db) + ".model")
    if args.db.exists() and marker.exists() and marker.read_text().strip() == fp \
            and not Path(str(args.db) + "-wal").exists():
        sidecar.write_text(MODEL + "\n")
        print(args.db)
        return 0
    rebuild(args.corpus, args.db, args.namespace)
    marker.write_text(fp)
    sidecar.write_text(MODEL + "\n")
    print(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
