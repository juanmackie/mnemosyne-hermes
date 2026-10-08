#!/usr/bin/env python3
"""Apply or restore the audited engine source patches without importing the engine."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import tempfile

PATCH_ROOT = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "engine-patches"
HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
BACKUP_SUFFIX = ".mnemosyne-hermes-original"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_diff(original: bytes, delta: bytes) -> bytes:
    """Apply an exact unified diff: no fuzz, offset guessing, or shell dependency."""
    source = original.decode("utf-8").splitlines(keepends=True)
    lines = delta.decode("utf-8").splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    for line in lines[2:]:
        match = HUNK.match(line)
        if match:
            start = int(match[1]) - 1
            if start < cursor:
                raise ValueError("overlapping patch hunks")
            result.extend(source[cursor:start])
            cursor = start
        elif line.startswith((" ", "-")):
            if cursor >= len(source) or source[cursor] != line[1:]:
                raise ValueError("patch context differs from audited source")
            if line[0] == " ":
                result.append(source[cursor])
            cursor += 1
        elif line.startswith("+"):
            result.append(line[1:])
        else:
            raise ValueError("unsupported patch line")
    result.extend(source[cursor:])
    return "".join(result).encode("utf-8")


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    mode = path.stat().st_mode
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    staged = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        staged.chmod(mode)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def apply_patches(
    engine_root: pathlib.Path, *, restore: bool = False, dry_run: bool = False
) -> list[str]:
    """Preflight every source and backup before changing any source file."""
    manifest = json.loads((PATCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    engine_root = engine_root.resolve(strict=True)
    pending = []
    report = []
    for relative, entry in manifest["files"].items():
        path = engine_root / relative
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(engine_root):
            raise ValueError(f"refusing engine source outside package: {relative}")
        current = path.read_bytes()
        current_hash = digest(current)
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if backup.is_symlink() or (
            backup.exists() and digest(backup.read_bytes()) != entry["original_sha256"]
        ):
            raise ValueError(f"backup differs from audited source: {relative}")
        previous_hashes = entry.get("previous_patched_sha256", [])
        if current_hash not in (
            entry["original_sha256"],
            entry["patched_sha256"],
            *previous_hashes,
        ):
            raise ValueError(
                f"engine source differs from audited {manifest['version']}: {relative}"
            )
        if restore:
            if current_hash == entry["original_sha256"]:
                report.append(f"original: {relative}")
                continue
            if not backup.exists():
                raise ValueError(f"cannot restore without verified backup: {relative}")
            replacement = backup.read_bytes()
        else:
            delta = (PATCH_ROOT / entry["patch"]).read_bytes()
            if digest(delta) != entry["patch_sha256"]:
                raise ValueError(f"patch digest mismatch: {relative}")
            if current_hash == entry["patched_sha256"]:
                report.append(f"already patched: {relative}")
                continue
            # Upgrade only a declared previous patch with its verified original.
            # Never treat arbitrary local edits as a base for an updated diff.
            original = current
            if current_hash in previous_hashes:
                if not backup.exists():
                    raise ValueError(f"cannot upgrade without verified original backup: {relative}")
                original = backup.read_bytes()
            replacement = apply_diff(original, delta)
            if digest(replacement) != entry["patched_sha256"]:
                raise ValueError(f"patched source digest mismatch: {relative}")
            compile(replacement, str(path), "exec")
        pending.append((path, backup, current, replacement))
        report.append(f"{'restore' if restore else 'patch'}: {relative}")
    if dry_run:
        return report

    # Backups are retained for verified rollback, including after restore.
    for _, backup, current, _ in pending:
        if not restore and not backup.exists():
            with backup.open("xb") as output:
                output.write(current)
    changed = []
    try:
        for path, _, current, replacement in pending:
            atomic_write(path, replacement)
            changed.append((path, current))
    except Exception:
        for path, current in reversed(changed):
            atomic_write(path, current)
        raise
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine-root", type=pathlib.Path, help="explicit mnemosyne package directory"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        root = args.engine_root
        if root is None:
            dist = importlib.metadata.distribution("mnemosyne-memory")
            root = pathlib.Path(dist.locate_file("mnemosyne"))
        print(f"engine package: {root.resolve()}")
        for line in apply_patches(root, restore=args.restore, dry_run=args.dry_run):
            print(line)
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
