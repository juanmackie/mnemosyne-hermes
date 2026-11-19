"""Parallel execution of scorecard arms over independent synthetic lives.

Each life is a complete, isolated store: its own database, its own provider
instance, its own sessions. That makes the corpus embarrassingly parallel, and
parallelism is the only reason the full 12,559-probe sweep is affordable — the
work per life is dominated by ingestion, not by recall.

Parallelism must not change what is measured: every worker runs the same
``runner.LifeStore`` path with the same environment scrubbing, and results are
merged in life order so a report is byte-identical regardless of worker count.
The reference-behaviour tests prove that (see ``tests/test_scorecard.py``).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from . import runner as runner_module
from .grade import grade, grade_prefetch, grade_provenance, prefetch_entries


def _measure_life(job: dict[str, Any]) -> dict[str, Any]:
    """Measure one life in this process. Returns records, dumps and errors."""
    life = job["life"]
    probes = job["probes"]
    arm = job["arm"]
    ingest = job["ingest"]
    limit = job["limit"]
    dump_candidates = job["dump_candidates"]
    dump_every = job["dump_every"]
    environment = job.get("environment") or None
    engine_sys_path = job.get("engine_sys_path")
    runner_module.activate_engine_path(engine_sys_path)

    records: list[dict[str, Any]] = []
    dumps: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    secrets: dict[str, Any] = {"planted": len(life.get("secrets") or []), "stored": 0}

    # A store that is closed can still leave a handle that Windows will not let
    # us delete (sqlite/vec teardown races); a leftover temp dir must not turn a
    # measured life into an instrument error.
    with tempfile.TemporaryDirectory(
        prefix=f"mnemosyne-scorecard-{arm}-", ignore_cleanup_errors=True
    ) as folder:
        scratch = Path(folder)
        resolved = runner_module.clean_environment(scratch, environment)
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(resolved)
        store = None
        try:
            store = runner_module.LifeStore.create(scratch, life["life_id"])
            if ingest == "conversation":
                for session in life["sessions"]:
                    for message in session["messages"]:
                        store.ingest_turn(message)
            else:
                for event in life["events"]:
                    if event["disposition"] != "store":
                        continue
                    store.ingest_tool(
                        event["sentence"],
                        valid_until=(
                            event["valid_to"] if event.get("valid_to_stated_in_text") else None
                        ),
                    )
            # Carried lessons: written under model A, measured under model B.
            lessons = life.get("lessons") or []
            for lesson in lessons:
                os.environ["MNEMOSYNE_MODEL_ID"] = str(lesson["model_a"])
                store.ingest_lesson(lesson["text"])
            if lessons:
                os.environ["MNEMOSYNE_MODEL_ID"] = str(lessons[0]["model_b"])

            if life.get("secrets"):
                texts = runner_module.store_texts(Path(store.provider._beam.db_path))
                audit = runner_module.secrets_in_texts({"lives": [life]}, texts)
                secrets["stored"] = audit["stored"]
                secrets["stored_event_ids"] = audit["stored_event_ids"]

            for probe_index, probe in enumerate(probes):
                try:
                    payload = store.recall(probe["query"], limit=limit)
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    results = payload.get("results") or []
                    texts = [str(row.get("content") or "") for row in results]
                    tool_grade = grade(probe, texts)
                    tool_grade["surface"] = "tool"
                    if probe["kind"] == "carried_lesson":
                        tool_grade.update(
                            grade_provenance(
                                probe,
                                [
                                    (text, str(row.get("learned_under") or ""))
                                    for text, row in zip(texts, results, strict=True)
                                ],
                            )
                        )
                    records.append(tool_grade)

                    context = store.prefetch(probe["query"])
                    prefetch_grade = grade_prefetch(probe, context)
                    prefetch_grade["surface"] = "prefetch"
                    if probe["kind"] == "carried_lesson":
                        prefetch_grade.update(grade_provenance(probe, prefetch_entries(context)))
                    records.append(prefetch_grade)

                    want_dump = dump_candidates and (
                        probe_index % max(dump_every, 1) == 0 or probe["kind"] == "fabricated"
                    )
                    if want_dump:
                        explained = store.recall_explain(probe["query"], limit=limit)
                        dumps.extend(
                            {"arm": arm, **row}
                            for row in runner_module.candidate_dump(probe, explained, texts)
                        )
                except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                    errors.append(
                        {
                            "arm": arm,
                            "probe_id": probe["probe_id"],
                            "life_id": life["life_id"],
                            "error": f"{type(error).__name__}: {error}",
                            "traceback": traceback.format_exc(limit=4),
                        }
                    )
        except Exception as error:  # noqa: BLE001 - a life that cannot be built is data
            errors.append(
                {
                    "arm": arm,
                    "life_id": life["life_id"],
                    "error": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc(limit=4),
                }
            )
        finally:
            if store is not None:
                store.close()
            os.environ.clear()
            os.environ.update(previous)
    return {
        "life_id": life["life_id"],
        "records": records,
        "candidate_dumps": dumps,
        "errors": errors,
        "secrets": secrets,
    }


def _jobs_for_life(
    arm: str,
    life: dict[str, Any],
    probes: list[dict[str, Any]],
    *,
    ingest: str,
    limit: int,
    dump_candidates: bool,
    dump_every: int,
    environment: dict[str, str] | None,
    engine_sys_path: str | None,
) -> dict[str, Any]:
    return {
        "arm": arm,
        "life": life,
        "probes": probes,
        "ingest": ingest,
        "limit": limit,
        "dump_candidates": dump_candidates,
        "dump_every": dump_every,
        "environment": environment,
        "engine_sys_path": engine_sys_path,
    }


def run_lives(
    lives: list[dict[str, Any]],
    probes_by_life: dict[str, list[dict[str, Any]]],
    *,
    arm: str,
    ingest: str = "conversation",
    limit: int = 10,
    dump_candidates: bool = True,
    dump_every: int = 40,
    environment: dict[str, str] | None = None,
    engine_sys_path: str | None = None,
    workers: int = 0,
    progress: bool = False,
    completed: dict[str, dict[str, Any]] | None = None,
    on_life_done: Any = None,
) -> list[dict[str, Any]]:
    """Measure every life, in parallel when `workers` > 0 (0 = auto).

    `completed` holds lives already measured (from a checkpoint directory); they
    are returned as-is so a resumed run reproduces the same report. Every life
    that finishes is passed to `on_life_done(life_id, result)` immediately, which
    is what makes an hours-long arm crash-safe.
    """
    already = completed or {}
    jobs = [
        _jobs_for_life(
            arm,
            life,
            probes_by_life.get(life["life_id"], []),
            ingest=ingest,
            limit=limit,
            dump_candidates=dump_candidates,
            dump_every=dump_every,
            environment=environment,
            engine_sys_path=engine_sys_path,
        )
        for life in lives
        if probes_by_life.get(life["life_id"])
    ]
    if not jobs:
        return []

    def record(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if on_life_done is not None:
            on_life_done(result["life_id"], result)
        return result

    resolved_workers = workers if workers and workers > 0 else min(len(jobs), _default_workers())
    results = [already[job["life"]["life_id"]] for job in jobs if job["life"]["life_id"] in already]
    remaining = [job for job in jobs if job["life"]["life_id"] not in already]
    if progress and results:
        print(
            f"  [scorecard] resuming: {len(results)}/{len(jobs)} lives already measured",
            file=sys.stderr,
            flush=True,
        )
    if not remaining:
        return results
    if resolved_workers <= 1:
        for job in remaining:
            results.append(record(job, _measure_life(job)))
            if progress:
                _report_progress(len(results), len(jobs), results)
        return _ordered(results, lives)

    # "spawn" is the portable choice: the engine's module-level sqlite
    # thread-locals and any already-loaded embedder must not be inherited.
    context = mp.get_context("spawn")
    with context.Pool(processes=resolved_workers) as pool:
        for result in pool.imap_unordered(_measure_life, remaining, chunksize=1):
            results.append(record({"life": {"life_id": result["life_id"]}}, result))
            if progress and len(results) % 10 == 0:
                _report_progress(len(results), len(jobs), results)
    return _ordered(results, lives)


def _ordered(results: list[dict[str, Any]], lives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Life-order the results so a report never depends on completion order."""
    order = {life["life_id"]: index for index, life in enumerate(lives)}
    return sorted(results, key=lambda row: order.get(row.get("life_id"), len(order)))


