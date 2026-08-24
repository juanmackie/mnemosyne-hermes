#!/usr/bin/env bash
# Black-box adoption smoke tests for a built Mnemosyne binary.
# This intentionally uses the public CLI/MCP surfaces only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${MNEMOSYNE_BIN:-${ROOT}/target/release/mnemosyne}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ ! -x "$BIN" ]]; then
  echo "binary not found or not executable: $BIN" >&2
  exit 2
fi

pass=0
fail=0
check() {
  local name="$1"
  shift
  if "$@"; then
    echo "CHECK ${name}=1"
    pass=$((pass + 1))
  else
    echo "CHECK ${name}=0"
    fail=$((fail + 1))
  fi
}

has_release_workflow() {
  [[ -f "$ROOT/.github/workflows/release.yml" ]] &&
    grep -Eq 'linux.*(x86_64|amd64)|x86_64.*linux' "$ROOT/.github/workflows/release.yml" &&
    grep -Eq 'aarch64|arm64' "$ROOT/.github/workflows/release.yml" &&
    grep -Eq 'darwin|macos' "$ROOT/.github/workflows/release.yml"
}

has_release_installer() {
  [[ -f "$ROOT/install.sh" ]] &&
    grep -Eq 'MNEMOSYNE_VERSION|--version' "$ROOT/install.sh" &&
    grep -Eq 'sha256|checksum|SHA256' "$ROOT/install.sh" &&
    grep -Eq 'github.com/.*/releases|releases/download' "$ROOT/install.sh"
}

mcp_command_works() {
  "$BIN" mcp --help >/dev/null 2>&1
}

mcp_aliases_work() {
  local db="$TMP/mcp.db"
  local response="$TMP/mcp.jsonl"
  local requests
  requests=$'{"jsonrpc":"2.0","method":"initialize","id":1}\n{"jsonrpc":"2.0","method":"tools/list","id":2}\n'
  MNEMOSYNE_DB_PATH="$db" RUST_LOG=error bash -c "printf '%s' \"\$1\" | \"\$2\" mcp" bash "$requests" "$BIN" >"$response" 2>/dev/null
  python3 - "$response" <<'PY'
import json
import sys

names = set()
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    for tool in payload.get("result", {}).get("tools", []):
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            names.add(tool["name"])
required = {"mnemosyne_remember", "mnemosyne_recall"}
raise SystemExit(0 if required <= names else 1)
PY
}

offline_core_works() {
  local db="$TMP/offline.db"
  local output="$TMP/offline.json"
  env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY MNEMOSYNE_DB_PATH="$db" \
    RUST_LOG=error "$BIN" remember --content "Hermes prefers local first memory" \
    --namespace agent:hermes --no-enrich --format json >"$output" 2>/dev/null
  grep -q 'Hermes prefers local first memory' "$output" || return 1
  env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY MNEMOSYNE_DB_PATH="$db" \
    RUST_LOG=error "$BIN" list --namespace agent:hermes --format json >/dev/null 2>&1
}

has_import_command() {
  "$BIN" import --help >/dev/null 2>&1
}

# Phase 1 gates. The alias surface counts two independent provider names.
check release_workflow has_release_workflow
check release_installer has_release_installer
check mcp_command mcp_command_works
if mcp_aliases_work; then
  echo "CHECK mcp_aliases=2"
  pass=$((pass + 2))
else
  echo "CHECK mcp_aliases=0"
  fail=$((fail + 2))
fi
check importer has_import_command
check offline_core offline_core_works

# Non-gating audience signal, useful while the docs are being rewritten.
if [[ -f "$ROOT/docs/HERMES_INTEGRATION.md" ]] &&
   grep -q 'hermes config set memory.provider' "$ROOT/docs/HERMES_INTEGRATION.md" &&
   grep -q 'mnemosyne_remember' "$ROOT/docs/HERMES_INTEGRATION.md"; then
  echo "CHECK hermes_docs_frontdoor=1"
else
  echo "CHECK hermes_docs_frontdoor=0"
fi

# The primary metric is deliberately a small, behavior-oriented score rather
# than a retrieval benchmark. It cannot pass by changing labels or answers.
echo "METRIC hermes_phase1_gates=${pass}"
echo "METRIC hermes_phase1_failures=${fail}"
