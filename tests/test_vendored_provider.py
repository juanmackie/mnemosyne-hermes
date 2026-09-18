"""Drift gate for the vendored Hermes provider snapshot.

`integrations/hermes-provider/hermes_memory_provider/` is a byte-for-byte
snapshot of the provider shipped by `mnemosyne-memory 3.15.1`. Any edit to it is
a deliberate, reviewable act: the edit must be declared as a local patch (a
`# LOCAL PATCH:` marker plus a PATCHES.md entry) or accompanied by a
`VENDORED_FROM.json` update in the same change.

Needs no engine, no Hermes and no network — it only reads files, so it runs in a
bare venv (which is exactly where the provider is otherwise hollow).

    python tests/test_vendored_provider.py
    pytest tests/test_vendored_provider.py
"""
import base64
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "integrations" / "hermes-provider"
PACKAGE = VENDOR / "hermes_memory_provider"
PATCH_MARKER = "# LOCAL PATCH:"
MANIFEST = json.loads((VENDOR / "VENDORED_FROM.json").read_text(encoding="utf-8"))


def _digest(path: pathlib.Path) -> str:
    """sha256 as base64url without padding — the wheel RECORD format."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(path.read_bytes()).digest()
    ).decode().rstrip("=")


def test_no_unmanifested_provider_files():
    on_disk = {f"hermes_memory_provider/{p.name}" for p in PACKAGE.glob("*.py")}
    assert on_disk == set(MANIFEST["files"]), (
        "the vendored file set changed without updating VENDORED_FROM.json "
        f"(on disk: {sorted(on_disk)}, manifest: {sorted(MANIFEST['files'])})"
    )


def test_vendored_files_match_manifest_hashes():
    for rel, entry in MANIFEST["files"].items():
        path = VENDOR / rel
        assert path.exists(), f"missing vendored file: {rel}"
        assert _digest(path) == entry["sha256"], (
            f"{rel} drifted from the vendored snapshot. If this is an intentional "
            "fix, mark the site '# LOCAL PATCH:', list it in PATCHES.md and update "
            "VENDORED_FROM.json; otherwise re-sync from the upstream wheel."
        )


def test_local_patches_are_declared():
    patched = {
        rel for rel in MANIFEST["files"]
        if PATCH_MARKER in (VENDOR / rel).read_text(encoding="utf-8")
    }
    declared = set(MANIFEST.get("local_patches", {}))
    assert patched == declared, (
        f"undeclared local patches: {sorted(patched - declared)}; "
        f"stale declarations: {sorted(declared - patched)}"
    )
    patches_md = (VENDOR / "PATCHES.md").read_text(encoding="utf-8")
    for rel in sorted(patched):
        assert pathlib.Path(rel).name in patches_md, (
            f"{rel} carries a local patch but has no entry in PATCHES.md"
        )


if __name__ == "__main__":
    tests = [test_no_unmanifested_provider_files,
             test_vendored_files_match_manifest_hashes,
             test_local_patches_are_declared]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