def _report_progress(done: int, total: int, results: list[dict[str, Any]]) -> None:
    probes = sum(len({r["probe_id"] for r in row["records"]}) // 2 for row in results)
    errors = sum(len(row["errors"]) for row in results)
    print(
        f"  [scorecard] {done}/{total} lives, {probes} probes, {errors} errors",
        file=sys.stderr,
        flush=True,
    )


def _default_workers() -> int:
    try:
        cpus = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        cpus = os.cpu_count() or 4
    # Leave headroom: onnxruntime already threads inside each worker.
    return max(1, min(cpus - 2, 12))


def merge(results: list[dict[str, Any]], *, arm: str, wall_seconds: float) -> dict[str, Any]:
    """Fold per-life results into the arm report shape used by the reporter."""
    records: list[dict[str, Any]] = []
    dumps: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    planted = 0
    stored = 0
    for row in results:
        records.extend(row["records"])
        dumps.extend(row["candidate_dumps"])
        errors.extend(row["errors"])
        planted += row["secrets"].get("planted", 0)
        stored += row["secrets"].get("stored", 0)
    tool_rows = [row for row in records if row.get("surface") == "tool"]
    return {
        "arm": arm,
        "probes_measured": len({row["probe_id"] for row in tool_rows}),
        "records": records,
        "candidate_dumps": dumps,
        "errors": errors,
        "wall_seconds": round(wall_seconds, 3),
        "lives_measured": len(results),
        "secret_audit": {"planted": planted, "stored": stored},
    }
