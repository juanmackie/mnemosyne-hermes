#!/usr/bin/env python3
"""Self-Improvement Memory (SIM) ledger for this repo's improvement loops.

The ledger is one machine-checked JSON file beside the prose logs
(``research/sim.json``, format ``mnemosyne-sim/v1``). It records, per
root-cause hypothesis, the attempts made against it: what changed and why, the
cases *predicted* to be fixed before evaluation, the cases actually fixed,
missed and regressed after it, and the provenance (objective, engine pin, fixture
digest, commit, proposing model) needed to read the result after a model or
benchmark switch.

Five commands, one per phase of the loop (``--ledger PATH`` goes before the
command name; it defaults to ``research/sim.json``):

    python scripts/sim.py open    --name "..." --objective precisionmembench
    python scripts/sim.py predict --problem P05 --cases a,b --what "..." --patch P51
    python scripts/sim.py record  --attempt P05/7 --report research/runs/iter-7.json \
                                  --baseline research/runs/iter-3.json --accept
    python scripts/sim.py render
    python scripts/sim.py validate

``record`` refuses an attempt that has no earlier prediction: the prediction is
written before the run, and the outcome is computed from two per-case reports —
the baseline taken before the attempt and the report taken after it. ``fixed``
means *newly* fixed (failed in the baseline, passes now), so an attempt cannot
credit itself with a case an earlier change already fixed; ``collateral`` is a
case that passed before and fails now. Both reports must name the same fixtures,
and a collision (``fixed`` outside ``predicted``) is a validation error.

Format (``mnemosyne-sim/v1``)

    top level      format, next_id, carried_from, aliases, problems
    problem        id, name, opened{date, objective}, attempts[]
    attempt        round, accepted, backfilled, what, files, patch, predicted,
                   fixed, missed, collateral, passed, total, predicted_at,
                   evaluated_at, provenance
    provenance     objective, fixture_sha256, engine_pin,
                   engine_patches_sha256, hermes_version, git_commit, model

``round`` is unique inside one problem and is the research-iteration number when
the attempt has one. A problem reference is its id, an alias, or its exact
(normalized) name, so ``--problem P05`` and ``--problem "the p51 trade"`` both
work. Identical problem names merge deterministically into the first id.

``backfilled: true`` marks an attempt reconstructed after the fact from the
prose logs; a non-backfilled attempt is refused when ``predicted_at`` is later
than ``evaluated_at``. Backfilled predictions are the best reconstruction of the change's
stated intent, so ``render`` reports their calibration separately from the
predictions that were written before a run.

Exit status: 0 ok, 1 the ledger failed validation or the command was refused,
2 bad usage.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = ROOT / "research" / "sim.json"
FORMAT = "mnemosyne-sim/v1"
PATCHES_MD = ROOT / "integrations" / "hermes-provider" / "PATCHES.md"
PROVIDER_PYPROJECT = ROOT / "integrations" / "hermes-provider" / "pyproject.toml"
ENGINE_MANIFEST = ROOT / "integrations" / "engine-patches" / "manifest.json"

# Objectives this repo can measure. `retrieval-mrr` comes from
# bench/precision_recall_eval.py; nothing uses it yet, and an unused objective
# is cheaper than a rename later.
OBJECTIVES = ("precisionmembench", "task/write-throughput", "retrieval-mrr")

PROBLEM_ID = re.compile(r"^P[0-9]{2,3}$")
PATCH_ID = re.compile(r"^P[0-9]{1,3}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WHITESPACE = re.compile(r"\s+")

PROBLEM_FIELDS = {"id", "name", "opened", "attempts"}
OPENED_FIELDS = {"date", "objective"}
ATTEMPT_FIELDS = {
    "round",
    "accepted",
    "backfilled",
    "what",
    "files",
    "patch",
    "predicted",
    "fixed",
    "missed",
    "collateral",
    "passed",
    "total",
    "predicted_at",
    "evaluated_at",
    "provenance",
}
PROVENANCE_FIELDS = {
    "objective",
    "fixture_sha256",
    "engine_pin",
    "engine_patches_sha256",
    "hermes_version",
    "git_commit",
    "model",
}
CARRIED_FIELDS = {"date", "sources", "note"}


class Refused(RuntimeError):
    """The command cannot proceed; the ledger is left unchanged."""


# --- small helpers ---------------------------------------------------------


def normalize_name(name: str) -> str:
    """Deterministic identity for a root-cause hypothesis name."""
    text = _WHITESPACE.sub(" ", str(name or "")).strip().lower()
    return text.rstrip(". ")


def now_utc() -> str:
    return _datetime.datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> _datetime.datetime | None:
    """Parse a `YYYY-MM-DD` date or an ISO-8601 instant, in UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return _datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)
        parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def engine_pin() -> str | None:
    """The engine pin this repo declares, read from the provider's pyproject."""
    try:
        import tomllib

        data = tomllib.loads(PROVIDER_PYPROJECT.read_text(encoding="utf-8"))
        for requirement in data["project"]["dependencies"]:
            if "mnemosyne-memory" in requirement:
                return str(requirement)
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return None


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and GIT_COMMIT.match(value) else None


