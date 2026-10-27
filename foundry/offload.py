#!/usr/bin/env python3
"""Run content-addressed benchmark jobs locally; validate remote job policy."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "bench" / "records" / "foundry-ledger.jsonl"
KAGGLE_BACKENDS = {"kaggle-cpu", "kaggle-gpu", "kaggle-tpu"}
BACKENDS = KAGGLE_BACKENDS | {"z640", "local"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_job(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        job = tomllib.load(source)
    required = {
        "name",
        "version",
        "backend",
        "entry",
        "time_budget_seconds",
        "seed",
        "data_class",
        "inputs",
        "outputs",
    }
    missing = required - job.keys()
    if missing:
        raise ValueError(f"job is missing fields: {sorted(missing)}")
    if not isinstance(job["name"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", job["name"]
    ):
        raise ValueError("job name must be a safe path component")
    if job["backend"] not in BACKENDS:
        raise ValueError(f"unsupported backend: {job['backend']}")
    if job["data_class"] not in {"public", "synthetic", "private"}:
        raise ValueError("data_class must be public, synthetic, or private")
    if job["data_class"] == "private" and job["backend"] in KAGGLE_BACKENDS:
        raise PermissionError("private jobs cannot run on Kaggle")
    if (
        not isinstance(job["entry"], list)
        or not job["entry"]
        or not all(isinstance(arg, str) for arg in job["entry"])
    ):
        raise ValueError("entry must be a non-empty argv array")
    if not isinstance(job["time_budget_seconds"], int) or job["time_budget_seconds"] <= 0:
        raise ValueError("time_budget_seconds must be positive")
    return job


def verify_engine(job: dict[str, Any], root: Path) -> dict[str, str] | None:
    expected_distribution = job.get("engine_distribution")
    if expected_distribution is None:
        return None
    distribution = importlib.metadata.distribution(expected_distribution)
    version = distribution.version
    if version != job.get("engine_version"):
        raise ValueError(
            f"engine version mismatch: expected {job['engine_version']}, got {version}"
        )
    manifest_path = (root / job["engine_patch_manifest"]).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("distribution") != expected_distribution or manifest.get("version") != version:
        raise ValueError("engine patch manifest does not match the pinned distribution")
    package_root = Path(str(distribution.locate_file("mnemosyne"))).resolve(strict=True)
    package_hashes = {}
    for relative, entry in manifest.get("files", {}).items():
        path = (package_root / relative).resolve(strict=True)
        if not path.is_relative_to(package_root) or not path.is_file():
            raise ValueError(f"engine source path is unsafe: {relative}")
        actual = sha256(path)
        if actual != entry["patched_sha256"]:
            raise ValueError(f"engine patch state mismatch: {relative}")
        package_hashes[relative] = actual
    return package_hashes


def verify_inputs(job: dict[str, Any], root: Path) -> dict[str, str]:
    hashes = {}
    for item in job["inputs"]:
        relative = Path(item["path"])
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"input must be a regular project file: {item['path']}")
        actual = sha256(path)
        if actual != item["sha256"]:
            raise ValueError(f"input hash mismatch: {item['path']}")
        hashes[item["path"]] = actual
    return hashes


def verify_outputs(output_dir: Path, declarations: list[dict[str, str]]) -> dict[str, str]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("job did not write manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {entry["path"] for entry in declarations}
    entries = manifest.get("outputs") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict) or set(entries) != declared:
        raise ValueError("output manifest paths do not match the job specification")
    hashes = {}
    for relative, expected in entries.items():
        path = (output_dir / relative).resolve(strict=True)
        if not path.is_relative_to(output_dir.resolve()) or not path.is_file():
            raise ValueError(f"invalid output path: {relative}")
        actual = sha256(path)
        if actual != expected:
            raise ValueError(f"output hash mismatch: {relative}")
        hashes[relative] = actual
    return hashes


def _accelerator() -> str:
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if names:
                return ",".join(names)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import jax

        devices = jax.devices()
        if devices:
            return ",".join(f"{device.platform}:{device.device_kind}" for device in devices)
    except ImportError:
        pass
    return "cpu"


def run_local(job_path: Path, *, root: Path = ROOT, ledger: Path = LEDGER) -> dict[str, Any]:
    job = load_job(job_path)
    if job["backend"] != "local":
        raise ValueError("Phase 0 executes local jobs only; remote execution is not enabled")
    input_hashes = verify_inputs(job, root)
    engine_hashes = verify_engine(job, root)
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    accelerator = _accelerator()
    artifact_dir = ledger.parent / job["name"] / run_id
    with tempfile.TemporaryDirectory(prefix="mnemosyne-foundry-") as scratch:
        output_dir = Path(scratch)
        argv = [arg.replace("{output}", str(output_dir)) for arg in job["entry"]]
        if argv[0] == "python":
            argv[0] = sys.executable
        env = os.environ.copy()
        env["MNEMOSYNE_JOB_SEED"] = str(job["seed"])
        try:
            subprocess.run(argv, cwd=root, env=env, check=True, timeout=job["time_budget_seconds"])
            output_hashes = verify_outputs(output_dir, job["outputs"])
            artifact_dir.mkdir(parents=True, exist_ok=False)
            for relative in output_hashes:
                target = artifact_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(output_dir / relative, target)
            shutil.copy2(output_dir / "manifest.json", artifact_dir / "manifest.json")
            output_hashes = verify_outputs(artifact_dir, job["outputs"])
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"job exceeded {job['time_budget_seconds']} seconds") from exc
        wall_seconds = time.monotonic() - started
        record = {
            "format": "mnemosyne-foundry-ledger/v1",
            "run_id": run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "job": job["name"],
            "backend": job["backend"],
            "input_hashes": input_hashes,
            "engine_source_hashes": engine_hashes,
            "accelerator": accelerator,
            "wall_seconds": round(wall_seconds, 6),
            "artifact_dir": str(artifact_dir.relative_to(ledger.parent)),
            "quota_used": {"runtime_seconds": round(wall_seconds, 6)},
            "output_hashes": output_hashes,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path, help="path to job.toml")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    try:
        job = load_job(args.job)
        if job["backend"] in KAGGLE_BACKENDS and job["data_class"] == "private":
            raise PermissionError("private jobs cannot run on Kaggle")
        if job["backend"] != "local":
            raise RuntimeError("remote Kaggle/Z640 dispatch is not enabled in Phase 0")
        print(
            json.dumps(
                run_local(args.job, root=args.root.resolve(), ledger=args.ledger.resolve()),
                indent=2,
            )
        )
        return 0
    except (
        OSError,
        ValueError,
        PermissionError,
        RuntimeError,
        TimeoutError,
        subprocess.CalledProcessError,
        importlib.metadata.PackageNotFoundError,
    ) as error:
        print(f"foundry: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
