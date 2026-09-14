#!/usr/bin/env python3
"""Warm recall latency for the Python-native storage path.

The Rust ``hermes_recall_bench`` was removed by the Python-only runtime pivot,
so the primary metric is measured on the runtime that actually ships:
``MnemosyneClient.storage.recall()`` -> :class:`PythonMemoryStorage`.

Fairness rules baked in (do not tune these between runs):
  * deterministic corpus -- no random ids/contents, so runs are comparable
    and an "improvement" cannot come from a luckier corpus
  * warm-up calls are dropped; serial calls on one warm storage handle
  * corpus + queries are fixed here; the optimization must move the number,
    the number must not be redefined around the optimization

Emits ``METRIC name=value`` lines; primary is ``recall_latency_warm_p95_ms``.
"""
from __future__ import annotations

import os
import shutil
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.realpath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from lib.storage import PythonMemoryStorage  # noqa: E402

N_MEMORIES = int(os.environ.get("BENCH_MEMORIES", "2000"))
REPEATS = int(os.environ.get("BENCH_REPEATS", "300"))
WARMUP = int(os.environ.get("BENCH_WARMUP", "30"))
NAMESPACE = "bench:recall"

TOPICS = ["retrieval", "ranking", "storage", "latency", "graph", "embedding",
          "context", "memory", "cache", "index", "vector", "keyword", "hybrid",
          "recall", "precision", "schema", "migration", "transaction",
          "journal", "cursor"]
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
         "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
         "oscar", "papa", "quebec", "romeo", "sierra", "tango"]

QUERIES = ["note 7:", "retrieval tuning", "ranking batch", "sierra", "latency",
           "graph", "embedding", "cache index", "november", "quebec"]


def corpus() -> list[str]:
    """Deterministic corpus; identical on every run."""
    out = []
    for i in range(N_MEMORIES):
        topic = TOPICS[i % len(TOPICS)]
        word = WORDS[(i * 7) % len(WORDS)]
        out.append(f"note {i}: {topic} tuning {word} batch {i % 97} for the local stack")
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mnemo-recall-bench-")
    try:
        storage = PythonMemoryStorage(os.path.join(tmp, "bench.db"))
        for text in corpus():
            storage.remember(text, NAMESPACE, 5)

        lat: list[float] = []
        total = WARMUP + REPEATS
        for i in range(total):
            q = QUERIES[i % len(QUERIES)]
            t0 = time.perf_counter()
            storage.recall(q, namespace=NAMESPACE, max_results=10)
            dt = (time.perf_counter() - t0) * 1000.0
            if i >= WARMUP:
                lat.append(dt)

        lat.sort()
        idx = min(len(lat) - 1, max(0, int(round(0.95 * len(lat))) - 1))
        p95 = lat[idx]
        p50 = statistics.median(lat)
        mean = statistics.fmean(lat)

        print(f"METRIC recall_latency_warm_p95_ms={p95:.4f}")
        print(f"METRIC recall_latency_warm_p50_ms={p50:.4f}")
        print(f"METRIC recall_latency_warm_mean_ms={mean:.4f}")
        print(f"note corpus={N_MEMORIES} repeats={REPEATS} warmup={WARMUP} ns={NAMESPACE}")
        print(f"note calls={len(lat)}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