def hermes_version(requested: str | None = None) -> str | None:
    if requested:
        return requested.strip() or None
    try:
        import importlib.metadata

        return importlib.metadata.version("hermes-agent")
    except Exception:
        return None


def detected_model(requested: str | None) -> str | None:
    """The model that proposed a change, when the operator names it."""
    if requested:
        return requested.strip() or None
    return os.environ.get("SIM_MODEL") or None


def default_provenance(
    objective: str,
    *,
    fixture_sha256: dict[str, str] | None = None,
    hermes: str | None = None,
    commit: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    return {
        "objective": objective,
        "fixture_sha256": fixture_sha256 or None,
        "engine_pin": engine_pin(),
        "engine_patches_sha256": _sha256_file(ENGINE_MANIFEST),
        "hermes_version": hermes_version(hermes),
        "git_commit": commit or git_commit(),
        "model": detected_model(model),
    }


# --- ledger io -------------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Refused(f"no ledger at {path}; create it with `open` first")
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # Fail loud: an unreadable ledger is broken, never empty.
        raise Refused(f"cannot read the ledger at {path}: {error}") from error
    if not isinstance(ledger, dict):
        raise Refused(f"{path} is not a JSON object")
    return ledger


def save(path: Path, ledger: dict[str, Any]) -> None:
    """Atomic replace, so an interrupted write cannot truncate the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".sim-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            target.write(payload)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def empty_ledger() -> dict[str, Any]:
    return {
        "format": FORMAT,
        "next_id": 1,
        "carried_from": {"date": now_utc()[:10], "sources": [], "note": ""},
        "aliases": {},
        "problems": [],
    }


def find_problem(ledger: dict[str, Any], reference: str) -> dict[str, Any]:
    problems = ledger.get("problems") or []
    text = str(reference or "").strip()
    if not text:
        raise Refused("a problem reference is required")
    aliases = ledger.get("aliases") or {}
    for candidate in (text, text.upper()):
        if PROBLEM_ID.match(candidate):
            for problem in problems:
                if problem.get("id") == candidate:
                    return problem
            # An alias may legitimately look like a P-number (`p48`), so an
            # unmatched id-shaped reference falls through to the alias table.
            break
    alias = aliases.get(text) or aliases.get(text.lower())
    if alias:
        return find_problem(ledger, alias)
    wanted = normalize_name(text)
    matches = [p for p in problems if normalize_name(p.get("name", "")) == wanted]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise Refused(f"ambiguous problem name {text!r}: {[p['id'] for p in matches]}")
    raise Refused(f"unknown problem {text!r}; use an id, an alias, or the exact name")


def find_attempt(ledger: dict[str, Any], reference: str) -> tuple[dict[str, Any], dict[str, Any]]:
    problem_ref, _, round_ref = str(reference or "").strip().rpartition("/")
    if not problem_ref or not round_ref.strip().isdigit():
        raise Refused(f"attempt reference must look like P05/7, got {reference!r}")
    problem = find_problem(ledger, problem_ref)
    wanted = int(round_ref)
    for attempt in problem.get("attempts") or []:
        if attempt.get("round") == wanted:
            return problem, attempt
    raise Refused(f"{problem['id']} has no attempt round {wanted}")


def next_problem_id(ledger: dict[str, Any]) -> str:
    number = int(ledger.get("next_id") or 1)
    used = {problem.get("id") for problem in ledger.get("problems") or []}
    candidate = f"P{number:02d}"
    while candidate in used:
        number += 1
        candidate = f"P{number:02d}"
    ledger["next_id"] = number + 1
    return candidate


# --- commands --------------------------------------------------------------


def cmd_open(ledger: dict[str, Any], args: argparse.Namespace) -> int:
    name = _WHITESPACE.sub(" ", args.name or "").strip()
    if not name:
        raise Refused("--name is required")
    if args.objective not in OBJECTIVES:
        raise Refused(f"--objective must be one of {', '.join(OBJECTIVES)}")
    requested = [str(alias).strip() for alias in args.alias or [] if str(alias).strip()]
    wanted = normalize_name(name)
    existing = next(
        (
            problem
            for problem in ledger.get("problems") or []
            if normalize_name(problem.get("name", "")) == wanted
        ),
        None,
    )
    aliases = ledger.setdefault("aliases", {})
    if existing is not None:
        problem_id = existing["id"]
        merged = True
        opened = existing.get("opened") or {}
        if opened.get("objective") not in (None, args.objective):
            raise Refused(
                f"{problem_id} is already open against objective {opened['objective']}; "
                f"a root cause belongs to one objective (use a new --name for {args.objective})"
            )
    else:
        problem_id = next_problem_id(ledger)
        merged = False
    names = {
        normalize_name(problem.get("name", "")): problem["id"]
        for problem in ledger.get("problems") or []
        if problem.get("id") != problem_id
    }
    for alias in requested:
        if normalize_name(alias) in names:
            # An alias equal to a problem's name would make `--problem <name>`
            # resolve to the alias target instead of the problem that owns the
            # name. Refuse rather than become ambiguous.
            raise Refused(
                f"alias {alias!r} would shadow the name of {names[normalize_name(alias)]}"
            )
        owner = aliases.get(alias.lower())
        if owner is not None and owner != problem_id:
            raise Refused(f"alias {alias!r} already points at {owner}")
    if not merged:
        ledger.setdefault("problems", []).append(
            {
                "id": problem_id,
                "name": name,
                "opened": {"date": args.date or now_utc()[:10], "objective": args.objective},
                "attempts": [],
            }
        )
    for alias in requested:
        aliases[alias.lower()] = problem_id
    print(f"{problem_id} (merged into the existing root cause)" if merged else problem_id)
    return 0


def _split_cases(value: str | None) -> list[str]:
    if not value:
        return []
    seen: list[str] = []
    for chunk in value.split(","):
        case = chunk.strip()
        if case and case not in seen:
            seen.append(case)
    return seen


def cmd_predict(ledger: dict[str, Any], args: argparse.Namespace) -> int:
    problem = find_problem(ledger, args.problem)
    cases = _split_cases(args.cases)
    if not cases:
        raise Refused("--cases names at least one case id that the change should fix")
    what = _WHITESPACE.sub(" ", args.what or "").strip()
    if not what:
        raise Refused("--what records the change and its rationale")
    attempts = problem.setdefault("attempts", [])
    if args.round is None:
        round_number = max((int(a.get("round") or 0) for a in attempts), default=-1) + 1
    else:
        round_number = int(args.round)
    if any(int(a.get("round") or 0) == round_number for a in attempts):
        raise Refused(f"{problem['id']} already has round {round_number}")
    patch = (args.patch or "").strip() or None
    if patch and not PATCH_ID.match(patch):
        raise Refused(f"--patch must look like P51, got {patch!r}")
    files = [str(item).strip() for item in (args.files or []) if str(item).strip()]
    for path in files:
        if Path(path).is_absolute():
            raise Refused(f"--files takes repo-relative paths, got {path!r}")
    provenance = default_provenance(
        problem["opened"]["objective"],
        hermes=args.hermes_version,
        commit=args.git_commit,
        model=args.model,
    )
    attempts.append(
        {
            "round": round_number,
            "accepted": False,
            "backfilled": False,
            "what": what,
            "files": files,
            "patch": patch,
            "predicted": cases,
            "fixed": [],
            "missed": [],
            "collateral": [],
            "passed": None,
            "total": None,
            "predicted_at": args.date or now_utc(),
            "evaluated_at": None,
            "provenance": provenance,
        }
    )
    print(f"{problem['id']}/{round_number}")
    return 0


def _read_report(path: Path) -> tuple[dict[str, bool], dict[str, str]]:
    """Per-case pass map and fixture digests from an evaluator report.

    A duplicated case id is refused rather than collapsed: two verdicts for one
    case make every count downstream a guess.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Refused(f"cannot read the report at {path}: {error}") from error
    report = payload.get("report", payload) if isinstance(payload, dict) else None
    if not isinstance(report, dict) or not isinstance(report.get("cases"), list):
        raise Refused(
            f"{path} carries no per-case report; expected the output of research/evaluate.py"
        )
    cases: dict[str, bool] = {}
    for case in report["cases"]:
        if not isinstance(case, dict) or "caseId" not in case:
            raise Refused(f"{path} has a case without a caseId")
        case_id = str(case["caseId"])
        if case_id in cases:
            raise Refused(f"{path} reports case {case_id!r} twice")
        cases[case_id] = bool(case.get("passed"))
    if not cases:
        raise Refused(f"{path} reports zero cases; refusing to record an empty run")
    fixt = report.get("fixtureSha256")
    fixtures = (
        {str(key): str(value) for key, value in fixt.items()} if isinstance(fixt, dict) else {}
    )
    return cases, fixtures


def cmd_record(ledger: dict[str, Any], args: argparse.Namespace) -> int:
    """Fill an attempt's outcome from a before/after pair of evaluator reports.

    `fixed` is what the change newly fixed: a predicted case that FAILED in the
    baseline report and passes in this one. A predicted case that already passed
    in the baseline cannot be counted as fixed, and is reported separately —
    otherwise a "made it worse" or "changed nothing" attempt credits itself with
    someone else's earlier fix. `collateral` is the other direction: a case that
    passed in the baseline and fails now.
    """
    problem, attempt = find_attempt(ledger, args.attempt)
    if attempt.get("evaluated_at"):
        raise Refused(f"{problem['id']}/{attempt['round']} is already recorded")
    predicted = list(attempt.get("predicted") or [])
    if not predicted:
        raise Refused(
            f"{problem['id']}/{attempt['round']} has no prediction; "
            "`predict` must run before the evaluation"
        )
    if not attempt.get("predicted_at"):
        raise Refused(f"{problem['id']}/{attempt['round']} has no prediction timestamp")
    cases, fixtures = _read_report(Path(args.report))
    before, baseline_fixtures = _read_report(Path(args.baseline))
    if fixtures != baseline_fixtures:
        raise Refused(
            "the report and the baseline were measured on different fixtures "
            f"({fixtures or 'none recorded'} vs {baseline_fixtures or 'none recorded'}); "
            "a change measured across two fixture sets is not a comparison"
        )
    unknown = [case for case in predicted if case not in cases or case not in before]
    if unknown:
        raise Refused(
            f"the predicted case(s) {unknown} are missing from the report or the baseline; "
            "a typo in --cases would otherwise be recorded as a miss"
        )
    objective = (attempt.get("provenance") or {}).get("objective")
    if objective not in OBJECTIVES:
        raise Refused(f"{problem['id']}/{attempt['round']} has no known objective to record")
    fixed = [case for case in predicted if not before[case] and cases[case]]
    missed = [case for case in predicted if not cases[case]]
    already = [case for case in predicted if before[case] and cases[case]]
    collateral = [
        case
        for case in before
        if before[case] and case in cases and not cases[case] and case not in predicted
    ]
    attempt.update(
        {
            "fixed": fixed,
            "missed": missed,
            "collateral": collateral,
            "passed": sum(1 for passed in cases.values() if passed),
            "total": len(cases),
            "evaluated_at": args.date or now_utc(),
            "provenance": default_provenance(
                objective,
                fixture_sha256=fixtures,
                hermes=args.hermes_version
                or (attempt.get("provenance") or {}).get("hermes_version"),
                commit=args.git_commit
                or git_commit()
                or (attempt.get("provenance") or {}).get("git_commit"),
                model=args.model or (attempt.get("provenance") or {}).get("model"),
            ),
        }
    )
    attempt["accepted"] = bool(args.accept)
    print(
        f"{problem['id']}/{attempt['round']}: {len(fixed)}/{len(predicted)} newly fixed, "
        f"{len(missed)} missed, {attempt['passed']}/{attempt['total']} passing, "
        f"{len(collateral)} collateral, accepted={attempt['accepted']}"
    )
    if already:
        print(
            f"  note: {len(already)} predicted case(s) already passed in the baseline "
            f"({', '.join(already)}); they are not counted as fixed"
        )
    return 0


def _calibration(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for problem in ledger.get("problems") or []:
        for attempt in problem.get("attempts") or []:
            predicted = attempt.get("predicted") or []
            if not predicted or not attempt.get("evaluated_at"):
                continue
            objective = attempt.get("provenance", {}).get("objective") or "unknown"
            for bucket_name, backfilled in (("fresh", False), ("backfilled", True)):
                if bool(attempt.get("backfilled")) != backfilled:
                    continue
                row = rows.setdefault(
                    f"{objective}|{bucket_name}",
                    {
                        "objective": objective,
                        "written": bucket_name,
                        "predicted": 0,
                        "fixed": 0,
                        "attempts": 0,
                    },
                )
                row["predicted"] += len(predicted)
                row["fixed"] += len(attempt.get("fixed") or [])
                row["attempts"] += 1
    for row in rows.values():
        row["hit_rate"] = row["fixed"] / row["predicted"] if row["predicted"] else None
    return rows


def render(ledger: dict[str, Any]) -> str:
    problems = ledger.get("problems") or []
    attempts = [a for p in problems for a in p.get("attempts") or []]
    backfilled = sum(1 for a in attempts if a.get("backfilled"))
    lines = [
        "## Self-Improvement Memory",
        "",
        f"`{FORMAT}` · {len(problems)} root causes · {len(attempts)} attempts "
        f"({backfilled} backfilled) · carried from "
        + (
            ", ".join(
                f"`{source}`" for source in ledger.get("carried_from", {}).get("sources") or []
            )
            or "nothing"
        ),
        "",
    ]
    calibration = _calibration(ledger)
    if calibration:
        lines += [
            "### Prediction calibration (fixed / predicted)",
            "",
            "| objective | written | attempts | predicted | fixed | hit rate |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
        for key in sorted(calibration):
            row = calibration[key]
            hit = "n/a" if row["hit_rate"] is None else f"{row['hit_rate']:.0%}"
            lines.append(
                f"| {row['objective']} | {row['written']} | {row['attempts']} | "
                f"{row['predicted']} | {row['fixed']} | {hit} |"
            )
        lines.append("")
    for problem in problems:
        opened = problem.get("opened") or {}
        lines += [
            f"### {problem['id']} {problem['name']}",
            "",
            f"opened {opened.get('date', '?')} · objective {opened.get('objective', '?')}",
            "",
            "| round | verdict | patch | passed/total | pred | fixed | missed | what |",
            "| ---: | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
        for attempt in problem.get("attempts") or []:
            if attempt.get("evaluated_at") is None:
                verdict = "predicted"
            elif attempt.get("accepted"):
                verdict = "accepted" if not attempt.get("backfilled") else "accepted (backfilled)"
            else:
                verdict = "rejected" if not attempt.get("backfilled") else "rejected (backfilled)"
            passed = attempt.get("passed")
            total = attempt.get("total")
            tally = "—" if passed is None or total is None else f"{passed}/{total}"
            lines.append(
                f"| {attempt.get('round')} | {verdict} | {attempt.get('patch') or '—'} | "
                f"{tally} | {len(attempt.get('predicted') or [])} | "
                f"{len(attempt.get('fixed') or [])} | {len(attempt.get('missed') or [])} | "
                f"{_WHITESPACE.sub(' ', str(attempt.get('what') or ''))} |"
            )
        collateral_rounds = [
            (attempt, attempt.get("collateral") or [])
            for attempt in problem.get("attempts") or []
            if attempt.get("collateral")
        ]
        if collateral_rounds:
            lines += ["", "Collateral (kept regressions, never dropped):"]
            for attempt, cases in collateral_rounds:
                lines.append(f"- round {attempt.get('round')}: {', '.join(cases)}")
        provenance_rounds = [
            (attempt, attempt.get("provenance") or {})
            for attempt in problem.get("attempts") or []
            if attempt.get("evaluated_at") and attempt.get("provenance")
        ]
        if provenance_rounds:
            lines += ["", "Provenance:"]
            for attempt, provenance in provenance_rounds:
                detail = [
                    f"engine {provenance.get('engine_pin') or 'not recorded'}",
                    f"commit {provenance.get('git_commit') or 'not recorded'}",
                    f"model {provenance.get('model') or 'not recorded'}",
                    f"evaluated {attempt.get('evaluated_at')}",
                ]
                if provenance.get("hermes_version"):
                    detail.append(f"hermes {provenance['hermes_version']}")
                lines.append(f"- round {attempt.get('round')}: " + ", ".join(detail))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_render(ledger: dict[str, Any], args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(ledger, indent=2, sort_keys=True))
        return 0
    print(render(ledger), end="")
    return 0


# --- validation ------------------------------------------------------------


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _validate_provenance(where: str, provenance: Any, errors: list[str]) -> None:
    if not isinstance(provenance, dict):
        errors.append(f"{where}: provenance must be an object")
        return
    missing = PROVENANCE_FIELDS - set(provenance)
    extra = set(provenance) - PROVENANCE_FIELDS
    _require(not missing, f"{where}: provenance is missing {sorted(missing)}", errors)
    _require(not extra, f"{where}: provenance has unknown fields {sorted(extra)}", errors)
    _require(
        provenance.get("objective") in OBJECTIVES,
        f"{where}: provenance.objective must be one of {OBJECTIVES}",
        errors,
    )
    pin = provenance.get("engine_pin")
    _require(
        pin is None or isinstance(pin, str),
        f"{where}: provenance.engine_pin must be a string or null",
        errors,
    )
    patch_digest = provenance.get("engine_patches_sha256")
    _require(
        patch_digest is None or (isinstance(patch_digest, str) and SHA256.match(patch_digest)),
        f"{where}: provenance.engine_patches_sha256 must be a sha256 or null",
        errors,
    )
    commit = provenance.get("git_commit")
    _require(
        commit is None or (isinstance(commit, str) and GIT_COMMIT.match(commit)),
        f"{where}: provenance.git_commit must be a git hash or null",
        errors,
    )
    fixtures = provenance.get("fixture_sha256")
    if fixtures is not None:
        ok = (
            isinstance(fixtures, dict)
            and fixtures
            and all(
                isinstance(name, str) and isinstance(digest, str) and SHA256.match(digest)
                for name, digest in fixtures.items()
            )
        )
        _require(bool(ok), f"{where}: provenance.fixture_sha256 must map names to sha256", errors)
    for field in ("hermes_version", "model"):
        value = provenance.get(field)
        _require(
            value is None or (isinstance(value, str) and value.strip()),
            f"{where}: provenance.{field} must be a non-empty string or null",
            errors,
        )


def _validate_attempt(where: str, attempt: Any, errors: list[str]) -> None:
    if not isinstance(attempt, dict):
        errors.append(f"{where}: attempt must be an object")
        return
    missing = ATTEMPT_FIELDS - set(attempt)
    extra = set(attempt) - ATTEMPT_FIELDS
    _require(not missing, f"{where}: attempt is missing {sorted(missing)}", errors)
    _require(not extra, f"{where}: attempt has unknown fields {sorted(extra)}", errors)
    _require(isinstance(attempt.get("round"), int), f"{where}: round must be an integer", errors)
    for field in ("accepted", "backfilled"):
        _require(
            isinstance(attempt.get(field), bool), f"{where}: {field} must be a boolean", errors
        )
    what = attempt.get("what")
    _require(
        isinstance(what, str) and what.strip(),
        f"{where}: what must describe the change and its rationale",
        errors,
    )
    files = attempt.get("files")
    _require(
        isinstance(files, list) and all(isinstance(item, str) and item for item in files),
        f"{where}: files must be a list of repo-relative paths",
        errors,
    )
    patch = attempt.get("patch")
    _require(
        patch is None or (isinstance(patch, str) and PATCH_ID.match(patch)),
        f"{where}: patch must be a P-number or null",
        errors,
    )
    for field in ("predicted", "fixed", "missed", "collateral"):
        value = attempt.get(field)
        ok = isinstance(value, list) and all(isinstance(item, str) and item for item in value)
        _require(bool(ok), f"{where}: {field} must be a list of case ids", errors)
        if ok:
            _require(len(set(value)) == len(value), f"{where}: {field} has duplicates", errors)
    predicted = set(attempt.get("predicted") or [])
    fixed = set(attempt.get("fixed") or [])
    missed = set(attempt.get("missed") or [])
    collateral = set(attempt.get("collateral") or [])
    backfilled = attempt.get("backfilled") is True
    _require(
        predicted or backfilled,
        f"{where}: a prediction must be written before the run",
        errors,
    )
    _require(fixed <= predicted, f"{where}: fixed is not a subset of predicted", errors)
    _require(missed <= predicted, f"{where}: missed is not a subset of predicted", errors)
    _require(not (fixed & missed), f"{where}: a case is both fixed and missed", errors)
    _require(
        not (collateral & predicted),
        f"{where}: collateral must be cases the prediction did not cover",
        errors,
    )
    passed = attempt.get("passed")
    total = attempt.get("total")
    for field, value in (("passed", passed), ("total", total)):
        _require(
            value is None
            or (isinstance(value, int) and not isinstance(value, bool) and value >= 0),
            f"{where}: {field} must be a non-negative integer or null",
            errors,
        )
    if isinstance(passed, int) and isinstance(total, int):
        _require(passed <= total, f"{where}: passed exceeds total", errors)
    predicted_at = parse_timestamp(attempt.get("predicted_at"))
    evaluated_at = parse_timestamp(attempt.get("evaluated_at"))
    _require(predicted_at is not None, f"{where}: predicted_at is not a timestamp", errors)
    _require(
        attempt.get("evaluated_at") is None or evaluated_at is not None,
        f"{where}: evaluated_at is not a timestamp",
        errors,
    )
    if predicted_at and evaluated_at and not backfilled:
        # Not "strictly earlier": a prediction and its evaluation can land in
        # the same second. A prediction written *after* the evaluation is the
        # case that must be flagged as reconstructed, so only that is refused.
        _require(
            predicted_at <= evaluated_at,
            f"{where}: predicted_at is later than evaluated_at "
            "(mark the attempt backfilled if it was reconstructed afterwards)",
            errors,
        )
    _validate_provenance(where, attempt.get("provenance"), errors)


def validate(ledger: Any) -> list[str]:
    """Every rule the ledger must satisfy. Returns a list of errors, empty when valid."""
    errors: list[str] = []
    if not isinstance(ledger, dict):
        return ["the ledger must be a JSON object"]
    expected = {"format", "next_id", "carried_from", "aliases", "problems"}
    _require(
        not (expected - set(ledger)),
        f"top level is missing {sorted(expected - set(ledger))}",
        errors,
    )
    _require(
        not (set(ledger) - expected),
        f"top level has unknown keys {sorted(set(ledger) - expected)}",
        errors,
    )
    _require(ledger.get("format") == FORMAT, f"format must be {FORMAT}", errors)
    next_id = ledger.get("next_id")
    _require(
        isinstance(next_id, int) and not isinstance(next_id, bool) and next_id >= 1,
        "next_id must be a positive integer",
        errors,
    )
    carried = ledger.get("carried_from")
    if isinstance(carried, dict):
        _require(not (CARRIED_FIELDS - set(carried)), "carried_from is missing fields", errors)
        _require(not (set(carried) - CARRIED_FIELDS), "carried_from has unknown fields", errors)
        _require(
            parse_timestamp(carried.get("date")) is not None,
            "carried_from.date is not a date",
            errors,
        )
        sources = carried.get("sources")
        _require(
            isinstance(sources, list) and all(isinstance(item, str) for item in sources),
            "carried_from.sources must be a list of paths",
            errors,
        )
        for source in sources if isinstance(sources, list) else []:
            if not (ROOT / source).exists():
                errors.append(f"carried_from names a missing source file: {source}")
        _require(isinstance(carried.get("note"), str), "carried_from.note must be a string", errors)
    else:
        errors.append("carried_from must be an object")

    problems = ledger.get("problems")
    if not isinstance(problems, list):
        return errors + ["problems must be a list"]
    ids: list[str] = []
    names: dict[str, str] = {}
    for index, problem in enumerate(problems):
        where = f"problems[{index}]"
        if not isinstance(problem, dict):
            errors.append(f"{where}: problem must be an object")
            continue
        problem_id = problem.get("id")
        if isinstance(problem_id, str) and PROBLEM_ID.match(problem_id):
            where = f"problem {problem_id}"
            ids.append(problem_id)
            if isinstance(next_id, int) and int(problem_id[1:]) >= next_id:
                errors.append(f"{where}: id is not below next_id {next_id}")
        else:
            errors.append(f"{where}: id must look like P05")
        _require(
            not (PROBLEM_FIELDS - set(problem)),
            f"{where}: missing {sorted(PROBLEM_FIELDS - set(problem))}",
            errors,
        )
        _require(
            not (set(problem) - PROBLEM_FIELDS),
            f"{where}: unknown fields {sorted(set(problem) - PROBLEM_FIELDS)}",
            errors,
        )
        name = problem.get("name")
        if isinstance(name, str) and name.strip():
            normalized = normalize_name(name)
            if normalized in names:
                errors.append(
                    f"{where}: name duplicates {names[normalized]} (identical names merge)"
                )
            names[normalized] = str(problem_id)
        else:
            errors.append(f"{where}: name must be the root-cause hypothesis")
        opened = problem.get("opened")
        if isinstance(opened, dict):
            _require(
                not (OPENED_FIELDS - set(opened)), f"{where}: opened is missing fields", errors
            )
            _require(
                not (set(opened) - OPENED_FIELDS), f"{where}: opened has unknown fields", errors
            )
            _require(
                parse_timestamp(opened.get("date")) is not None,
                f"{where}: opened.date is not a date",
                errors,
            )
            _require(
                opened.get("objective") in OBJECTIVES,
                f"{where}: opened.objective is not a known objective",
                errors,
            )
        else:
            errors.append(f"{where}: opened must be an object")
        attempts = problem.get("attempts")
        if not isinstance(attempts, list):
            errors.append(f"{where}: attempts must be a list")
            continue
        rounds: list[int] = []
        for attempt in attempts:
            round_number = attempt.get("round") if isinstance(attempt, dict) else None
            attempt_where = f"{where} round {round_number}"
            if isinstance(round_number, int):
                if round_number in rounds:
                    errors.append(f"{attempt_where}: duplicate round")
                rounds.append(round_number)
            _validate_attempt(attempt_where, attempt, errors)
    if len(set(ids)) != len(ids):
        errors.append("problem ids are not unique")

    aliases = ledger.get("aliases")
    if isinstance(aliases, dict):
        upper_ids = {problem_id.upper(): problem_id for problem_id in ids}
        for alias, target in aliases.items():
            if not isinstance(alias, str) or not alias.strip():
                errors.append("aliases must be non-empty strings")
                continue
            if alias.upper() in upper_ids:
                errors.append(f"alias {alias!r} collides with a problem id")
            if normalize_name(alias) in names:
                errors.append(f"alias {alias!r} shadows the name of {names[normalize_name(alias)]}")
            if target not in ids:
                errors.append(f"alias {alias!r} points at unknown problem {target!r}")
    else:
        errors.append("aliases must be an object")
    return errors


def cmd_validate(args: argparse.Namespace) -> int:
    """Check the ledger on disk. Takes no in-memory ledger: it reads the file."""
    path = Path(args.file) if args.file else DEFAULT_LEDGER
    ledger = load(path)
    errors = validate(ledger)
    if errors:
        print(f"INVALID {path}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    problems = len(ledger.get("problems") or [])
    attempts = sum(len(problem.get("attempts") or []) for problem in ledger.get("problems") or [])
    print(f"VALID {path}: {problems} problems, {attempts} attempts")
    return 0


# --- entry point -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="ledger path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    opener = subparsers.add_parser("open", help="add a root-cause hypothesis")
    opener.add_argument("--name", required=True, help="the root-cause hypothesis")
    opener.add_argument("--objective", required=True, choices=OBJECTIVES)
    opener.add_argument("--alias", action="append", help="alternate name (repeatable)")
    opener.add_argument("--date", help="opened date (default: today, UTC)")

    predictor = subparsers.add_parser("predict", help="record an attempt and its prediction")
    predictor.add_argument("--problem", required=True)
    predictor.add_argument("--cases", required=True, help="case ids predicted to be fixed")
    predictor.add_argument("--what", required=True, help="the change and its rationale")
    predictor.add_argument("--files", action="append", help="repo-relative file changed")
    predictor.add_argument("--patch", help="P-number of the local patch, if any")
    predictor.add_argument("--round", type=int, help="round number (default: next)")
    predictor.add_argument("--date", help="prediction timestamp (default: now, UTC)")
    predictor.add_argument(
        "--model",
        help="model that proposed the change (default: $SIM_MODEL, else null)",
    )
    predictor.add_argument("--hermes-version", dest="hermes_version")
    predictor.add_argument("--git-commit", dest="git_commit")

    recorder = subparsers.add_parser("record", help="fill an attempt's outcome from a report")
    recorder.add_argument("--attempt", required=True, help="problem/round, e.g. P05/7")
    recorder.add_argument("--report", required=True, help="output of research/evaluate.py")
    recorder.add_argument(
        "--baseline",
        required=True,
        help="report of the state BEFORE this attempt (the previous iteration)",
    )
    verdict = recorder.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true", help="the change was kept")
    verdict.add_argument(
        "--reject", dest="accept", action="store_false", help="the change was reverted"
    )
    recorder.add_argument("--date", help="evaluation timestamp (default: now, UTC)")
    recorder.add_argument(
        "--model",
        help="model that proposed the change (default: $SIM_MODEL, else the predicted value)",
    )
    recorder.add_argument("--hermes-version", dest="hermes_version")
    recorder.add_argument("--git-commit", dest="git_commit")

    renderer = subparsers.add_parser("render", help="print the Markdown ledger")
    renderer.add_argument("--json", action="store_true", help="print the JSON ledger instead")

    validator = subparsers.add_parser("validate", help="check the ledger against the schema")
    validator.add_argument("--file", type=Path, help="ledger to check (default: research/sim.json)")
    return parser


COMMANDS = {
    "open": cmd_open,
    "predict": cmd_predict,
    "record": cmd_record,
    "render": cmd_render,
    "validate": cmd_validate,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "open" and not args.ledger.is_file():
            # `open` is the first step of a ledger's life, so it may create it.
            ledger = empty_ledger()
        else:
            ledger = load(args.ledger)
        if args.command != "render":
            errors = validate(ledger)
            if errors:
                raise Refused(
                    "the ledger is invalid; fix these before writing:\n  - " + "\n  - ".join(errors)
                )
        status = COMMANDS[args.command](ledger, args)
        if args.command in ("open", "predict", "record"):
            # The command may have accepted an argument the schema rejects (a
            # malformed --date, say). Refuse the write instead of storing a
            # ledger the next gate run will fail on.
            errors = validate(ledger)
            if errors:
                raise Refused(
                    "the resulting ledger would be invalid; nothing was written:\n  - "
                    + "\n  - ".join(errors)
                )
            save(args.ledger, ledger)
        return status
    except Refused as error:
        print(f"sim: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
