from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_power_plan_rejects_underpowered_tiny_fixture_and_sizes_effects():
    power = _load("power", ROOT / "bench/mnemobench/power.py")
    assert power.paired_mde(27) > 0.5
    assert power.sample_size(0.05) > power.sample_size(0.10)
    assert power.planning_record(300)["minimum_effect_sample_sizes"]["0.1"] == power.sample_size(
        0.1
    )


def test_capture_guard_detects_and_redacts_synthetic_secret_without_touching_neighbors():
    guard = _load("capture_guard", ROOT / "bench/mnemobench/capture_guard.py")
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2"
    original = f"safe before {secret} safe after"
    redacted, count = guard.redact(original)
    assert count == 1
    assert secret not in redacted
    assert redacted == "safe before [REDACTED] safe after"
    assert guard.redact("ordinary meeting notes")[1] == 0


def test_mnemosim_labels_are_deterministic_and_temporal_truth_is_exact():
    simulator = _load("mnemosim_generate", ROOT / "bench/sim/generate.py")
    life = simulator.make_life(7)
    assert life == simulator.make_life(7)
    assert {event["domain"] for event in life["events"]} == {"personal", "coding", "homelab"}
    by_id = {event["event_id"]: event for event in life["events"]}
    assert by_id["personal-coffee-v1"]["valid_to"] == by_id["personal-coffee-v2"]["valid_from"]
    assert by_id["injection-tool-output"]["disposition"] == "never_store"
    assert (
        next(probe for probe in life["probes"] if probe["probe_id"] == "never-told")["answers"]
        == []
    )


def test_foundry_refuses_private_kaggle_jobs_and_detects_bad_output_hashes(tmp_path):
    foundry = _load("foundry_offload", ROOT / "foundry/offload.py")
    import pytest

    private_job = tmp_path / "private.toml"
    for backend in ("kaggle-cpu", "kaggle-gpu", "kaggle-tpu"):
        private_job.write_text(
            f'name="private"\nversion=1\nbackend="{backend}"\nentry=["true"]\n'
            'time_budget_seconds=10\nseed=1\ndata_class="private"\ninputs=[]\noutputs=[]\n',
            encoding="utf-8",
        )
        with pytest.raises(PermissionError, match="private jobs cannot run on Kaggle"):
            foundry.load_job(private_job)

    output = tmp_path / "output"
    output.mkdir()
    (output / "result.json").write_text("result", encoding="utf-8")
    (output / "manifest.json").write_text('{"outputs":{"result.json":"bad"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="output hash mismatch"):
        foundry.verify_outputs(output, [{"path": "result.json"}])
