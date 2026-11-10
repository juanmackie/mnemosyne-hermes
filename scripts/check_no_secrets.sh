#!/usr/bin/env bash
# Fail when a secret-bearing file is tracked. `.env` with a Kaggle API token sat
# on public main since 7847316; .gitignore alone does not stop `git add -f`, an
# editor, or a fresh clone's tooling from re-adding one. This gate is the
# regression test for that incident.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail=0

# 1. No tracked .env file (examples are allowed: .env.example carries no value).
while IFS= read -r path; do
  case "$path" in
  *.env.example | *.env.sample) continue ;;
  esac
  echo "TRACKED SECRET FILE: $path" >&2
  fail=1
done < <(git ls-files | grep -E '(^|/)\.env($|\.)' || true)

# 2. A .gitignore rule must exist, so an untracked .env stays untracked.
if ! grep -qE '^\.env$' .gitignore && ! grep -qE '^\.env\*?$' .gitignore; then
  echo "MISSING .gitignore RULE: .env*" >&2
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "no secret files tracked; .gitignore carries the .env rule"
