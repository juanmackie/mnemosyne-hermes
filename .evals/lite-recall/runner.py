#!/usr/bin/env python3
"""Deterministic, per-case warm recall latency eval; stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FLOW = ROOT / ".evals" / "lite-recall"
SOURCE_ROOT = Path(os.environ.get("MNEMOSYNE_EVAL_SOURCE", ROOT / "src")).resolve()
if not (SOURCE_ROOT / "mnemosyne_lite" / "storage.py").is_file():
    raise RuntimeError(f"MNEMOSYNE_EVAL_SOURCE must contain mnemosyne_lite/storage.py: {SOURCE_ROOT}")
sys.path.insert(0, str(SOURCE_ROOT))

from mnemosyne_lite.storage import PythonMemoryStorage  # noqa: E402

SEED = 42
CORPUS_SIZE = 3000
WORDS = (
    "project alpha milestone review deploy staging cache index query router agent memory "
    "session token stream buffer ledger atlas beacon cipher drift ember flux grove harbor ivy "
    "junction kernel lumen meadow north oak pine quartz ridge stone timber umbra vale wheat "
    "xenon yarn zenith amber brass copper delta echo frost glacier helm inlet jade kite lotus "
    "magnet nest onyx prism quilt raven solar terra unity vapor willow pixel frame dock lane "
    "port signal tower bridge cloud rain snow wind leaf root branch seed bloom field river ocean "
    "sand cliff cave forge anvil hammer nail plank beam arch dome spire stair hall gate wall "
    "floor roof window door key lock chain rope sail mast oar helm deck hull anchor buoy tide "
    "wave foam shell pearl coral reef shark whale dolphin seal otter crab shrimp squid octopus "
    "turtle frog toad newt snake lizard gecko eagle hawk falcon owl robin sparrow finch wren "
    "lark dove crow raven goose duck swan heron crane stork ibis flamingo pelican gull tern "
    "puffin penguin albatross"
).split()


def read_cases() -> list[dict[str, Any]]:
    path = FLOW / "cases.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)) or any(not case.get("query") for case in cases):
        raise ValueError("cases.jsonl must have unique ids and nonempty queries")
    return cases


def build_corpus(store: PythonMemoryStorage) -> None:
    rng = random.Random(SEED)
    planted_xyl = set(rng.sample(range(CORPUS_SIZE), 7))
    planted_phrase = set(rng.sample(range(CORPUS_SIZE), 2))
    for index in range(CORPUS_SIZE):
        sentence = lambda: " ".join(  # noqa: E731
            rng.choice(WORDS) for _ in range(rng.randint(8, 14))
        ).capitalize() + "."
        content = f"Memory {index}: {sentence()} {sentence()}"
        if index in planted_xyl:
            content += " xylophone"
        if index in planted_phrase:
            content += " midnight lantern protocol"
        namespace = "agent:hermes" if rng.random() < 0.8 else rng.choice(
            ["session:x", "agent:other"]
        )
        store.remember(content, namespace=namespace, importance=rng.randint(1, 10))


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot compute percentile of empty values")
    index = (len(ordered) - 1) * pct / 100
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def grade(rows: list[dict[str, Any]], expected: dict[str, Any]) -> tuple[int, list[str]]:
    issues: list[str] = []
    count = len(rows)
    if "exact_count" in expected and count != expected["exact_count"]:
        issues.append(f"count={count}, expected {expected['exact_count']}")
    if "min_count" in expected and count < expected["min_count"]:
        issues.append(f"count={count}, expected at least {expected['min_count']}")
    if "max_count" in expected and count > expected["max_count"]:
        issues.append(f"count={count}, expected at most {expected['max_count']}")
    for row in rows:
        content = row["content"].casefold()
        for token in expected.get("content_tokens", []):
            if token.casefold() not in content:
                issues.append(f"result {row['id']} lacks token {token!r}")
        if "namespace" in expected and row["namespace"] != expected["namespace"]:
            issues.append(f"result {row['id']} has wrong namespace {row['namespace']!r}")
        if "min_importance" in expected and row["importance"] < expected["min_importance"]:
            issues.append(f"result {row['id']} is below importance floor")
    return (0 if issues else 1), issues


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="baseline", help="output variant (baseline, v1, ...)")
    parser.add_argument("--reps", type=int, default=60, help="measured calls per case")
    parser.add_argument("--pilot", action="store_true", help="run 3 cases once under pilot/")
    parser.add_argument(
        "--output-dir",
        help="internal output directory relative to this eval flow (used by replicate.py)",
    )
    args = parser.parse_args()
    if args.reps < 1:
        parser.error("--reps must be >= 1")
    variant = "pilot" if args.pilot else args.variant
    if not variant.replace("_", "").replace("-", "").isalnum():
        parser.error("variant may contain only letters, digits, hyphens, and underscores")
    cases = read_cases()
    if args.pilot:
        cases = [cases[index] for index in (0, 1, 3)]
        reps = 1
    else:
        reps = args.reps
    if args.output_dir:
        relative_out = Path(args.output_dir)
        if relative_out.is_absolute() or ".." in relative_out.parts:
            parser.error("--output-dir must stay inside the eval flow")
        out_dir = FLOW / relative_out
        if not out_dir.resolve().is_relative_to(FLOW.resolve()):
            parser.error("--output-dir must stay inside the eval flow")
    else:
        out_dir = FLOW / variant
    traces_dir = out_dir / "traces"
    results_path = out_dir / "results.jsonl"
    done: set[tuple[str, int]] = set()
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done.add((row["prompt_id"], row["rep"]))

    case_file = (FLOW / "cases.jsonl").read_bytes()
    source_file = (SOURCE_ROOT / "mnemosyne_lite" / "storage.py").read_bytes()
    runner_file = Path(__file__).read_bytes()
    rows_written = 0

    fd, db_path = tempfile.mkstemp(prefix=".corpus-", suffix=".db", dir=FLOW)
    os.close(fd)
    try:
        store = PythonMemoryStorage(db_path)
        try:
            build_corpus(store)
            if store.count() != CORPUS_SIZE:
                raise RuntimeError(f"corpus has {store.count()} rows, expected {CORPUS_SIZE}")
            for case in cases:
                query = case["query"]
                kwargs = {
                    "namespace": case.get("namespace"),
                    "max_results": case["max_results"],
                    "min_importance": case.get("min_importance"),
                }
                for _ in range(case.get("warmup", 10)):
                    store.recall(query, **kwargs)
                final_rows: list[dict[str, Any]] = []
                for rep in range(reps):
                    if (case["id"], rep) in done:
                        continue
                    start = time.perf_counter_ns()
                    try:
                        final_rows = store.recall(query, **kwargs)
                    except Exception as exc:
                        append_jsonl(
                            out_dir / "errors.jsonl",
                            {
                                "prompt_id": case["id"],
                                "rep": rep,
                                "failure_class": "harness_error",
                                "error": f"{type(exc).__name__}: {exc}",
                                "retry_count": 0,
                                "usage": None,
                            },
                        )
                        raise
                    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
                    correct, issues = grade(final_rows, case["expected"])
                    result = {
                        "prompt_id": case["id"],
                        "rep": rep,
                        "prompt": json.dumps({"query": query, **kwargs}, sort_keys=True),
                        "tags": case["tags"],
                        "status": "ok",
                        "grade": {"latency_ms": elapsed_ms},
                        "correct": correct,
                        "latency_s": elapsed_ms / 1000,
                        "meta": {
                            "corpus_rows": CORPUS_SIZE,
                            "warmup_calls": case.get("warmup", 10),
                            "ground_truth": "seeded fixture plus predicate checks",
                            "case_version": "1",
                            "correctness_reason": "; ".join(issues) if issues else "All checks passed.",
                        },
                    }
                    trace = [
                        {"role": "user", "content": f"Recall query: {query}"},
                        {"role": "tool_call", "name": "PythonMemoryStorage.recall", "content": json.dumps(kwargs, sort_keys=True)},
                        {"role": "tool_result", "content": json.dumps({"count": len(final_rows), "ids": [r["id"] for r in final_rows]}, sort_keys=True)},
                        {"role": "assistant", "content": f"Measured warm recall in {elapsed_ms:.6f} ms; correctness={'pass' if correct else 'fail'}: {result['meta']['correctness_reason']}"},
                    ]
                    write_json(traces_dir / f"{case['id']}_rep{rep}.json", trace)
                    append_jsonl(results_path, result)
                    done.add((case["id"], rep))
                    rows_written += 1
        finally:
            store.close()
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass

    # Re-read all rows so a resumed run produces complete, honest summary stats.
    variant_rows = [
        json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    expected_keys = {(case["id"], rep) for case in cases for rep in range(reps)}
    actual_keys = {(row["prompt_id"], row["rep"]) for row in variant_rows}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        raise RuntimeError(f"incomplete run: {len(missing)} result rows missing; rerun to resume")
    summary_rows = [row for row in variant_rows if row["status"] == "ok"]
    all_ms = [row["grade"]["latency_ms"] for row in summary_rows]
    case_summary: dict[str, Any] = {}
    for case in cases:
        values = [row["grade"]["latency_ms"] for row in summary_rows if row["prompt_id"] == case["id"]]
        correct_count = sum(
            row["correct"] for row in summary_rows if row["prompt_id"] == case["id"]
        )
        case_summary[case["id"]] = {
            "reps": len(values),
            "correct": correct_count,
            "correct_rate": correct_count / len(values),
            "p50_ms": percentile(values, 50),
            "p95_ms": percentile(values, 95),
            "p99_ms": percentile(values, 99),
        }
    correct_total = sum(row["correct"] for row in summary_rows)
    summary = {
        "description": "Warm repeated-query recall latency on a seeded synthetic corpus",
        "target": "code",
        "cases": len(cases),
        "reps": reps,
        "rows": len(summary_rows),
        "correct": correct_total,
        "correct_rate": correct_total / len(summary_rows),
        "latency_ms": {
            "p50": percentile(all_ms, 50),
            "p95": percentile(all_ms, 95),
            "p99": percentile(all_ms, 99),
            "min": min(all_ms),
            "max": max(all_ms),
        },
        "by_case": case_summary,
        "environment": {
            "commit": git_commit(),
            "python": platform.python_version(),
            "sqlite": __import__("sqlite3").sqlite_version,
            "platform": platform.platform(),
            "runner_sha256": hashlib.sha256(runner_file).hexdigest(),
            "cases_sha256": hashlib.sha256(case_file).hexdigest(),
            "storage_sha256": hashlib.sha256(source_file).hexdigest(),
            "source_root": str(SOURCE_ROOT),
            "seed": SEED,
            "corpus_rows": CORPUS_SIZE,
            "retries": 0,
        },
    }
    write_json(out_dir / "summary.json", summary)
    print(
        f"{variant}: {len(summary_rows)}/{len(expected_keys)} rows, "
        f"correct {correct_total}/{len(summary_rows)}, "
        f"warm p50 {summary['latency_ms']['p50']:.4f} ms, "
        f"p99 {summary['latency_ms']['p99']:.4f} ms; appended {rows_written}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
