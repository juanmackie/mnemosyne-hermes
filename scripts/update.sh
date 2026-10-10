#!/usr/bin/env bash
# Check for, and optionally apply, a mnemosyne-hermes update.
#
#   scripts/update.sh [--check]                     tell me if an update exists (default; changes nothing)
#   scripts/update.sh --apply [-- INSTALL_ARGS...]  fast-forward the checkout, re-run ./install.sh --yes, verify
#
# Options:
#   --remote NAME   git remote to read (default: origin)
#   --quiet         print nothing when already up to date (for scheduled, deliver-stdout jobs)
#   --branch NAME   branch to follow (default: main); the checkout must be on it for --apply
#   INSTALL_ARGS    passed to ./install.sh after --yes, e.g. --venv DIR --hermes-home DIR
#
# Exit status:
#   0   up to date, or the update was applied and verified
#   10  --check only: an update is available (the commit list is printed)
#   1   failure; an --apply that fails after the fast-forward is rolled back
#   2   usage error
#
# --apply runs the installer from the fetched commits, so only schedule it for
# a remote you trust. It refuses to run on a dirty tree, never merges (fast-forward
# only), never touches the memory database, and restores the previous commit and
# install when the install or `hermes mnemosyne doctor --no-fix` fails.
# Restart the Hermes gateway after an applied update: Hermes caches the loaded
# provider module for the life of the process.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE=origin
BRANCH=main
MODE=check
QUIET=false
INSTALL_ARGS=()

usage() { sed -n '2,26p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
  --check) MODE=check ;;
  --apply) MODE=apply ;;
  --quiet) QUIET=true ;;
  --remote)
    [[ $# -ge 2 ]] || { echo "ERROR: --remote needs a value" >&2; exit 2; }
    REMOTE="$2"
    shift
    ;;
  --branch)
    [[ $# -ge 2 ]] || { echo "ERROR: --branch needs a value" >&2; exit 2; }
    BRANCH="$2"
    shift
    ;;
  --)
    shift
    INSTALL_ARGS=("$@")
    break
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "ERROR: unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
  esac
  shift
done

cd "$ROOT"
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "ERROR: $ROOT is not a git checkout; clone the repository to use updates" >&2
  exit 1
}

# One run at a time: a scheduler can fire again before a slow install finishes.
LOCK="$(git rev-parse --git-dir)/mnemosyne-update.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "ERROR: another update is running (lock: $LOCK)" >&2
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

git fetch --quiet "$REMOTE" "$BRANCH" || {
  echo "ERROR: could not fetch $REMOTE $BRANCH" >&2
  exit 1
}
HEAD_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse FETCH_HEAD)"

if [[ "$HEAD_SHA" == "$REMOTE_SHA" ]] || git merge-base --is-ancestor "$REMOTE_SHA" "$HEAD_SHA"; then
  [[ "$QUIET" == true ]] || echo "mnemosyne-hermes is up to date at $(git rev-parse --short HEAD)."
  exit 0
fi
if ! git merge-base --is-ancestor "$HEAD_SHA" "$REMOTE_SHA"; then
  echo "ERROR: local history has diverged from $REMOTE/$BRANCH; resolve it by hand" >&2
  exit 1
fi

COUNT="$(git rev-list --count "$HEAD_SHA..$REMOTE_SHA")"
echo "Update available: $COUNT new commit(s), $(git rev-parse --short "$HEAD_SHA") -> $(git rev-parse --short "$REMOTE_SHA")"
git log --oneline --max-count=20 "$HEAD_SHA..$REMOTE_SHA"

if [[ "$MODE" == check ]]; then
  echo "Apply it with: scripts/update.sh --apply"
  exit 10
fi

[[ "$(git rev-parse --abbrev-ref HEAD)" == "$BRANCH" ]] || {
  echo "ERROR: checkout is not on branch $BRANCH; refusing to update" >&2
  exit 1
}
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
  echo "ERROR: tracked files have local changes; refusing to update" >&2
  exit 1
}

# Locate the Hermes CLI the installer targets: the --venv one, else PATH.
HERMES_BIN=""
for ((i = 0; i < ${#INSTALL_ARGS[@]}; i++)); do
  if [[ "${INSTALL_ARGS[i]}" == --venv && $((i + 1)) -lt ${#INSTALL_ARGS[@]} ]]; then
    for candidate in "${INSTALL_ARGS[i + 1]}/bin/hermes" "${INSTALL_ARGS[i + 1]}/Scripts/hermes.exe"; do
      if [[ -x "$candidate" ]]; then HERMES_BIN="$candidate"; fi
    done
  fi
done
[[ -n "$HERMES_BIN" ]] || HERMES_BIN="$(command -v hermes || true)"

install_and_verify() {
  # ${arr[@]+...}: an empty array is an unbound variable under `set -u` on bash < 4.4
  ./install.sh --yes ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"} || return 1
  if [[ -n "$HERMES_BIN" ]]; then
    "$HERMES_BIN" mnemosyne doctor --no-fix || return 1
  else
    echo "NOTE: hermes not found; run 'hermes mnemosyne doctor --no-fix' yourself"
  fi
}

git merge --ff-only --quiet "$REMOTE_SHA"
if install_and_verify; then
  echo "Updated to $(git rev-parse --short HEAD). Restart the Hermes gateway to load it."
  exit 0
fi

echo "ERROR: install or verification failed; restoring $(git rev-parse --short "$HEAD_SHA")" >&2
git reset --hard --quiet "$HEAD_SHA"
install_and_verify >&2 || echo "ERROR: the restored install also failed verification; run ./install.sh by hand" >&2
exit 1
