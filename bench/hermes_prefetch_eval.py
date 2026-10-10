"""Reproducible, keyless evaluation of the real provider's public prefetch hook.

Run with the audited patched engine on PYTHONPATH. --provider-ref HEAD evaluates
the committed baseline in a separate process/import tree. Reports retain only
synthetic fixture content; no operator config or memory databases are read.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import importlib.metadata
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("hermes_prefetch_cases.json")
PACKAGE = "integrations/hermes-provider/hermes_memory_provider"


def grade(case: dict, output: str, labels: set[str]) -> dict:
    """Outcome grading against independently labelled fixture rows."""
    found = {label for label in labels if f"[EVAL_{label.upper()}]" in output}
    expected = set(case["expected"])
    allowed = set(case["allowed"])
    missing = sorted(expected - found)
    forbidden = sorted(found & set(case["forbidden"]))
    irrelevant = sorted(found - allowed)
    oversized = len(output) > case.get("max_chars", 8000)
    duplicates = sum(max(0, output.count(f"[EVAL_{label.upper()}]") - 1) for label in found)
    return {
        "passed": not (missing or forbidden or irrelevant or oversized or duplicates),
        "found": sorted(found),
        "missing": missing,
        "forbidden": forbidden,
        "irrelevant": irrelevant,
        "identity_covered": "identity" in found,
        "label_recall": len(expected & found) / len(expected) if expected else None,
        "label_precision": len(allowed & found) / len(found) if found else 0.0,
        "duplicate_markers": duplicates,
        "chars": len(output),
        "estimated_tokens": (len(output) + 3) // 4,
        "oversized": oversized,
    }


def summarize_held_out(records: list[dict], cases: list[dict]) -> dict:
    """Aggregate task outcomes from per-case labels, never from latency rows."""
    case_by_id = {case["id"]: case for case in cases}
    selected = [row for row in records if row.get("id") in case_by_id]
    scored = [row for row in selected if "error" not in row]
    expected_count = sum(len(case_by_id[row["id"]].get("expected", [])) for row in scored)
    found_count = sum(
        len(set(case_by_id[row["id"]].get("expected", [])) & set(row.get("found", [])))
        for row in scored
    )
    forbidden_count = sum(len(case_by_id[row["id"]].get("forbidden", [])) for row in scored)
    forbidden_found = sum(len(row.get("forbidden", [])) for row in scored)
    forbidden_cases = sum(bool(row.get("forbidden")) for row in scored)
    context_passes = sum(not row.get("oversized", False) for row in scored)
    return {
        "case_count": len(scored),
        "execution_errors": len(selected) - len(scored),
        "required_label_count": expected_count,
        "required_labels_found": found_count,
        "task_recall": found_count / expected_count if expected_count else None,
        "forbidden_label_count": forbidden_count,
        "forbidden_labels_found": forbidden_found,
        "forbidden_fact_label_rate": forbidden_found / forbidden_count if forbidden_count else 0.0,
        "cases_with_forbidden_facts": forbidden_cases,
        "forbidden_fact_case_rate": forbidden_cases / len(scored) if scored else 0.0,
        "within_context_budget_cases": context_passes,
        "context_budget_pass_rate": context_passes / len(scored) if scored else 0.0,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _provider_tree(ref: str | None, temporary: Path) -> Path:
    if ref is None:
        return ROOT / "integrations/hermes-provider"
    destination = temporary / "baseline"
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", PACKAGE], cwd=ROOT, text=True
    ).splitlines()
    if not files:
        raise ValueError(f"No provider package at git ref {ref!r}")
    for name in files:
        if not name.endswith(".py"):
            continue
        target = destination / Path(name).relative_to("integrations/hermes-provider")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "show", f"{ref}:{name}"], cwd=ROOT))
    return destination


def run(ref: str | None, repetitions: int, cache: bool = False) -> dict:
    fixtures = json.loads(CASES.read_text(encoding="utf-8"))
    records = []
    connections = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    with tempfile.TemporaryDirectory(prefix="mnemosyne-prefetch-eval-") as folder:
        temporary = Path(folder)
        provider_root = _provider_tree(ref, temporary)
        sys.path.insert(0, str(provider_root))
        import hermes_memory_provider as provider_module

        environment = {
            "HERMES_HOME": str(temporary),
            "MNEMOSYNE_DATA_DIR": str(temporary / "engine"),
            "MNEMOSYNE_EMBEDDINGS_OFF": "1",
            "MNEMOSYNE_PREFETCH_CACHE_ENABLED": "1" if cache else "0",
        }
        # Exclude operator knobs that could invalidate fixture comparability.
        clean_environment = {
            key: value for key, value in os.environ.items() if not key.startswith("MNEMOSYNE_")
        }
        provider = None
        started = time.perf_counter()
        try:
            with (
                patch.dict(os.environ, {**clean_environment, **environment}, clear=True),
                patch.object(sqlite3, "connect", side_effect=tracked_connect),
            ):
                provider = provider_module.MnemosyneMemoryProvider()
                provider._init_audit_log = lambda: None
                provider._read_config_key = lambda key: None
                provider.initialize(
                    session_id="eval-contact",
                    hermes_home=str(temporary),
                    agent_context="primary",
                    db_path=str(temporary / "fixture.db"),
                    auto_sleep=False,
                    shared_surface_path=str(temporary / "shared.db"),
                )
                if provider._beam is None:
                    raise RuntimeError(f"Provider failed to initialize: {provider._init_error}")
                initialization_ms = (time.perf_counter() - started) * 1000
                for row in fixtures["memories"]:
                    if row.get("store") == "canonical":
                        provider._beam.canonical.remember(
                            row["owner"],
                            "model:workflow",
                            "formatter",
                            row["content"],
                            source=row["source"],
                            confidence=1.0,
                        )
                        continue
                    provider._beam.session_id = row.get("session", "eval-contact")
                    content = row["content"]
                    content += content.split("]", 1)[-1] * (row.get("repeat", 1) - 1)
                    provider._beam.remember(
                        content,
                        importance=0.95,
                        source=row["source"],
                        scope="session",
                        valid_until=row.get("valid_until"),
                    )
                provider._beam.session_id = "eval-contact"
                labels = {row["label"] for row in fixtures["memories"]}
                all_cases = fixtures["cases"] + fixtures.get("held_out_cases", [])
                for case in all_cases:
                    if case.get("overflow_identity"):
                        provider._beam.remember(
                            "Extra synthetic contact details: " + "context " * 1800,
                            importance=0.90,
                            source="identity",
                            scope="session",
                        )
                    timings = []
                    outputs = []
                    try:
                        for _ in range(repetitions):
                            tick = time.perf_counter()
                            output = provider.prefetch(case["query"], session_id="eval-contact")
                            timings.append((time.perf_counter() - tick) * 1000)
                            outputs.append(output)
                            diagnostics = getattr(
                                provider, "get_prefetch_diagnostics", lambda: {}
                            )()
                            if diagnostics.get("last_error"):
                                raise RuntimeError(
                                    f"Prefetch source failed: {diagnostics['last_error']}"
                                )
                        record = {
                            "id": case["id"],
                            "split": case.get("split", "development"),
                            "query": case["query"],
                            "output": outputs[0],
                            **grade(case, outputs[0], labels),
                        }
                        record.update(
                            first_call_ms=timings[0],
                            warm_p50_ms=statistics.median(timings[1:]),
                            warm_p95_ms=_percentile(timings[1:], 0.95),
                            stable_output=len(set(outputs)) == 1,
                            diagnostics=getattr(provider, "get_prefetch_diagnostics", lambda: {})(),
                        )
                    except Exception as error:
                        record = {
                            "id": case["id"],
                            "split": case.get("split", "development"),
                            "passed": False,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    records.append(record)
        finally:
            if provider is not None:
                provider.shutdown()
            for connection in connections:
                connection.close()
            for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
                local = getattr(sys.modules.get(name), "_thread_local", None)
                if local is not None:
                    local.conn = None
        source_root = provider_root / "hermes_memory_provider"
        source_hashes = {
            path.relative_to(source_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source_root.rglob("*.py"))
        }
        source = (source_root / "__init__.py").read_bytes()
        held_out_cases = fixtures.get("held_out_cases", [])
        held_out_records = [row for row in records if row.get("split") == "held_out"]
    return {
        "schema_version": 2,
        "evaluation_version": fixtures["version"],
        "fixture_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
        "provider_ref": ref or "working-tree",
        "git_commit": subprocess.check_output(["git", "rev-parse", ref or "HEAD"], cwd=ROOT)
        .decode()
        .strip(),
        "engine_version": importlib.metadata.version("mnemosyne-memory"),
        "provider_sha256": base64.urlsafe_b64encode(hashlib.sha256(source).digest())
        .decode()
        .rstrip("="),
        "provider_source_hashes": source_hashes,
        "provider_source_tree_sha256": hashlib.sha256(
            json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "repetitions": repetitions,
        "initialization_ms": initialization_ms,
        "cache_requested": cache,
        "scope": fixtures["description"],
        "limitations": [
            "synthetic exact-marker cases do not measure paraphrase or semantic recall",
            "results are not a production traffic sample",
            "no live gateway, model, or API key is used",
        ],
        "token_measurement": "characters / 4 estimate, not model input tokens",
        "llm_calls": 0,
        "errors": sum("error" in row for row in records),
        "passed": sum(row["passed"] for row in records),
        "total": len(records),
        "held_out_metrics": summarize_held_out(held_out_records, held_out_cases),
        "cases": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider-ref", help="Evaluate provider at this git ref instead of working tree"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".evals/hermes-prefetch/candidate.json"
    )
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument(
        "--cache", action="store_true", help="Explicitly enable experimental exact-query caching"
    )
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error("--repetitions must be at least 2")
    result = run(args.provider_ref, args.repetitions, args.cache)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    rows = []
    for case in result["cases"]:
        outcome = "PASS" if case["passed"] else "FAIL"
        details = html.escape(
            json.dumps({key: value for key, value in case.items() if key != "output"}, indent=2)
        )
        output = html.escape(case.get("output", ""))
        rows.append(
            f"<details><summary>{outcome}: {html.escape(case['id'])}</summary>"
            f"<h3>Metrics and grading</h3><pre>{details}</pre>"
            f"<h3>Actual injected context</h3><pre>{output}</pre></details>"
        )
    args.output.with_suffix(".html").write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Hermes prefetch evaluation</title>"
        "<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 20px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f3f3;padding:16px}"
        "details{border-top:1px solid #ccc;padding:16px 0}summary{cursor:pointer;font-weight:600}</style>"
        f"<h1>Hermes prefetch evaluation: {result['passed']}/{result['total']}</h1>"
        f"<p>{html.escape(result['scope'])}</p>"
        f"<h2>Held-out task metrics</h2><pre>{html.escape(json.dumps(result['held_out_metrics'], indent=2))}</pre>"
        f"<p>{html.escape('; '.join(result['limitations']))}</p><p>No LLM calls. Tokens are a character estimate.</p>"
        + "".join(rows)
        + "</html>\n",
        encoding="utf-8",
    )
    print(
        f"{result['passed']}/{result['total']} cases passed; {result['errors']} execution errors; {args.output}"
    )
    return int(result["errors"] > 0 or (args.require_pass and result["passed"] != result["total"]))


if __name__ == "__main__":
    raise SystemExit(main())
