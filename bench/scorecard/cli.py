"""Scorecard CLI: measure arms, compare them, and report real numbers.

Usage (from the repository root, with the pinned patched engine importable):

    python -m bench.scorecard.cli corpus --output /tmp/corpus.json
    python -m bench.scorecard.cli run --arm a-a --corpus /tmp/corpus.json --output /tmp/aa.json
    python -m bench.scorecard.cli run --arm floor-revert --engine-patch revert-floor ...
    python -m bench.scorecard.cli compare --baseline /tmp/aa.json --candidate /tmp/b.json

An "arm" is one measurement of the corpus. Two arms over the same corpus are
paired probe-for-probe, which is what makes the comparison sharp.

The two built-in interventions are expressed as *engine patch states*, because
both targets live in the pinned engine's recall path and because reverting a
patch is exactly what the instrument has to be able to detect:

``--engine-patch revert-floor``
    Restores the published 3.15.1 stepped admission gate
    (``_minimum_recall_relevance``: 0.5 for three query tokens, 0.3 for four or
    more) in place of the audited ``0.15`` floor. The A/A arms must show no
    difference; this arm must.

The patch state is applied to the installed engine inside a private temporary
tree that is put *ahead* of site-packages on ``sys.path`` (see ``engine_state``),
so the installed engine is never written and two arms can never see each
other's state.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# The published 3.15.1 text of the audited function, restored verbatim.
_ORIGINAL_MINIMUM_RECALL_RELEVANCE = '''def _minimum_recall_relevance(query_tokens: List[str]) -> float:
    """Raise the lexical gate for broad natural-language queries.

    One matching real word is enough for short lookup-style queries, but not
    for broad nonsense strings like "purple bicycle quantum oatmeal".
    """
    if len(query_tokens) >= 4:
        return 0.3
    if len(query_tokens) == 3:
        return 0.5
    return 0.15
'''


def _engine_package() -> Path:
    import mnemosyne

    return Path(mnemosyne.__file__).resolve().parent


def engine_state(kind: str, scratch: Path) -> dict[str, Any]:
    """Materialize an engine patch state and return the sys.path entry to use.

    `kind` is ``installed`` (whatever is in site-packages) or one of the
    reversible states. Reversible states copy the *whole engine package* into
    `scratch` and edit the copy, so the installed tree stays byte-identical and
    ``scripts/apply_engine_patches.py`` keeps working afterwards.
    """
    package = _engine_package()
    if kind == "installed":
        return {"kind": kind, "sys_path": str(package.parent), "files": {}, "write_free": True}
    if kind != "revert-floor":
        raise ValueError(f"unknown engine state: {kind}")
    target_root = scratch / f"engine-{kind}"
    if target_root.exists():
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True)
    sandbox = target_root / "mnemosyne"
    shutil.copytree(package, sandbox, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    beam = sandbox / "core" / "beam.py"
    source = beam.read_text(encoding="utf-8")
    start = source.find("def _minimum_recall_relevance(")
    if start == -1:
        raise ValueError("the installed engine has no _minimum_recall_relevance")
    # Find the end of the function: the first following line that starts a new
    # module-level statement (column 0, non-blank, not a decorator). Scanning for
    # "def "/"class " alone is not enough — the audited engine has module-level
    # constants right after this function, and eating them breaks the module.
    tail = source[start:]
    lines = tail.splitlines(keepends=True)
    end_offset = len(lines[0])
    for line in lines[1:]:
        stripped = line.rstrip("\n")
        if stripped and not stripped[0].isspace():
            break
        end_offset += len(line)
    current = source[start : start + end_offset]
    if current.strip() == _ORIGINAL_MINIMUM_RECALL_RELEVANCE.strip():
        raise ValueError("the installed engine already carries the reverted floor")
    beam.write_text(
        source[:start] + _ORIGINAL_MINIMUM_RECALL_RELEVANCE + source[start + end_offset :],
        encoding="utf-8",
    )
    return {
        "kind": kind,
        "sys_path": str(target_root),
        "files": {"mnemosyne/core/beam.py": _digest(beam)},
        "write_free": False,
    }


def _digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _life_dir(args: argparse.Namespace) -> Path:
    """Checkpoint directory keyed by *what was measured*, not by output name.

    Keying on the output filename would punish the common case — rerunning the
    same arm to a differently-named file — by throwing away hours of work. The
    arm name, corpus digest, split, ingest path and limit are the identity of a
    measurement; the file it gets summarized into is not.
    """
    import hashlib as _hashlib

    # The arm's environment is part of what was measured: two runs of --arm fixed
    # with different flags are different measurements, and reusing one for the
    # other silently reports the flagless numbers under the flag name.
    identity = (
        f"{args.arm}|{_corpus_digest_for(args)}|{args.split}|{args.ingest}|{args.limit}"
        f"|{_environment_tag(args)}"
    )
    tag = _hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    safe_arm = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in args.arm)
    return args.output.parent / "lives" / f"{safe_arm}-{tag}"


def _load_life_checkpoints(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Read per-life results already on disk, keyed by life id.

    A full arm is hours of CPU. Losing it to one bad life (or a killed shell) is
    not acceptable, so each life's raw result is written as soon as it completes
    and a re-run resumes from what is already there. The key includes the arm and
    the corpus digest, so a stale checkpoint from a different corpus is ignored
    instead of silently mixed in.
    """
    import json as _json

    directory = _life_dir(args)
    if not directory.is_dir():
        return {}
    key = (
        f"{args.arm}:{_corpus_digest_for(args)}:{args.split}:{args.ingest}:"
        f"{args.limit}:{_environment_tag(args)}"
    )
    loaded: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("key") != key:
            continue
        loaded[payload["life_id"]] = payload["result"]
    return loaded


