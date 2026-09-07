#!/bin/bash
# check_notes.sh — gate for .mnemosyne_notes: sorted, tab-separated, sha exists.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTES="$ROOT/.mnemosyne_notes"
fail=0
data_count=0

if [ ! -f "$NOTES" ]; then
    printf 'FAIL: %s missing\n' "$NOTES"
    exit 1
fi

line_no=0
while IFS= read -r line; do
    line_no=$((line_no + 1))
    # skip comments / blanks
    case "$line" in
        '#'*|'') continue ;;
    esac
    data_count=$((data_count + 1))
    tabs=$(printf '%s' "$line" | tr -cd '\t' | wc -c)
    if [ "$tabs" -lt 4 ]; then
        printf 'FAIL: %s:%d fewer than 4 tab fields\n' "$NOTES" "$line_no"
        fail=1
        continue
    fi
    key=$(printf '%s' "$line" | cut -f1)
    date=$(printf '%s' "$line" | cut -f2)
    sha=$(printf '%s' "$line" | cut -f4)
    if ! printf '%s' "$date" | grep -EqE '^[0-9]{4}-[0-9]{2}-[0-9]{2}'; then
        printf 'FAIL: %s:%d bad date %s\n' "$NOTES" "$line_no" "$date"
        fail=1
    fi
    if ! git -C "$ROOT" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        printf 'FAIL: %s:%d unknown sha %s\n' "$NOTES" "$line_no" "$sha"
        fail=1
    fi
done < "$NOTES"

if ! LC_ALL=C sort -c "$NOTES" 2>/dev/null; then
    printf 'FAIL: %s is not sorted\n' "$NOTES"
    fail=1
fi

if [ "$fail" = 0 ]; then
    printf 'PASS: %s (%d entries)\n' "$NOTES" "$data_count"
fi
exit $fail
