#!/usr/bin/env python3
"""Repo gate: the memory-quality scorecard must still measure what it claims.

This is the cheap half of the scorecard. It runs on a small deterministic corpus
so it belongs in CI, and it checks the *instrument*, not the product's quality:

1. the generator is deterministic and its labels are exact (answerable probes
   have a real answer, fabricated/expired probes have none);
2. the held-out split is disjoint from dev;
3. A/A — two arms of the same configuration — produces zero paired difference;
4. reverting the 0.15 admission-floor engine patch is detected;
5. staged A/A, deletion, sleep, and degradation controls run on scratch stores.

A quality regression in the product does not fail this gate; a broken instrument
does. Product quality is reported by `bench/scorecard/`, weekly in the drift
workflow, and in the record under `bench/scorecard/RESULTS.md`.

Usage:
    python bench/scorecard/gate.py            # defaults: 240 probes, 4 workers
    python bench/scorecard/gate.py --probes 600
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        failures.append(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # 240 held-out probes is ~55 fabricated probes, comfortably above the
    # ~40 the revert detection needs at 80% power (measured on the full
    # 12,559-probe corpus: at n=40 the paired CI is [+0.18, +0.48]).
    parser.add_argument("--probes", type=int, default=240, help="held-out probe count")
    parser.add_argument("--dev-probes", type=int, default=48)
    parser.add_argument("--probes-per-life", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resamples", type=int, default=2000)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from bench.scorecard import parallel
    from bench.scorecard.aggregate import compare_surfaces
    from bench.scorecard.cli import engine_state
    from bench.scorecard.corpus import corpus_digest
    from bench.scorecard.retention import (
        _job,
        aa_control,
        assign_cohorts,
        compare_control,
        stage_events,
        summary_loss_control,
    )
    from bench.scorecard.runner import PROVIDER_ROOT, activate_engine_path, engine_attestation
    from bench.sim.scale import make_corpus

    failures: list[str] = []
    started = time.perf_counter()

    print("== corpus ==")
    corpus = make_corpus(
        seed=7,
        probes=args.probes,
        dev_probes=args.dev_probes,
        probes_per_life=args.probes_per_life,
        slots=4,
    )
    again = make_corpus(
        seed=7,
        probes=args.probes,
        dev_probes=args.dev_probes,
        probes_per_life=args.probes_per_life,
        slots=4,
    )
    _check(
        corpus_digest(corpus) == corpus_digest(again),
        "the generator is deterministic for a fixed seed",
        failures,
    )
    _check(
        corpus["counts"]["held_out"] == args.probes,
        f"the held-out split has exactly {args.probes} probes",
        failures,
    )
    held = {probe["probe_id"] for probe in corpus["probes"] if probe["split"] == "held_out"}
    dev = {probe["probe_id"] for probe in corpus["probes"] if probe["split"] == "dev"}
    _check(not (held & dev), "held-out and dev splits are disjoint", failures)
    kinds = {probe["kind"] for probe in corpus["probes"]}
    _check(
        {"answerable_current", "fabricated", "expired"} <= kinds,
        "the corpus covers answerable, fabricated and expired probes",
        failures,
    )
    label_errors = []
    for probe in corpus["probes"]:
        if probe["kind"] in ("fabricated", "expired"):
            if probe["answers"]:
                label_errors.append(probe["probe_id"])
        elif not probe["answers"]:
            label_errors.append(probe["probe_id"])
    _check(not label_errors, "every probe's answer set matches its kind", failures)

    probes = [probe for probe in corpus["probes"] if probe["split"] == "held_out"]
    probes_by_life: dict[str, list[dict]] = {}
    for probe in probes:
        probes_by_life.setdefault(probe["life_id"], []).append(probe)
    lives = [life for life in corpus["lives"] if life["life_id"] in probes_by_life]

    def measure(arm: str, patch: str) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix=f"scorecard-gate-{arm}-") as folder:
            state = engine_state(patch, Path(folder))
            activate_engine_path(state["sys_path"])
            results = parallel.run_lives(
                lives,
                probes_by_life,
                arm=arm,
                dump_candidates=False,
                engine_sys_path=state["sys_path"],
                workers=args.workers,
            )
        merged = parallel.merge(results, arm=arm, wall_seconds=0.0)
        if merged["errors"]:
            raise RuntimeError(
                f"{arm} produced {len(merged['errors'])} errors: {merged['errors'][0]}"
            )
        return merged["records"]

    import mnemosyne

    installed_path = str(Path(mnemosyne.__file__).resolve().parent.parent)
    print("== arms ==")
    t0 = time.perf_counter()
    arm_a = measure("gate-a", "installed")
    arm_a2 = measure("gate-a2", "installed")
    print(f"  A/A                       {time.perf_counter() - t0:6.1f}s")
    t0 = time.perf_counter()
    arm_revert = measure("gate-floor-revert", "revert-floor")
    print(f"  floor revert              {time.perf_counter() - t0:6.1f}s")

    print("== A/A control ==")
    aa = compare_surfaces(probes, arm_a, arm_a2, "tool", resamples=args.resamples)
    for name in (
        "fabricated_entity_probes_returning_rows",
        "expired_versions_served",
        "answerable_recall",
    ):
        block = aa[name]
        low, high = block["difference"]["ci_low"], block["difference"]["ci_high"]
        _check(
            low <= 0.0 <= high,
            f"A/A {name}: CI [{low:.5f}, {high:.5f}] spans zero",
            failures,
        )

    print("== detection (admission-floor revert) ==")
    revert = compare_surfaces(probes, arm_a, arm_revert, "tool", resamples=args.resamples)
    detection_targets = {
        "fabricated_entity_probes_returning_rows": revert[
            "fabricated_entity_probes_returning_rows"
        ],
        "fabricated_rows_returned": revert["fabricated_rows_returned"],
        "answerable_recall": revert["answerable_recall"],
    }
    detected = [
        name for name, block in detection_targets.items() if block["difference"]["excludes_zero"]
    ]
    detail = ", ".join(
        f"{name} d={block['difference']['mean']:+.5f} "
        f"[{block['difference']['ci_low']:+.5f}, {block['difference']['ci_high']:+.5f}]"
        for name, block in detection_targets.items()
    )
    _check(
        bool(detected),
        f"reverting the 0.15 admission floor is detected ({detail})",
        failures,
    )

    print("== staged retention controls ==")
    activate_engine_path(installed_path)
    sys.path.insert(0, str(PROVIDER_ROOT))
    control_lives = [
        life
        for life in lives
        if any(
            cohort < 3
            for cohort in assign_cohorts(
                stage_events(life["events"]), probes_by_life[life["life_id"]]
            ).values()
        )
    ][:2]
    _check(len(control_lives) == 2, "two lives have an early measurable cohort", failures)
    import multiprocessing

    attestation = engine_attestation()
    _check(
        attestation.get("all_patched") is True
        and attestation.get("version")
        == attestation.get("manifest_match_expected", {}).get("version"),
        "staged controls use the manifest-attested engine",
        failures,
    )
    staged: dict[str, list[dict]] = {}
    results_by_arm = {}
    with multiprocessing.get_context("spawn").Pool(processes=min(args.workers or 2, 2)) as pool:
        for arm in (
            "no-sleep",
            "aa",
            "delete-originals",
            "sleep+degrade",
            "sleep-delete-originals",
            "sleep-loss-control",
        ):
            treatment = "no-sleep" if arm == "aa" else arm
            jobs = [(life, probes_by_life[life["life_id"]], treatment) for life in control_lives]
            results_by_arm[arm] = pool.map(_job, jobs)
            staged[arm] = [record for run in results_by_arm[arm] for record in run["records"]]
    tier_counts = [
        sum(
            operation["degradation"][tier]["eligible"]
            for run in results_by_arm["sleep+degrade"]
            for operation in run["operations"]
            if "degradation" in operation
        )
        for tier in (0, 1)
    ]
    _check(
        all(tier_counts),
        f"staged sleep and degradation exercised both transitions: {tier_counts}",
        failures,
    )
    retention_controls = {}
    for surface in ("tool", "prefetch"):
        aa_control_result = aa_control(staged["no-sleep"], staged["aa"], surface)
        deletion = compare_control(
            staged["no-sleep"], staged["delete-originals"], surface, resamples=args.resamples
        )
        deletion_detected = deletion["n"] > 0 and deletion["ci_high"] < 0
        sleep_loss = summary_loss_control(staged, surface, resamples=args.resamples)
        sleep_loss_detected = sleep_loss["n"] > 0 and sleep_loss["ci_high"] < 0
        _check(sleep_loss_detected, f"staged summary loss {surface}: {sleep_loss}", failures)
        _check(aa_control_result["passed"], f"staged A/A {surface}: {aa_control_result}", failures)
        _check(deletion_detected, f"staged deletion {surface}: {deletion}", failures)
        retention_controls[surface] = {
            "aa": aa_control_result,
            "deletion": deletion,
            "sleep_loss": sleep_loss,
        }

    elapsed = time.perf_counter() - started
    report = {
        "format": "mnemosyne-scorecard-gate/v1",
        "corpus_digest": corpus_digest(corpus),
        "probes": args.probes,
        "workers": args.workers,
        "resamples": args.resamples,
        "wall_seconds": round(elapsed, 2),
        "aa": aa,
        "floor_revert": revert,
        "retention_controls": retention_controls,
        "detected_metrics": detected,
        "failures": failures,
        "ok": not failures,
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, sort_keys=True, indent=1), encoding="utf-8")
    print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + str(len(failures)) + ' check(s)'}")
    print(f"wall {elapsed:.1f}s")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