def _environment_tag(args: argparse.Namespace) -> str:
    items = sorted(getattr(args, "env", []) or [])
    return ",".join(items)


def _corpus_digest_for(args: argparse.Namespace) -> str:
    from .corpus import corpus_digest

    return corpus_digest(load_corpus(args.corpus))


def _write_life_checkpoint(args: argparse.Namespace, life_id: str, result: dict[str, Any]) -> None:
    import json as _json

    directory = _life_dir(args)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": (
            f"{args.arm}:{_corpus_digest_for(args)}:{args.split}:{args.ingest}:"
            f"{args.limit}:{_environment_tag(args)}"
        ),
        "life_id": life_id,
        "result": result,
    }
    target = directory / f"{life_id}.json"
    staging = target.with_suffix(".json.tmp")
    staging.write_text(_json.dumps(payload), encoding="utf-8")
    staging.replace(target)


def load_corpus(path: Path) -> dict[str, Any]:
    from .corpus import load_or_build

    return load_or_build(path)


def cmd_corpus(args: argparse.Namespace) -> int:
    from .corpus import build_corpus

    corpus = build_corpus(
        seed=args.seed,
        probes=args.probes,
        dev_probes=args.dev_probes,
        probes_per_life=args.probes_per_life,
        slots=args.slots,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(corpus, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(args.output), **corpus["counts"]}, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from . import parallel, runner
    from .corpus import corpus_digest
    from .report import build_arm_report

    corpus = load_corpus(args.corpus)
    probes = corpus["probes"]
    if args.split != "all":
        probes = [probe for probe in probes if probe["split"] == args.split]
    if args.probe_limit:
        probes = probes[: args.probe_limit]
    probes_by_life: dict[str, list[dict[str, Any]]] = {}
    for probe in probes:
        probes_by_life.setdefault(probe["life_id"], []).append(probe)
    lives = [life for life in corpus["lives"] if life["life_id"] in probes_by_life]

    with tempfile.TemporaryDirectory(prefix="mnemosyne-scorecard-engine-") as folder:
        scratch = Path(folder)
        arm_environment: dict[str, str] = {}
        for item in args.env:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise SystemExit(f"--env expects KEY=VALUE, got {item!r}")
            arm_environment[key] = value
        state = engine_state(args.engine_patch, scratch)
        # Fail loudly rather than silently measure the wrong tree.
        try:
            runner.activate_engine_path(state["sys_path"])
        except RuntimeError as error:
            raise SystemExit(f"engine state {state['kind']}: {error}") from error
        completed = {} if args.no_resume else _load_life_checkpoints(args)
        started = time.perf_counter()
        results = parallel.run_lives(
            lives,
            probes_by_life,
            arm=args.arm,
            ingest=args.ingest,
            limit=args.limit,
            dump_candidates=not args.no_candidate_dumps,
            dump_every=args.dump_every,
            engine_sys_path=state["sys_path"],
            environment=arm_environment,
            workers=args.workers,
            progress=not args.quiet,
            completed=completed,
            on_life_done=(
                None
                if args.no_checkpoints
                else lambda life_id, result: _write_life_checkpoint(args, life_id, result)
            ),
        )
        wall = time.perf_counter() - started
        merged = parallel.merge(results, arm=args.arm, wall_seconds=wall)

    report = build_arm_report(
        merged,
        corpus=corpus,
        probes=probes,
        corpus_digest_value=corpus_digest(corpus),
        arm_name=args.arm,
        engine_state=state,
        ingest=args.ingest,
        limit=args.limit,
        workers=args.workers,
        split=args.split,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=1), encoding="utf-8")
    print(json.dumps(report["headline"], indent=2))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from .aggregate import compare_surfaces
    from .report import comparison_report

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    corpus = load_corpus(args.corpus)
    probes = corpus["probes"]
    if args.split != "all":
        probes = [probe for probe in probes if probe["split"] == args.split]

    blocks = {
        surface: compare_surfaces(
            probes,
            baseline["records"],
            candidate["records"],
            surface,
            resamples=args.resamples,
        )
        for surface in ("tool", "prefetch")
    }
    report = comparison_report(
        baseline=baseline,
        candidate=candidate,
        blocks=blocks,
        split=args.split,
        resamples=args.resamples,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, sort_keys=True, indent=1), encoding="utf-8")
    print(json.dumps(report["headline"], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    corpus_parser = sub.add_parser("corpus", help="generate the paired-probe corpus")
    corpus_parser.add_argument("--seed", type=int, default=20261115)
    corpus_parser.add_argument("--probes", type=int, default=12559)
    corpus_parser.add_argument("--dev-probes", type=int, default=2511)
    corpus_parser.add_argument("--probes-per-life", type=int, default=20)
    corpus_parser.add_argument("--slots", type=int, default=5)
    corpus_parser.add_argument("--output", type=Path, required=True)
    corpus_parser.set_defaults(func=cmd_corpus)

    run_parser = sub.add_parser("run", help="measure one arm")
    run_parser.add_argument("--arm", required=True)
    run_parser.add_argument("--corpus", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--engine-patch", default="installed")
    run_parser.add_argument("--ingest", choices=("conversation", "tool"), default="conversation")
    run_parser.add_argument("--limit", type=int, default=10)
    run_parser.add_argument("--split", choices=("held_out", "dev", "all"), default="held_out")
    run_parser.add_argument("--probe-limit", type=int, default=None)
    run_parser.add_argument("--dump-every", type=int, default=40)
    run_parser.add_argument("--no-candidate-dumps", action="store_true")
    run_parser.add_argument("--workers", type=int, default=0)
    run_parser.add_argument("--quiet", action="store_true")
    run_parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "arm environment override, applied AFTER the measurement environment "
            "is scrubbed. Required for flag-gated arms: a plain shell export is "
            "scrubbed on purpose, so an operator's stray MNEMOSYNE_* setting "
            "cannot change a measurement silently."
        ),
    )
    run_parser.add_argument(
        "--no-checkpoints",
        action="store_true",
        help="do not write per-life results beside the report",
    )
    run_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore existing per-life checkpoints and measure every life again",
    )
    run_parser.set_defaults(func=cmd_run)

    compare_parser = sub.add_parser("compare", help="paired comparison of two arms")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.add_argument("--corpus", type=Path, required=True)
    compare_parser.add_argument("--split", choices=("held_out", "dev", "all"), default="held_out")
    compare_parser.add_argument("--resamples", type=int, default=10_000)
    compare_parser.add_argument("--output", type=Path)
    compare_parser.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    if not os.environ.get("MNEMOSYNE_SCORECARD_PARENT"):
        pass
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
