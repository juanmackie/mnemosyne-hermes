#!/usr/bin/env bash
# Diff the vendored Hermes provider against an upstream copy/wheel extraction.
#
# Usage:
#   scripts/vendor-provider-sync.sh /path/to/site-packages/hermes_memory_provider
#   scripts/vendor-provider-sync.sh /path/to/extracted/wheel
#
# Exit status:
#   0  vendored snapshot matches upstream byte-for-byte
#   1  drift found (per-file report printed) — triage via PATCHES.md
#   2  usage/target error
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDORED="$ROOT/integrations/hermes-provider/hermes_memory_provider"

if [[ $# -ne 1 ]]; then
    sed -n '2,10p' "$0"
    exit 2
fi

TARGET="$1"
# Accept either the package dir itself or a tree containing it.
if [[ -d "$TARGET/hermes_memory_provider" && ! -f "$TARGET/__init__.py" ]]; then
    TARGET="$TARGET/hermes_memory_provider"
fi
if [[ ! -f "$TARGET/__init__.py" ]]; then
    printf 'Error: %s does not look like the hermes_memory_provider package\n' "$1" >&2
    exit 2
fi

drift=0
printf 'vendored: %s\nupstream: %s\n\n' "$VENDORED" "$TARGET"

for f in "$VENDORED"/*.py; do
    name="$(basename "$f")"
    if [[ ! -f "$TARGET/$name" ]]; then
        printf '  REMOVED UPSTREAM  %s\n' "$name"
        drift=1
        continue
    fi
    if cmp -s "$f" "$TARGET/$name"; then
        printf '  same              %s\n' "$name"
    else
        printf '  DIFFERS           %s  (%s lines differ)\n' \
            "$name" "$(diff "$f" "$TARGET/$name" | grep -c '^[<>]' || true)"
        drift=1
    fi
done

for f in "$TARGET"/*.py; do
    name="$(basename "$f")"
    [[ -f "$VENDORED/$name" ]] || { printf '  NEW UPSTREAM      %s\n' "$name"; drift=1; }
done

if [[ "$drift" -eq 0 ]]; then
    printf '\nNo drift: the snapshot matches upstream.\n'
    exit 0
fi

cat <<'EOF'

Drift found. For each line above:
  - upstream-only change   -> re-vendor the file and update VENDORED_FROM.json
  - local patch still needed -> re-apply it with '# LOCAL PATCH:' + a PATCHES.md entry
  - new local need          -> add the marker and the entry
Then run: python tests/test_vendored_provider.py
EOF
exit 1
