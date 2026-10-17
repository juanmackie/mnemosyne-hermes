"""Directly runnable tests for the memory-writer evaluator.

Run with ``python3 tests/test_memory_writer_eval.py``. The tests intentionally
do not require pytest so the evaluation gate works in the repository's bare-venv
lane. Pytest can also collect these functions.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bench" / "memory_writer_eval.py"
CASES_PATH = ROOT / "bench" / "memory_writer_cases.json"


def _module():
    spec = importlib.util.spec_from_file_location("memory_writer_eval", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(module):
    return module.load_fixture(CASES_PATH)


def _case_result(report, case_id):
    return next(row for row in report["cases"] if row["case_id"] == case_id)


def _oracle(module, fixture):
    return module.oracle_artifact(fixture)


def _append(artifact, case_id, memory, operation):
    case = next(row for row in artifact["cases"] if row["id"] == case_id)
    index = len(case["memories"])
    case["memories"].append(memory)
    operation = dict(operation)
    operation["memory"] = index
    case["operations"].append(operation)


def test_fixture_contract():
    module = _module()
    fixture = _fixture(module)
    cases = fixture["cases"]
    assert fixture["format"] == "mnemosyne-memory-writer-cases/v1"
    assert len(cases) == 24
    domains = Counter(case["domain"] for case in cases)
    splits = Counter(case["split"] for case in cases)
    assert domains == {"personal": 12, "coding": 12}, domains
    assert splits == {"dev": 16, "held_out": 8}, splits
    assert Counter(
        (case["domain"], case["split"]) for case in cases
    ) == {
        ("personal", "dev"): 8,
        ("personal", "held_out"): 4,
        ("coding", "dev"): 8,
        ("coding", "held_out"): 4,
    }
    assert len({case["id"] for case in cases}) == 24
    for case in cases:
        assert case["split"] in {"dev", "held_out"}
        assert case["domain"] in {"personal", "coding"}
        trace_ids = [message["id"] for message in case["trace"]]
        assert len(trace_ids) == len(set(trace_ids))
        assert case["recall_query"].strip()
        required_ids = [claim["id"] for claim in case["required"]]
        forbidden_ids = [claim["id"] for claim in case["forbidden"]]
        assert len(required_ids) == len(set(required_ids))
        assert len(forbidden_ids) == len(set(forbidden_ids))
        assert not set(required_ids) & set(forbidden_ids)
        seed_ids = [seed["id"] for seed in case["seed_memories"]]
        assert len(seed_ids) == len(set(seed_ids))
        assert set(case["preserve_seeds"]) <= set(seed_ids)
        for claim in case["required"]:
            assert claim["evidence"]
            assert set(claim["evidence"]) <= set(trace_ids)
            assert claim.get("all") or claim.get("any")
            if "op" in claim:
                assert claim["op"]["op"] in {"add", "update", "supersede"}
                if claim["op"]["op"] != "add":
                    assert claim["op"]["target"] in seed_ids
        for claim in case["forbidden"]:
            assert claim.get("all") or claim.get("any")
        for message in case["trace"] + case.get("context", []):
            content = message.get("content", "")
            assert "http://" not in content and "https://" not in content
            for marker in ("sk-", "AKIA", "ghp_", "-----BEGIN"):
                assert marker not in content
    assert (ROOT / "bench" / "memory_writer_cases.json").read_text(
        encoding="utf-8"
    ).count("https://") == 0


def test_oracle_passes_every_case_and_domain():
    module = _module()
    fixture = _fixture(module)
    report = module.score_artifact(fixture, _oracle(module, fixture))
    assert report["artifact"]["valid"] is True, report["artifact"]["errors"]
    metrics = report["metrics"]
    assert metrics["case_count"] == 24
    assert metrics["passed_cases"] == 24, report["cases"]
    assert metrics["failed_cases"] == 0
    assert metrics["required_claim_recall"] == 1.0
    by_domain = report["by_domain"]
    assert by_domain["personal"]["passed_cases"] == 12
    assert by_domain["coding"]["passed_cases"] == 12
    by_split = report["by_split"]
    assert by_split["held_out"]["passed_cases"] == 8


def test_empty_candidate_fails_required_claim():
    module = _module()
    fixture = _fixture(module)
    artifact = _oracle(module, fixture)
    case = next(row for row in artifact["cases"] if row["id"] == "personal-dev-01")
    case["memories"] = []
    case["operations"] = []
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "personal-dev-01")
    assert result["passed"] is False
    assert result["missing_required"] == ["r1"]
    assert report["metrics"]["required_claim_recall"] < 1.0


def test_fabricated_memory_is_unsupported():
    module = _module()
    fixture = _fixture(module)
    artifact = _oracle(module, fixture)
    _append(
        artifact,
        "personal-dev-01",
        {
            "content": "The user owns a yacht.",
            "kind": "fact",
            "subject": "user",
            "subject_name": None,
            "scope": "global",
            "valid_until": None,
            "evidence": ["u1"],
        },
        {"op": "add"},
    )
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "personal-dev-01")
    assert result["passed"] is False
    assert result["unsupported_memories"] >= 1
    assert report["metrics"]["unsupported_memories"] >= 1


def test_wrong_conclusion_is_a_forbidden_violation():
    module = _module()
    fixture = _fixture(module)
    artifact = {"format": module.ARTIFACT_FORMAT, "writer": {"name": "test", "model": "test"}, "cases": []}
    artifact["cases"].append(
        {
            "id": "coding-held-02",
            "memories": [
                {
                    "content": "parser.py empty input tests pass",
                    "kind": "decision",
                    "subject": "repo",
                    "subject_name": None,
                    "scope": "session",
                    "valid_until": None,
                    "evidence": ["a1"],
                }
            ],
            "operations": [{"op": "add", "memory": 0}],
        }
    )
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "coding-held-02")
    assert result["passed"] is False
    assert result["forbidden_violations"] >= 1
    assert "r2" in result["missing_required"]


def test_forced_trivial_experience_fails_no_op_case():
    module = _module()
    fixture = _fixture(module)
    artifact = {"format": module.ARTIFACT_FORMAT, "writer": {"name": "test", "model": "test"}, "cases": []}
    artifact["cases"].append(
        {
            "id": "coding-dev-04",
            "memories": [
                {
                    "content": "renamed file foo.py to bar.py",
                    "kind": "experience",
                    "subject": "repo",
                    "subject_name": None,
                    "scope": "session",
                    "valid_until": None,
                    "evidence": ["u1"],
                }
            ],
            "operations": [{"op": "add", "memory": 0}],
        }
    )
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "coding-dev-04")
    assert result["passed"] is False
    assert any("no-op" in failure for failure in result["failures"])


def test_repeated_event_merge_fails_preserve_and_forbidden():
    module = _module()
    fixture = _fixture(module)
    artifact = {"format": module.ARTIFACT_FORMAT, "writer": {"name": "test", "model": "test"}, "cases": []}
    artifact["cases"].append(
        {
            "id": "personal-dev-07",
            "memories": [
                {
                    "content": "The user ran the Berlin marathon twice, in 2024 and 2025, finishing 3:42.",
                    "kind": "fact",
                    "subject": "user",
                    "subject_name": None,
                    "scope": "global",
                    "valid_until": None,
                    "evidence": ["u1"],
                }
            ],
            "operations": [{"op": "supersede", "target": "s1", "memory": 0}],
        }
    )
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "personal-dev-07")
    assert result["passed"] is False
    assert result["preserved_seed_violations"] >= 1
    assert result["forbidden_violations"] >= 1


def test_cross_person_attribution_fails():
    module = _module()
    fixture = _fixture(module)
    artifact = {"format": module.ARTIFACT_FORMAT, "writer": {"name": "test", "model": "test"}, "cases": []}
    artifact["cases"].append(
        {
            "id": "personal-dev-02",
            "memories": [
                {
                    "content": "The user is vegetarian and allergic to peanuts.",
                    "kind": "fact",
                    "subject": "user",
                    "subject_name": None,
                    "scope": "global",
                    "valid_until": None,
                    "evidence": ["u1"],
                }
            ],
            "operations": [{"op": "add", "memory": 0}],
        }
    )
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "personal-dev-02")
    assert result["passed"] is False
    assert result["forbidden_violations"] >= 2
    assert set(result["missing_required"]) == {"r1", "r2", "r3"}


def test_artifact_validation_rejects_malformed_shapes():
    module = _module()
    fixture = _fixture(module)
    base = _oracle(module, fixture)

    def errors(mutate):
        artifact = copy.deepcopy(base)
        mutate(artifact)
        return module.validate_artifact(artifact, fixture)

    assert errors(lambda item: item.update(format="bad"))
    assert errors(lambda item: item["writer"].update(model=""))
    assert errors(lambda item: item["cases"][1].update(id=item["cases"][0]["id"]))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(kind="guess"))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(subject="nobody"))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(scope="public"))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(valid_until="2026-13-01"))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(evidence=[]))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(content="x" * 5000))
    assert errors(lambda item: item["cases"][0]["memories"][0].update(extra=True))
    assert errors(
        lambda item: item["cases"][0]["operations"].append(
            {"op": "update", "target": "missing", "memory": 0}
        )
    )
    assert errors(
        lambda item: item["cases"][0]["operations"].append(
            {"op": "add", "target": "s1", "memory": 0}
        )
    )
    assert errors(
        lambda item: item["cases"][0]["operations"].append({"op": "add", "memory": 99})
    )


def test_missing_and_unexpected_cases_are_counted():
    module = _module()
    fixture = _fixture(module)
    artifact = _oracle(module, fixture)
    artifact["cases"] = [row for row in artifact["cases"] if row["id"] != "personal-dev-01"]
    extra = copy.deepcopy(artifact["cases"][0])
    extra["id"] = "unknown-case"
    artifact["cases"].append(extra)
    report = module.score_artifact(fixture, artifact)
    assert report["artifact"]["valid"] is True
    assert report["metrics"]["missing_case_ids"] == ["personal-dev-01"]
    assert report["metrics"]["unexpected_case_ids"] == ["unknown-case"]
    assert report["metrics"]["passed_cases"] < 24
    assert report["metrics"]["failed_cases"] >= 1


def test_no_op_case_rejects_any_memory():
    module = _module()
    fixture = _fixture(module)
    artifact = {"format": module.ARTIFACT_FORMAT, "writer": {"name": "test", "model": "test"}, "cases": []}
    artifact["cases"].append(
        {
            "id": "personal-dev-06",
            "memories": [
                {
                    "content": "Hi",
                    "kind": "fact",
                    "subject": "user",
                    "subject_name": None,
                    "scope": "session",
                    "valid_until": None,
                    "evidence": ["u1"],
                }
            ],
            "operations": [{"op": "add", "memory": 0}],
        }
    )
    report = module.score_artifact(fixture, artifact)
    result = _case_result(report, "personal-dev-06")
    assert result["passed"] is False
    assert any("no-op" in failure for failure in result["failures"])


def test_output_refuses_existing_sensitive_or_input_paths():
    module = _module()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / "cases.json"
        source.write_text("{}", encoding="utf-8")
        existing = root / "report.json"
        existing.write_text("{}", encoding="utf-8")
        for candidate in (existing, root / "report.db", root / "report.sqlite3"):
            try:
                module.resolve_output_path(candidate, [source])
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe output accepted: {candidate}")
        try:
            module.resolve_output_path(source, [source])
        except ValueError:
            pass
        else:
            raise AssertionError("input path accepted as output")
        accepted = module.resolve_output_path(root / "new-report.json", [source])
        assert accepted == root / "new-report.json"


def test_cli_end_to_end_and_refusals():
    module = _module()
    fixture = _fixture(module)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        oracle_path = root / "oracle.json"
        oracle_path.write_text(json.dumps(_oracle(module, fixture)), encoding="utf-8")
        report_path = root / "report.json"
        command = [
            sys.executable,
            str(MODULE_PATH),
            "--cases",
            str(CASES_PATH),
            "--candidate",
            str(oracle_path),
            "--output",
            str(report_path),
            "--require-pass",
        ]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["metrics"]["passed_cases"] == 24
        assert report["artifact"]["valid"] is True

        rerun = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        assert rerun.returncode == 2, rerun.stdout + rerun.stderr

        empty_path = root / "empty.json"
        empty_path.write_text(
            json.dumps(
                {
                    "format": module.ARTIFACT_FORMAT,
                    "writer": {"name": "test", "model": "test"},
                    "cases": [],
                }
            ),
            encoding="utf-8",
        )
        failed_path = root / "failed.json"
        failed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--cases",
                str(CASES_PATH),
                "--candidate",
                str(empty_path),
                "--output",
                str(failed_path),
                "--require-pass",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert failed.returncode == 1, failed.stdout + failed.stderr
        assert json.loads(failed_path.read_text(encoding="utf-8"))["metrics"][
            "passed_cases"
        ] == 0


def test_baseline_no_regression_gate():
    module = _module()
    fixture = _fixture(module)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        baseline = _oracle(module, fixture)
        candidate = copy.deepcopy(baseline)
        case = next(row for row in candidate["cases"] if row["id"] == "personal-held-02")
        case["memories"] = [memory for memory in case["memories"] if "78 kg" not in memory["content"]]
        case["operations"] = [
            operation
            for operation in case["operations"]
            if operation["memory"] < len(case["memories"])
        ]
        baseline_path = root / "baseline.json"
        candidate_path = root / "candidate.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        output = root / "comparison.json"
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--cases",
                str(CASES_PATH),
                "--baseline",
                str(baseline_path),
                "--candidate",
                str(candidate_path),
                "--output",
                str(output),
                "--require-no-regression",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        comparison = json.loads(output.read_text(encoding="utf-8"))["comparison"]
        assert comparison["regressions"], comparison


def test_historical_and_negated_mentions_do_not_trip_forbidden_claims():
    module = _module()
    fixture = _fixture(module)
    artifact = {"format": module.ARTIFACT_FORMAT, "writer": {"name": "test", "model": "test"}, "cases": []}
    artifact["cases"].append(
        {
            "id": "personal-dev-03",
            "memories": [
                {
                    "content": "The user previously lived in Berlin and now lives in Lisbon.",
                    "kind": "fact",
                    "subject": "user",
                    "subject_name": None,
                    "scope": "global",
                    "valid_until": None,
                    "evidence": ["u1"],
                }
            ],
            "operations": [{"op": "supersede", "target": "s1", "memory": 0}],
        }
    )
    artifact["cases"].append(
        {
            "id": "coding-dev-02",
            "memories": [
                {
                    "content": "not fixed: permission denied in scripts/migrate.py",
                    "kind": "blocker",
                    "subject": "repo",
                    "subject_name": None,
                    "scope": "session",
                    "valid_until": None,
                    "evidence": ["t1"],
                }
            ],
            "operations": [{"op": "add", "memory": 0}],
        }
    )
    artifact["cases"].append(
        {
            "id": "coding-held-02",
            "memories": [
                {
                    "content": "parser.py handles empty input.",
                    "kind": "decision",
                    "subject": "repo",
                    "subject_name": None,
                    "scope": "session",
                    "valid_until": None,
                    "evidence": ["a1"],
                },
                {
                    "content": "tests not run, not verified.",
                    "kind": "blocker",
                    "subject": "repo",
                    "subject_name": None,
                    "scope": "session",
                    "valid_until": None,
                    "evidence": ["a1"],
                },
            ],
            "operations": [{"op": "add", "memory": 0}, {"op": "add", "memory": 1}],
        }
    )
    report = module.score_artifact(fixture, artifact)
    assert _case_result(report, "personal-dev-03")["passed"] is True
    assert _case_result(report, "coding-dev-02")["passed"] is True
    assert _case_result(report, "coding-held-02")["passed"] is True


def test_replay_without_engine_fails_loudly():
    module = _module()
    if importlib.util.find_spec("mnemosyne") is not None:
        return
    try:
        module._default_provider_factory()
    except RuntimeError as error:
        assert "engine" in str(error).lower()
    else:
        raise AssertionError("missing engine did not fail loudly")


def test_replay_tool_configuration_uses_real_provider_contract():
    module = _module()
    provider_root = ROOT / "integrations" / "hermes-provider"
    if str(provider_root) not in sys.path:
        sys.path.insert(0, str(provider_root))
    import hermes_memory_provider

    provider = hermes_memory_provider.MnemosyneMemoryProvider()
    tools = [
        "mnemosyne_remember",
        "mnemosyne_recall",
        "mnemosyne_update",
        "mnemosyne_get",
        "mnemosyne_stats",
    ]
    module._configure_replay_tools(provider, tools)
    assert provider._configured_tool_names() == set(tools)
    assert provider.has_tool("mnemosyne_update")
    assert provider.has_tool("mnemosyne_get")
    assert not provider.has_tool("mnemosyne_forget")


def test_replay_with_fake_provider_uses_isolated_temp_store():
    module = _module()
    fixture = _fixture(module)
    artifact = _oracle(module, fixture)
    initialized = []

    class FakeProvider:
        def __init__(self):
            self.rows = {}
            self.next_id = 1
            self.calls = []

        def initialize(self, **kwargs):
            self.home = Path(kwargs["hermes_home"])
            self.db_path = Path(kwargs["db_path"])
            assert self.home.is_dir()
            assert self.home == self.db_path.parent
            initialized.append(self)

        def _read_config_key(self, key):
            return None

        def has_tool(self, name):
            return name in (self._read_config_key("tools") or [])

        def handle_tool_call(self, name, args):
            self.calls.append(name)
            if name == "mnemosyne_remember":
                memory_id = f"m{self.next_id}"
                self.next_id += 1
                self.rows[memory_id] = args["content"]
                return json.dumps({"status": "stored", "memory_id": memory_id})
            if name == "mnemosyne_update":
                memory_id = args["memory_id"]
                if memory_id not in self.rows:
                    return json.dumps({"error": "not found"})
                self.rows[memory_id] = args["content"]
                return json.dumps({"status": "updated", "memory_id": memory_id})
            if name == "mnemosyne_get":
                memory_id = args["memory_id"]
                if memory_id not in self.rows:
                    return json.dumps({"status": "not_found", "memory_id": memory_id})
                return json.dumps({"status": "ok", "memory": {"content": self.rows[memory_id]}})
            if name == "mnemosyne_recall":
                return json.dumps(
                    {"results": [{"content": content} for content in self.rows.values()]}
                )
            if name == "mnemosyne_stats":
                return json.dumps({"total": len(self.rows)})
            return json.dumps({"error": f"unknown tool: {name}"})

        def shutdown(self):
            self.calls.append("shutdown")

    replay = module.run_replay(
        fixture, artifact, provider_factory=lambda: FakeProvider()
    )
    assert replay["ok"] is True, replay
    assert replay["engine"] == "injected"
    assert replay["engine_pin"] and replay["engine_pin"].startswith("mnemosyne-memory")
    assert "engine_version" in replay
    assert replay["cases"] == 24
    assert len(initialized) == 24
    assert all(provider.calls.count("shutdown") == 1 for provider in initialized)
    assert any("mnemosyne_remember" in provider.calls for provider in initialized)


def main() -> int:
    tests = [
        (name, function)
        for name, function in sorted(globals().items())
        if name.startswith("test_") and callable(function)
    ]
    failed = 0
    for name, function in tests:
        try:
            function()
        except Exception as error:  # noqa: BLE001 - direct runner reports failures
            failed += 1
            print(f"FAIL {name}: {type(error).__name__}: {error}")
        else:
            print(f"PASS {name}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
