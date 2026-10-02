#!/usr/bin/env python3
"""Run independent evaluator processes and aggregate them into one variant."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from runner import CORPUS_SIZE, FLOW, percentile, read_cases, write_json

RUNNER = FLOW / "runner.py"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="baseline", help="aggregate output variant (baseline, v1, ...)")
    parser.add_argument("--trials", type=int, default=5, help="independent Python processes to run")
    parser.add_argument("--reps", type=int, default=60, help="measured calls per case per process")
    parser.add_argument(
        "--archive-existing",
        action="store_true",
        help="preserve existing aggregate files under runs/pre-repeat before replacing them",
    )
    args = parser.parse_args()
    if args.trials < 2 or args.reps < 1:
        parser.error("--trials must be >= 2 and --reps must be >= 1")
    if not args.variant.replace("_", "").replace("-", "").isalnum():
        parser.error("variant may contain only letters, digits, hyphens, and underscores")

    variant_dir = FLOW / args.variant
    runs_dir = variant_dir / "runs"
    aggregate_results = variant_dir / "results.jsonl"
    aggregate_summary = variant_dir / "summary.json"
    trial_count = args.trials
    cases = read_cases()
    cases_hash = hashlib.sha256((FLOW / "cases.jsonl").read_bytes()).hexdigest()

    for trial_index in range(trial_count):
        trial_name = f"trial-{trial_index + 1:02d}"
        trial_dir = runs_dir / trial_name
        relative = trial_dir.relative_to(FLOW).as_posix()
        child_variant = f"{args.variant}-{trial_name}"
        print(f"[{trial_index + 1}/{trial_count}] {trial_name}: launching a fresh Python process", flush=True)
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--variant",
                child_variant,
                "--reps",
                str(args.reps),
                "--output-dir",
                relative,
            ],
            cwd=RUNNER.parents[2],
            check=False,
        )
        if completed.returncode:
            raise SystemExit(completed.returncode)

    if aggregate_results.exists() or aggregate_summary.exists():
        repeat_summary = {}
        if aggregate_summary.exists():
            repeat_summary = json.loads(aggregate_summary.read_text(encoding="utf-8"))
        same_config = (
            repeat_summary.get("trials") == trial_count
            and repeat_summary.get("reps_per_trial") == args.reps
            and repeat_summary.get("cases_hash") == cases_hash
        )
        if not same_config:
            if not args.archive_existing:
                parser.error(
                    "aggregate outputs already exist; pass --archive-existing to preserve them before replacement"
                )
            archive = runs_dir / "pre-repeat"
            archive.mkdir(parents=True, exist_ok=True)
            for name in ("results.jsonl", "summary.json", "errors.jsonl"):
                source = variant_dir / name
                destination = archive / name
                if source.exists() and not destination.exists():
                    shutil.copy2(source, destination)
            traces = variant_dir / "traces"
            archived_traces = archive / "traces"
            if traces.exists() and not archived_traces.exists():
                shutil.copytree(traces, archived_traces)

    all_rows: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []
    per_trial: list[dict[str, Any]] = []
    trial_environment: dict[str, Any] = {}
    root_traces = variant_dir / "traces"
    root_traces.mkdir(parents=True, exist_ok=True)
    expected_trial_keys = {(case["id"], rep) for case in cases for rep in range(args.reps)}

    for trial_index in range(trial_count):
        trial_name = f"trial-{trial_index + 1:02d}"
        trial_dir = runs_dir / trial_name
        trial_rows = read_jsonl(trial_dir / "results.jsonl")
        trial_summary = json.loads((trial_dir / "summary.json").read_text(encoding="utf-8"))
        if trial_index == 0:
            trial_environment = trial_summary.get("environment", {})
        trial_keys = {(row["prompt_id"], row["rep"]) for row in trial_rows}
        if trial_keys != expected_trial_keys:
            raise RuntimeError(f"{trial_name} incomplete: {len(expected_trial_keys - trial_keys)} rows missing")
        trial_ms = [row["grade"]["latency_ms"] for row in trial_rows]
        trial_correct = sum(row["correct"] for row in trial_rows)
        per_trial.append(
            {
                "trial": trial_index + 1,
                "rows": len(trial_rows),
                "correct": trial_correct,
                "correct_rate": trial_correct / len(trial_rows),
                "p50_ms": percentile(trial_ms, 50),
                "p95_ms": percentile(trial_ms, 95),
                "p99_ms": percentile(trial_ms, 99),
            }
        )
        for row in trial_rows:
            local_rep = row["rep"]
            global_rep = trial_index * args.reps + local_rep
            row["rep"] = global_rep
            row.setdefault("meta", {}).update({"trial": trial_index + 1, "trial_rep": local_rep})
            source_trace = trial_dir / "traces" / f"{row['prompt_id']}_rep{local_rep}.json"
            target_trace = root_traces / f"{row['prompt_id']}_rep{global_rep}.json"
            if not source_trace.exists():
                raise RuntimeError(f"missing trace {source_trace.relative_to(FLOW)}")
            shutil.copy2(source_trace, target_trace)
            all_rows.append(row)
        for error in read_jsonl(trial_dir / "errors.jsonl"):
            error["rep"] = trial_index * args.reps + error["rep"]
            error["trial"] = trial_index + 1
            all_errors.append(error)

    expected_total = len(cases) * args.reps * trial_count
    if len(all_rows) != expected_total:
        raise RuntimeError(f"aggregated {len(all_rows)} rows, expected {expected_total}")
    temp_results = variant_dir / ".results.jsonl.tmp"
    temp_results.parent.mkdir(parents=True, exist_ok=True)
    temp_results.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in all_rows), encoding="utf-8"
    )
    temp_results.replace(aggregate_results)
    if all_errors:
        (variant_dir / "errors.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in all_errors), encoding="utf-8"
        )

    all_ms = [row["grade"]["latency_ms"] for row in all_rows]
    run_p50s = [item["p50_ms"] for item in per_trial]
    case_summary: dict[str, Any] = {}
    for case in cases:
        rows = [row for row in all_rows if row["prompt_id"] == case["id"]]
        values = [row["grade"]["latency_ms"] for row in rows]
        correct = sum(row["correct"] for row in rows)
        case_summary[case["id"]] = {
            "reps": len(values),
            "correct": correct,
            "correct_rate": correct / len(values),
            "mean_ms": sum(values) / len(values),
            "p50_ms": percentile(values, 50),
            "p95_ms": percentile(values, 95),
            "p99_ms": percentile(values, 99),
        }
    correct_total = sum(row["correct"] for row in all_rows)
    median_trial_p50 = percentile(run_p50s, 50)
    summary = {
        "description": "Warm repeated-query recall latency; independent-process median is the decision metric",
        "target": "code",
        "cases": len(cases),
        "reps": args.reps * trial_count,
        "reps_per_trial": args.reps,
        "trials": trial_count,
        "rows": len(all_rows),
        "correct": correct_total,
        "correct_rate": correct_total / len(all_rows),
        "median_trial_p50_ms": median_trial_p50,
        "latency_ms": {
            "pooled_p50": percentile(all_ms, 50),
            "pooled_p95": percentile(all_ms, 95),
            "pooled_p99": percentile(all_ms, 99),
            "median_trial_p50": percentile(run_p50s, 50),
            "mean_trial_p50": sum(run_p50s) / len(run_p50s),
            "min_trial_p50": min(run_p50s),
            "max_trial_p50": max(run_p50s),
            "trial_p50_values": run_p50s,
        },
        "by_trial": per_trial,
        "by_case": case_summary,
        "cases_hash": cases_hash,
        "environment": {
            **trial_environment,
            "trials": trial_count,
            "reps_per_trial": args.reps,
            "runner_sha256": hashlib.sha256(RUNNER.read_bytes()).hexdigest(),
            "seed": 42,
            "corpus_rows": CORPUS_SIZE,
            "retries": 0,
        },
    }
    write_json(aggregate_summary, summary)
    print(
        f"{args.variant}: {len(all_rows)}/{expected_total} rows, correct "
        f"{correct_total}/{len(all_rows)}, median run p50 "
        f"{median_trial_p50:.6f} ms, "
        f"run p50 range {min(run_p50s):.6f}..{max(run_p50s):.6f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
