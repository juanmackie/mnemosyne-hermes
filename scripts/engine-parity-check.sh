#!/usr/bin/env bash
# Verify the installed mnemosyne-memory engine is byte-identical to its wheel.
#
# Run this in the Hermes venv before the provider swap (D6): the review of
# 2026-09-18 suspected local engine patches that the wheel does not ship. On the
# dev host there is exact parity, so this is the check that pins it anywhere else.
#
#   scripts/engine-parity-check.sh                       # autodetect from PATH
#   scripts/engine-parity-check.sh /opt/hermes/.venv     # explicit venv
#
# Exit 0 = full parity, 1 = drift found (report printed), 2 = cannot locate a venv.
set -euo pipefail

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

venv_python() {
    if [[ -x "$1/bin/python" ]]; then printf '%s' "$1/bin/python"
    elif [[ -x "$1/Scripts/python.exe" ]]; then printf '%s' "$1/Scripts/python.exe"
    else printf '%s' ""
    fi
}

VENV="${1:-}"
if [[ -z "$VENV" ]]; then
    for candidate in "${HERMES_VENV:-}" "/opt/hermes/.venv" \
                     "$(dirname "$(dirname "$(command -v hermes 2>/dev/null || true)")" 2>/dev/null || true)"; do
        [[ -n "$candidate" && -n "$(venv_python "$candidate")" ]] && { VENV="$candidate"; break; }
    done
fi
[[ -n "$VENV" ]] || { usage; echo; echo "Error: no venv found; pass one explicitly" >&2; exit 2; }
PY="$(venv_python "$VENV")"
[[ -n "$PY" ]] || { echo "Error: $VENV has no python" >&2; exit 2; }

echo "venv: $VENV"
"$PY" - <<'PYEOF'
import base64, glob, hashlib, pathlib, sys

site = pathlib.Path(glob.glob(str(pathlib.Path(sys.prefix) / "lib" / "**" / "site-packages"), recursive=True)[0]) \
    if glob.glob(str(pathlib.Path(sys.prefix) / "lib" / "**" / "site-packages"), recursive=True) \
    else None
if site is None:
    import sysconfig
    site = pathlib.Path(sysconfig.get_paths()["purelib"])

records = sorted(site.glob("mnemosyne_memory-*.dist-info/RECORD"))
if not records:
    print(f"no mnemosyne_memory dist-info under {site}", file=sys.stderr)
    sys.exit(1)
record = records[0]
version = record.parent.name

matching = modified = missing = 0
drift = []
for line in record.read_text(encoding="utf-8").splitlines():
    parts = line.split(",")
    if len(parts) < 3 or not parts[0].startswith("mnemosyne/") or parts[0].endswith(".pyc"):
        continue
    path, want = parts[0], parts[1]
    p = site / path
    if not p.exists():
        missing += 1; drift.append(f"MISSING  {path}"); continue
    got = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(p.read_bytes()).digest()).decode().rstrip("=")
    if got == want:
        matching += 1
    else:
        modified += 1; drift.append(f"MODIFIED {path}")

shipped = {l.split(",")[0] for l in record.read_text(encoding="utf-8").splitlines() if l.startswith("mnemosyne/")}
on_disk = {str(p.relative_to(site)).replace("\\", "/") for p in (site / "mnemosyne").rglob("*.py")}
extra = sorted(on_disk - shipped)

print(f"engine           : {version}")
print(f"site-packages    : {site}")
print(f"files matching   : {matching}")
print(f"files modified   : {modified}")
print(f"files missing    : {missing}")
print(f"files not shipped: {len(extra)}")
for line in drift[:20]:
    print("   ", line)
for line in extra[:20]:
    print("    EXTRA", line)
if modified or missing or extra:
    print("\nDRIFT: the installed engine differs from its wheel. Reconcile before the swap:")
    print("  - upstream the change, or")
    print("  - reinstall the pinned engine into the venv, or")
    print("  - accept the divergence explicitly and record it (D6).")
    sys.exit(1)
print("\nPARITY: every engine file matches its wheel RECORD.")
PYEOF
