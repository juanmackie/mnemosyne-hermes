#!/usr/bin/env python3
"""Run two source trees in time-blocked, counterbalanced process pairs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from runner import FLOW, ROOT, percentile, read_cases, write_json

RUNNER = FLOW / "runner.py"


def p50_for(output_dir: Path, expected_rows: int) -> float:
    rows = [
        json.loads(line)
        for line in (output_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != expected_rows or any(row["correct"] != 1 for row in rows):
        raise RuntimeError(
            f"{output_dir.relative_to(FLOW)} must contain {expected_rows} passing rows"
        )
    return percentile([row["grade"]["latency_ms"] for row in rows], 50)


def source_hash(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((source / "mnemosyne_lite").rglob("*.py")):
        digest.update(path.relative_to(source).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-source", default="baseline/source", help="directory containing mnemosyne_lite/")
    parser.add_argument("--right-source", default="src", help="directory containing mnemosyne_lite/")
    parser.add_argument("--left-label", default="baseline")
    parser.add_argument("--right-label", default="candidate")
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--reps", type=int, default=60, help="calls per case per process")
    parser.add_argument("--run-id", default="calibration", help="directory name under baseline/evidence/paired/")
    args = parser.parse_args()
    if args.pairs < 2 or args.reps < 1:
        parser.error("--pairs must be >= 2 and --reps must be >= 1")
    if not args.run_id.replace("_", "").replace("-", "").isalnum():
        parser.error("run-id may contain only letters, digits, hyphens, and underscores")

    def source_path(raw: str) -> Path:
        path = Path(raw)
        resolved = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        if not (resolved / "mnemosyne_lite" / "storage.py").is_file():
            parser.error(f"source directory must contain mnemosyne_lite/storage.py: {resolved}")
        return resolved

    left_source = source_path(args.left_source)
    right_source = source_path(args.right_source)
    left_hash = source_hash(left_source)
    right_hash = source_hash(right_source)
    run_root = FLOW / "baseline" / "evidence" / "paired" / args.run_id
    raw_root = run_root / "raw"
    run_root.mkdir(parents=True, exist_ok=True)
    case_count = len(read_cases())
    pairs: list[dict[str, Any]] = []

    for pair_index in range(args.pairs):
        pair_name = f"pair-{pair_index + 1:02d}"
        pair_dir = raw_root / pair_name
        # Alternate order to keep clock/load drift from consistently favoring one side.
        order = ["left", "right"] if pair_index % 2 == 0 else ["right", "left"]
        measured: dict[str, float] = {}
        for side in order:
            label = args.left_label if side == "left" else args.right_label
            source = left_source if side == "left" else right_source
            output_dir = pair_dir / side
            relative_out = output_dir.relative_to(FLOW).as_posix()
            print(
                f"[{pair_index + 1}/{args.pairs}] {pair_name}: {label} ({side})",
                flush=True,
            )
            env = os.environ.copy()
            env["MNEMOSYNE_EVAL_SOURCE"] = str(source)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--variant",
                    f"paired-{side}-{pair_name}",
                    "--reps",
                    str(args.reps),
                    "--output-dir",
                    relative_out,
                ],
                cwd=ROOT,
                env=env,
                check=False,
            )
            if completed.returncode:
                raise SystemExit(completed.returncode)
            measured[side] = p50_for(output_dir, case_count * args.reps)
        left_p50 = measured["left"]
        right_p50 = measured["right"]
        pairs.append(
            {
                "pair": pair_index + 1,
                "order": order,
                "left_p50_ms": left_p50,
                "right_p50_ms": right_p50,
                "right_minus_left_ms": right_p50 - left_p50,
                "relative_delta_pct": (right_p50 / left_p50 - 1) * 100,
            }
        )

    deltas = [pair["right_minus_left_ms"] for pair in pairs]
    relative = [pair["relative_delta_pct"] for pair in pairs]
    sample_sd = statistics.stdev(deltas)
    # Two-sided 95% t critical values, indexed by number of paired runs.
    t_critical = {
        2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447,
        8: 2.365, 9: 2.306, 10: 2.262, 11: 2.228, 12: 2.201,
        13: 2.179, 14: 2.160, 15: 2.145, 16: 2.131, 17: 2.120,
        18: 2.110, 19: 2.101, 20: 2.093, 21: 2.086, 22: 2.080,
        23: 2.074, 24: 2.069, 25: 2.064, 26: 2.060, 27: 2.056,
        28: 2.052, 29: 2.048, 30: 2.045,
    }.get(args.pairs)
    ci_half_width = (
        t_critical * sample_sd / (args.pairs**0.5) if t_critical is not None else None
    )
    result = {
        "description": "Time-blocked paired-process latency comparison",
        "left_label": args.left_label,
        "right_label": args.right_label,
        "left_source": str(left_source),
        "right_source": str(right_source),
        "left_source_sha256": left_hash,
        "right_source_sha256": right_hash,
        "source_equal": left_hash == right_hash,
        "cases": case_count,
        "reps_per_case_per_process": args.reps,
        "pairs": pairs,
        "relative_delta_pct": {
            "median": percentile(relative, 50),
            "min": min(relative),
            "max": max(relative),
        },
        "absolute_delta_ms": {
            "mean": statistics.mean(deltas),
            "sample_sd": sample_sd,
            "paired_95pct_t_ci": [
                statistics.mean(deltas) - ci_half_width,
                statistics.mean(deltas) + ci_half_width,
            ]
            if ci_half_width is not None
            else None,
        },
    }
    write_json(run_root / "summary.json", result)
    print(
        f"paired p50 delta median {result['relative_delta_pct']['median']:+.2f}% "
        f"(range {result['relative_delta_pct']['min']:+.2f}%.."
        f"{result['relative_delta_pct']['max']:+.2f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
