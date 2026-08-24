#!/usr/bin/env bash
# Black-box adoption smoke tests for a built Mnemosyne binary.
# This intentionally uses the public CLI/MCP surfaces only.
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"

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

has_release_smoke() {
  local workflow="$ROOT/.github/workflows/release.yml"
  [[ -f "$workflow" ]] &&
    grep -Eq 'gh release download' "$workflow" &&
    grep -Eq 'sha256sum --check' "$workflow" &&
    grep -Eq 'mnemosyne-linux-x86_64\.tar\.gz' "$workflow" &&
    grep -Eq 'mnemosyne-linux-aarch64\.tar\.gz' "$workflow" &&
    grep -Eq 'mnemosyne-macos-x86_64\.tar\.gz' "$workflow" &&
    grep -Eq 'mnemosyne-macos-aarch64\.tar\.gz' "$workflow"
}

default_features_are_local() {
  grep -Eq '^default = \[\]$' "$ROOT/Cargo.toml"
}

locked_manifest_is_current() {
  cargo metadata --locked --no-deps --format-version 1 >/dev/null 2>&1
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

mcp_provider_surfaces_work() {
  mcp_aliases_work || return 1
  python3 - "$TMP/mcp.jsonl" <<'PY'
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
required = {"mnemosyne_persona", "mnemosyne_canonical", "mnemosyne_triples"}
raise SystemExit(0 if required <= names else 1)
PY
}

mcp_provider_roundtrip_work() {
  local db="$TMP/provider.db"
  local response="$TMP/provider.jsonl"
  local requests
  requests=$'{"jsonrpc":"2.0","method":"initialize","id":1}\n{"jsonrpc":"2.0","method":"tools/call","id":2,"params":{"name":"mnemosyne_canonical","arguments":{"action":"remember","category":"identity","name":"name","body":"The user goes by Ada.","namespace":"global"}}}\n{"jsonrpc":"2.0","method":"tools/call","id":3,"params":{"name":"mnemosyne_triples","arguments":{"action":"add","subject":"Ada","predicate":"uses","object":"Rust","namespace":"global"}}}\n{"jsonrpc":"2.0","method":"tools/call","id":4,"params":{"name":"mnemosyne_triples","arguments":{"action":"query","subject":"Ada","predicate":"uses","namespace":"global"}}}\n'
  MNEMOSYNE_DB_PATH="$db" RUST_LOG=error bash -c "printf '%s' \"\$1\" | \"\$2\" mcp" bash "$requests" "$BIN" >"$response" 2>/dev/null
  python3 - "$response" <<'PY'
import json
import sys

stored = False
triple_count = None
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        payload = json.loads(line)
        text = payload["result"]["content"][0]["text"]
        value = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        continue
    stored = stored or value.get("stored") is True
    if "triples" in value:
        triple_count = value.get("count")
raise SystemExit(0 if stored and triple_count == 1 else 1)
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
  local source="$TMP/python-memory.db"
  local target="$TMP/imported.db"
  local first="$TMP/import-first.json"
  local second="$TMP/import-second.json"
  python3 - "$source" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("""
    CREATE TABLE working_memory (
      id TEXT PRIMARY KEY,
      content TEXT NOT NULL,
      source TEXT,
      timestamp TEXT,
      session_id TEXT,
      importance REAL,
      metadata_json TEXT
    )
""")
conn.execute(
    "INSERT INTO working_memory VALUES (?, ?, ?, ?, ?, ?, ?)",
    ("hermes-memory-1", "The user prefers local-only storage.", "preference",
     "2026-01-02T03:04:05+00:00", "session-1", 0.9, '{"tags":["privacy"]}'),
)
conn.commit()
conn.close()
PY

# The first import writes one deterministic memory; the second is the
# idempotence check and must not create a duplicate.
MNEMOSYNE_DB_PATH="$target" RUST_LOG=error "$BIN" import --from "$source" \
  --namespace agent:hermes --format json >"$first" 2>/dev/null || return 1
MNEMOSYNE_DB_PATH="$target" RUST_LOG=error "$BIN" import --from "$source" \
  --namespace agent:hermes --format json >"$second" 2>/dev/null || return 1
python3 - "$first" "$second" <<'PY'
import json
import sys

first = json.load(open(sys.argv[1], encoding="utf-8"))
second = json.load(open(sys.argv[2], encoding="utf-8"))
raise SystemExit(0 if first["imported"] == 1 and second["imported"] == 0 else 1)
PY
MNEMOSYNE_DB_PATH="$target" RUST_LOG=error "$BIN" list --namespace agent:hermes \
  --format json 2>/dev/null | grep -q 'local-only storage'
}

# Phase 1 gates. The alias surface counts two independent provider names.
check release_workflow has_release_workflow
check release_installer has_release_installer
check release_smoke has_release_smoke
check default_features default_features_are_local
check locked_manifest locked_manifest_is_current
check mcp_command mcp_command_works
if mcp_aliases_work; then
  echo "CHECK mcp_aliases=2"
  pass=$((pass + 2))
else
  echo "CHECK mcp_aliases=0"
  fail=$((fail + 2))
fi
if mcp_provider_surfaces_work; then
  echo "CHECK provider_surfaces=3"
  pass=$((pass + 3))
else
  echo "CHECK provider_surfaces=0"
  fail=$((fail + 3))
fi
if mcp_provider_roundtrip_work; then
  echo "CHECK provider_roundtrip=1"
  pass=$((pass + 1))
else
  echo "CHECK provider_roundtrip=0"
  fail=$((fail + 1))
fi
check importer has_import_command
check offline_core offline_core_works

# The front door is part of adoption: a working binary without an accurate
# install/configure/import/verify guide still makes the project non-adoptable.
if [[ -f "$ROOT/docs/HERMES_INTEGRATION.md" ]] &&
   grep -q 'hermes config set memory.provider' "$ROOT/docs/HERMES_INTEGRATION.md" &&
   grep -q 'mnemosyne_remember' "$ROOT/docs/HERMES_INTEGRATION.md"; then
  echo "CHECK hermes_docs_frontdoor=1"
  pass=$((pass + 1))
else
  echo "CHECK hermes_docs_frontdoor=0"
  fail=$((fail + 1))
fi

# The primary metric is deliberately a small, behavior-oriented score rather
# than a retrieval benchmark. It cannot pass by changing labels or answers.
echo "METRIC hermes_phase1_gates=${pass}"
echo "METRIC hermes_phase1_failures=${fail}"
