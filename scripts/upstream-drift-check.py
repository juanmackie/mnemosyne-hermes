#!/usr/bin/env python3
"""Check PyPI for upstream releases this repo cares about, and report drift.

Two upstreams matter here:

* ``mnemosyne-memory`` — the engine, and the source of the vendored
  ``hermes_memory_provider`` snapshot. A new release means the snapshot should
  be re-checked with ``scripts/vendor-provider-sync.sh``.
* ``hermes-agent`` — Hermes itself. A release outside the supported range
  (``>=0.18,<0.20``) means the range contract needs re-auditing, and a release
  inside it means the smoke lane should be re-run against it.

The script is stdlib-only, so it runs anywhere Python does. It downloads the
new wheel directly from the PyPI JSON API rather than shelling out to pip.

    python scripts/upstream-drift-check.py
    python scripts/upstream-drift-check.py --json      # machine-readable

Exit status:
    0  nothing to do: both upstreams are at the versions this repo expects
    1  something to look at (the report says what)
    2  the check itself could not run (network, malformed response)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDORED = ROOT / "integrations" / "hermes-provider" / "hermes_memory_provider"
MANIFEST = ROOT / "integrations" / "hermes-provider" / "VENDORED_FROM.json"
SYNC_SCRIPT = ROOT / "scripts" / "vendor-provider-sync.sh"
SUPPORTED_HERMES_RANGE = ">=0.18,<0.20"
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_PROVIDER_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_FILES = 512

PYPI = "https://pypi.org/pypi/{name}/json"


def latest_release(name: str) -> dict:
    """Latest version and its wheel URL, from the PyPI JSON API."""
    try:
        with urllib.request.urlopen(PYPI.format(name=name), timeout=30) as response:
            payload = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"could not read {name} from PyPI: {e}") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("info"), dict):
        raise RuntimeError(f"{name} returned an invalid PyPI response")
    version = payload["info"].get("version")
    urls = payload.get("urls")
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]{0,127}", version) is None
        or not isinstance(urls, list)
    ):
        raise RuntimeError(f"{name} returned an invalid PyPI version or file list")
    wheels = [
        entry
        for entry in urls
        if isinstance(entry, dict)
        and entry.get("packagetype") == "bdist_wheel"
        and isinstance(entry.get("url"), str)
        and isinstance(entry.get("filename"), str)
    ]
    if not wheels:
        raise RuntimeError(f"{name} {version} publishes no wheel")
    # Prefer a pure-Python wheel; the provider package is not compiled.
    wheels.sort(key=lambda entry: "py3-none-any" not in entry["filename"])
    return {"name": name, "version": version, "wheel": wheels[0]["url"]}


def vendored_engine_version() -> str:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return manifest["source"]["version"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"could not read {MANIFEST}: {e}") from e


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts) or (0,)


def hermes_range_status(version: str) -> tuple[bool, str]:
    """(in range, message) for a hermes-agent version, against the contract."""
    numbers = _version_tuple(version)
    low, high = (0, 18), (0, 20)
    if numbers[:2] < low:
        return False, f"{version} is below the supported range {SUPPORTED_HERMES_RANGE}"
    if numbers[:2] >= high:
        return False, (
            f"{version} is at or above the upper bound of {SUPPORTED_HERMES_RANGE}. "
            "The bound is a contract: re-run the contract audit before widening it."
        )
    return True, f"{version} is inside the supported range"


def download_wheel(url: str, into: pathlib.Path) -> pathlib.Path:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise RuntimeError("PyPI returned an invalid wheel URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or parsed.port is not None
        or not parsed.path.endswith(".whl")
    ):
        raise RuntimeError("untrusted wheel URL; expected an HTTPS PyPI file URL")
    filename = PurePosixPath(parsed.path).name
    if filename in {"", ".", ".."}:
        raise RuntimeError("PyPI wheel URL has no safe filename")

    wheel = into / filename
    created = False
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            try:
                final_url = urlsplit(response.geturl())
            except ValueError as exc:
                raise RuntimeError("PyPI returned an invalid redirect URL") from exc
            if final_url.scheme != "https" or final_url.hostname != "files.pythonhosted.org":
                raise RuntimeError("PyPI wheel download redirected outside files.pythonhosted.org")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("PyPI returned an invalid wheel size") from exc
                if declared_size < 0 or declared_size > MAX_WHEEL_BYTES:
                    raise RuntimeError("wheel exceeds the size limit")
            downloaded = 0
            with wheel.open("xb") as output:
                created = True
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_WHEEL_BYTES:
                        raise RuntimeError("wheel exceeds the size limit")
                    output.write(chunk)
    except Exception:
        if created:
            wheel.unlink(missing_ok=True)
        raise
    return wheel


def extract_provider(wheel: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    """Safely extract bounded Python sources from hermes_memory_provider/."""
    members = []
    total_size = 0
    seen = set()
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name.startswith("hermes_memory_provider/") or not name.endswith(".py"):
                continue
            parts = name.split("/")
            if (
                "\\" in name
                or any(part in {"", ".", ".."} or ":" in part for part in parts)
                or parts[0] != "hermes_memory_provider"
            ):
                raise RuntimeError(f"unsafe provider path in wheel: {name!r}")
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG):
                raise RuntimeError(f"provider wheel contains a non-regular file: {name!r}")
            if name in seen:
                raise RuntimeError(f"provider wheel contains a duplicate path: {name!r}")
            seen.add(name)
            if info.file_size < 0:
                raise RuntimeError(f"provider wheel contains an invalid file size: {name!r}")
            total_size += info.file_size
            if total_size > MAX_PROVIDER_BYTES:
                raise RuntimeError("provider source exceeds the size limit")
            members.append((info, parts))
            if len(members) > MAX_PROVIDER_FILES:
                raise RuntimeError("provider source exceeds the file-count limit")

        if not members:
            raise RuntimeError(f"{wheel.name} carries no hermes_memory_provider/*.py")

        package = into / "hermes_memory_provider"
        if package.exists() or package.is_symlink():
            raise RuntimeError(f"provider extraction destination already exists: {package}")
        package.mkdir(parents=True)
        extracted = 0
        for info, parts in members:
            target = into.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                while chunk := source.read(64 * 1024):
                    extracted += len(chunk)
                    if extracted > MAX_PROVIDER_BYTES:
                        raise RuntimeError("provider source exceeds the size limit")
                    output.write(chunk)
    return into / "hermes_memory_provider"


def provider_drift(wheel_url: str) -> tuple[bool, str]:
    """(drift found, report) for a newer engine wheel."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        package = extract_provider(download_wheel(wheel_url, tmp_path), tmp_path)
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(package)],
            capture_output=True,
            text=True,
            check=False,
        )
        report = (result.stdout + result.stderr).strip()
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"vendor sync failed with exit {result.returncode}: {report or 'no output'}"
            )
        return result.returncode == 1, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check PyPI for upstream releases and report vendored drift"
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument(
        "--latest-hermes",
        action="store_true",
        help="print only the newest hermes-agent version and exit",
    )
    args = parser.parse_args(argv)

    if args.latest_hermes:
        # Used by .github/workflows/upstream-drift.yml, which runs the smoke
        # lane against whatever is newest on PyPI.
        try:
            print(latest_release("hermes-agent")["version"])
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        return 0

    try:
        engine = latest_release("mnemosyne-memory")
        hermes = latest_release("hermes-agent")
        pinned = vendored_engine_version()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    actions: list[str] = []
    engine_report: dict[str, Any] = {"latest": engine["version"], "vendored": pinned}
    hermes_report: dict[str, Any] = {
        "latest": hermes["version"],
        "supported_range": SUPPORTED_HERMES_RANGE,
    }
    report: dict[str, Any] = {"engine": engine_report, "hermes": hermes_report}

    engine_newer = _version_tuple(engine["version"]) > _version_tuple(pinned)
    if engine_newer:
        actions.append(
            f"mnemosyne-memory {engine['version']} is newer than the vendored "
            f"{pinned}. Re-vendor per PATCHES.md."
        )
        try:
            drift, drift_report = provider_drift(engine["wheel"])
        except (OSError, RuntimeError, ValueError, TypeError, zipfile.BadZipFile) as e:
            actions.append(f"could not compare the new wheel: {e}")
        else:
            engine_report["drift_report"] = drift_report
            if drift:
                actions.append("the new wheel differs from the vendored snapshot")
            else:
                actions.append("the new wheel matches the vendored snapshot")
    else:
        engine_report["status"] = "current"

    in_range, message = hermes_range_status(hermes["version"])
    hermes_report["message"] = message
    if not in_range:
        actions.append(f"hermes-agent {message}")

    if args.json:
        report["actions"] = actions
        print(json.dumps(report, indent=2))
    else:
        print(f"mnemosyne-memory: vendored {pinned}, latest {engine['version']}")
        print(f"hermes-agent:     latest {hermes['version']} ({message})")
        if "drift_report" in engine_report:
            print()
            print(engine_report["drift_report"])
        print()
        if actions:
            print("ACTION:")
            for action in actions:
                print(f"  - {action}")
        else:
            print("Nothing to do.")

    return 1 if actions else 0


if __name__ == "__main__":
    sys.exit(main())
