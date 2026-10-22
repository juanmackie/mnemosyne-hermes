#!/usr/bin/env python3
"""Replay the recovered retrieval fixtures against the pinned engine.

The corpus, query text, relevance labels and supersession pairs come from
`b548943:.auto/`, where a Rust-era harness used them. That harness shelled out
to the retired Rust binary (`target/release/mnemosyne`), so it cannot run here.
This runner keeps the data and the metric and replaces only the invocation
layer with the engine's public `Mnemosyne` facade.

What it does, in the original order:

1. Ingest `corpus.jsonl` into a throwaway engine database.
2. Apply the `supersedes` pairs through `Mnemosyne.invalidate`.
3. Backdate `created_at`/`timestamp` by the row's `age_days`.
4. Replay each query against a per-query copy of that database, so recall
   hotness from an earlier query cannot leak into a later ranking.
5. Report Hit@1, Hit@5, MRR and per-category MRR. A result matches a target
   when the target is a substring of its id, summary or content.

The engine keeps connections in a module thread-local and offers no `close()`,
so teardown tracks `sqlite3.connect` and resets that local, as
`tests/test_engine_recall_scope.py` does. Without it, Windows keeps the
database locked and the per-query copy fails.

Run with the pinned engine installed:

    python bench/retrieval/run_eval.py --dataset bench/retrieval/eval_dev.jsonl
    python bench/retrieval/run_eval.py --all

This is a synthetic 177-row corpus with 27 queries per split. It measures
ranking on those fixtures, not production retrieval quality.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

SESSION_ID = "retrieval-fixture-replay"
SCOPE = "session"
HERE = Path(__file__).resolve().parent

_REAL_CONNECT = sqlite3.connect
_CONNECTIONS: list[tuple[sqlite3.Connection, int]] = []


def _tracking_connect(*args, **kwargs):
    conn = _REAL_CONNECT(*args, **kwargs)
    _CONNECTIONS.append((conn, threading.get_ident()))
    return conn


def release_engine() -> None:
    """Close this thread's tracked connections and drop the engine's local."""
    for conn, owner_thread in _CONNECTIONS:
        if owner_thread == threading.get_ident():
            with contextlib.suppress(Exception):
                conn.close()
    _CONNECTIONS.clear()
    for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
        module = sys.modules.get(name)
        local = getattr(module, "_thread_local", None)
        if local is not None:
            local.conn = None


def load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def require_engine():
    try:
        from mnemosyne.core.memory import Mnemosyne
    except ImportError as exc:
        raise SystemExit(
            "mnemosyne-memory engine not importable; install ./integrations/hermes-provider first"
        ) from exc
    return Mnemosyne


def seed(Mnemosyne, db_path: Path, corpus: list[dict]) -> None:
    """Ingest the corpus, then apply supersession and backdating."""
    store = Mnemosyne(session_id=SESSION_ID, db_path=db_path)
    ids: dict[str, str] = {}
    for row in corpus:
        content = row["content"]
        ids[content] = store.remember(
            content,
            importance=min(max(float(row.get("importance", 5)) / 10.0, 0.0), 1.0),
            metadata={
                "tags": row.get("tags", ""),
                "memory_type": row.get("memory_type", ""),
            },
            scope=SCOPE,
        )
    for row in corpus:
        old = row.get("supersedes")
        if old and old in ids:
            store.invalidate(ids[old], replacement_id=ids[row["content"]])
    release_engine()
    _backdate(db_path, corpus, ids)


def _backdate(db_path: Path, corpus: list[dict], ids: dict[str, str]) -> None:
    conn = _REAL_CONNECT(db_path)
    try:
        now = datetime.now(UTC)
        for row in corpus:
            created = (now - timedelta(days=float(row.get("age_days", 0)))).isoformat()
            conn.execute(
                "UPDATE working_memory SET created_at=?, timestamp=? WHERE id=?",
                (created, created, ids[row["content"]]),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _copy_db(template: Path, target: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(template) + suffix)
        if source.exists():
            shutil.copy2(source, Path(str(target) + suffix))


def rank_of(results: list[dict], targets: list[str]) -> int | None:
    for index, result in enumerate(results, 1):
        rid = str(result.get("id", "")).lower()
        text = " ".join(str(result.get(key, "")) for key in ("summary", "content")).lower()
        for target in targets:
            needle = target.strip().lower()
            if not needle:
                continue
            if needle == rid or needle in text:
                return index
    return None


def evaluate(
    Mnemosyne, template: Path, dataset: list[dict], limit: int, details: bool = False
) -> dict:
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="mnemosyne-fixture-") as scratch:
        scratch_path = Path(scratch)
        for index, item in enumerate(dataset):
            query_db = scratch_path / f"query-{index}.db"
            _copy_db(template, query_db)
            store = Mnemosyne(session_id=SESSION_ID, db_path=query_db)
            started = time.perf_counter()
            try:
                results = store.recall(item["query"], top_k=limit)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
            finally:
                release_engine()
            rank = rank_of(results, item.get("relevant", []))
            rows.append(
                {
                    "rank": rank,
                    "latency_ms": elapsed_ms,
                    "count": len(results),
                    "category": item.get("category", "uncategorized"),
                }
            )
            if details:
                top = results[0].get("content", "")[:90] if results else "<no results>"
                print(
                    f"  [{item.get('category', '?'):<18}] rank={rank} "
                    f"n={len(results)} q={item['query'][:70]!r} top={top!r}"
                )
    return summarize(rows)


def summarize(rows: list[dict]) -> dict:
    ranks = [row["rank"] for row in rows]
    out = {
        "queries": len(rows),
        "hit1": sum(rank == 1 for rank in ranks) / len(rows) if rows else 0.0,
        "hit5": sum(rank is not None and rank <= 5 for rank in ranks) / len(rows) if rows else 0.0,
        "mrr": sum((1.0 / rank) if rank else 0.0 for rank in ranks) / len(rows) if rows else 0.0,
        "latency_p50_ms": statistics.median(row["latency_ms"] for row in rows) if rows else 0.0,
        "empty": sum(row["count"] == 0 for row in rows),
        "by_category": {},
    }
    for category in sorted({row["category"] for row in rows}):
        subset = [row for row in rows if row["category"] == category]
        cranks = [row["rank"] for row in subset]
        out["by_category"][category] = {
            "queries": len(subset),
            "hit1": sum(rank == 1 for rank in cranks) / len(subset),
            "mrr": sum((1.0 / rank) if rank else 0.0 for rank in cranks) / len(subset),
        }
    return out


def format_report(name: str, report: dict) -> str:
    lines = [
        f"{name}: {report['queries']} queries, "
        f"hit@1={report['hit1']:.4f} hit@5={report['hit5']:.4f} "
        f"mrr={report['mrr']:.4f} empty={report['empty']}"
    ]
    for category, values in report["by_category"].items():
        lines.append(
            f"  {category:<20} n={values['queries']:<3} "
            f"hit@1={values['hit1']:.4f} mrr={values['mrr']:.4f}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=HERE / "corpus.jsonl")
    parser.add_argument("--dataset", type=Path, default=HERE / "eval_dev.jsonl")
    parser.add_argument("--all", action="store_true", help="replay dev and both heldout splits")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json-out", type=Path, help="write per-split JSON here")
    parser.add_argument("--keep-template", type=Path, help="copy the seeded database here")
    parser.add_argument("--details", action="store_true", help="print per-query rank and top hit")
    args = parser.parse_args()

    datasets = (
        [
            HERE / "eval_dev.jsonl",
            HERE / "eval_heldout_a.jsonl",
            HERE / "eval_heldout_b.jsonl",
        ]
        if args.all
        else [args.dataset]
    )

    with tempfile.TemporaryDirectory(prefix="mnemosyne-fixture-seed-") as scratch:
        os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
        os.environ["MNEMOSYNE_DATA_DIR"] = str(Path(scratch) / "data")
        os.environ["HERMES_HOME"] = scratch

        sqlite3.connect = _tracking_connect
        try:
            Mnemosyne = require_engine()
            corpus = load_rows(args.corpus)
            template = Path(scratch) / "template.db"
            started = time.perf_counter()
            seed(Mnemosyne, template, corpus)
            seed_s = time.perf_counter() - started
            print(
                f"seeded {len(corpus)} rows in {seed_s:.2f}s "
                f"({sum(1 for row in corpus if row.get('supersedes'))} supersession pairs)"
            )
            if args.keep_template:
                shutil.copy2(template, args.keep_template)
                print(f"template copied to {args.keep_template}")

            reports = {}
            for dataset_path in datasets:
                dataset = load_rows(dataset_path)
                report = evaluate(Mnemosyne, template, dataset, args.limit, args.details)
                reports[dataset_path.stem] = report
                print(format_report(dataset_path.stem, report))
        finally:
            release_engine()
            sqlite3.connect = _REAL_CONNECT

        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(
                    {
                        "corpus": str(args.corpus),
                        "limit": args.limit,
                        "reports": reports,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
