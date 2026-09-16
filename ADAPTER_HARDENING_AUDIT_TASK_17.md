# Task 17 — Adapter Hardening Audit (Boundary Checks, MCP_ARGS, Stdio Transport, Execution Context Gating)

**Contract**: Relative path containment; configurable `MCP_ARGS`; thread-based stdio reader + queue; execution context gating; from commits `407a9b1`, `22a5136`, `32c88ba` (archive `feat/hermes-native-provider`).
**Evidence**: Adapter (`src/lib/storage.py`, `mnemosyne_client.py`), docs/archive (`RUST_ARCHIVE_REF.md`), `.github/workflows/`, `docs/AGENT_SETUP.md`, `docs/MCP_CLIENT_CONFIGS.md` (reference), user spec.

---

## 1. Archive Reference (Commits `407a9b1`, `22a5136`, `32c88ba`)

`docs/archive/RUST_ARCHIVE_REF.md`: Previous Rust implementation preserved at `feat/hermes-native-provider` (`09a6973` / previous stable `main` `ba6fe984`). The archive reference mentions adapter contracts (`memory.provider`, namespace `agent:hermes`, DB path `MNEMOSYNE_DB_PATH`, tool names `mnemosyne_memory_search` / `mnemosyne_memory_remember`).

- **Commit hashes `407a9b1`, `22a5136`, `32c88ba`**: NOT present in current repository (`git rev-parse` fails; archive reference uses different SHAs: `09a6973` / `ba6fe984`). The user's contract references these as the origin commits for adapter hardening; the actual archive uses different commit IDs. This discrepancy is preserved — the user's contract references the intended origin (adapter hardening commits), which are either from an unretrieved upstream branch or are design references.
- **Archive evidence**: `docs/archive/RUST_ARCHIVE_REF.md` (line 11-12) references the previous adapter contracts (provider.name `mnemosyne-python`, namespace `agent:hermes`, DB path, tool names). No `407a9b1` / `22a5136` / `32c88ba` references found.

---

## 2. Adapter Current State (`src/lib/storage.py` / `mnemosyne_client.py`)

### Basic Path Validation (Partial)
- `PythonMemoryStorage.__init__()` (`line 88+`): requires `db_path` (non-empty); creates directory (`_ensure_db_dir()`); raises `ValueError` / `sqlite3.Error`. Basic boundary check exists but is minimal.

### Relative Path Containment (MISSING)
- No `os.path.abspath()` / `os.path.realpath()` containment check against a configured base directory.
- `resolve_db_path()` (`mnemosyne_client.py`) resolves `DATABASE_URL` but does not enforce relative path containment (e.g., `../../etc/passwd` or `~/.local/share/` escape checks).
- No `os.path.commonpath()` or `pathlib.Path.resolve()` validation against a trusted base.
- **Contract gap**: Adapter accepts any absolute or relative `db_path`; no containment boundary enforced.

### Configurable `MCP_ARGS` (MISSING)
- `mnemosyne_client.py`: `__init__()` takes `db_path` and optional `storage`. No `MCP_ARGS` parameter.
- `.github/workflows/` / `docs/AGENT_SETUP.md`: `MNEMOSYNE_DB_PATH` is environment variable (not configurable `MCP_ARGS`). No `args` customization for MCP stdio transport.
- `docs/MCP_CLIENT_CONFIGS.md`: not present (file missing).
- **Contract gap**: Adapter/client has no configurable `MCP_ARGS` (transport args, env injection, argument customization).

### Thread-Based Stdio Reader + Queue (MISSING)
- `PythonMemoryStorage`: uses `sqlite3` with thread-local connections (`_local` thread-local). No `threading.Thread` stdio reader.
- `mnemosyne_client.py`: async `remember()` / `recall()` call `self.storage.remember()` / `self.storage.recall()` synchronously (no background stdio reader thread, no `queue.Queue`, no `threading.Event` for stdio signal handling).
- `docs/HERMES_INTEGRATION.md`: references `mnemosyne mcp` stdio server surface but adapter has no stdio transport layer.
- **Contract gap**: No `threading.Thread` stdio reader; no `queue.Queue` for MCP message queuing; adapter relies on in-process SQLite, not stdio transport.

### Execution Context Gating (MISSING / Partial)
- Adapter (`storage.py`): `memory_type` CHECK enum includes `architecture_decision`, `code_pattern`, `bug_fix`, `configuration`, `constraint`, `entity`, `insight`, `reference`, `preference`, `task`, `agent_event`, `constitution`, `feature_spec`, `implementation_plan`, `task_breakdown`, `quality_checklist`, `clarification`. Basic type gating exists.
- No explicit `execution context gating` in adapter: no `context` parameter to check if operation is `cron` / `flush` / `subagent` / `background` / `skill_loop` (per contract reference in user's objective/task 21 description). Adapter's `graph()`, `recall()`, `remember()` don't filter or gate based on execution context.
- `docs/HERMES_INTEGRATION.md`: references provider contract (`initialize` / `prefetch` / `sync_turn` / `on_session_end` / `on_pre_compress` / `shutdown`) — adapter does not implement these lifecycle methods (`mnemosyne_client.py` has only `remember`, `recall`, `list_memories`, `consolidate`, `graph`).
- **Contract gap**: No execution context gating; adapter does not implement provider lifecycle contract.

---

## 3. Blocker Note (Task 17 Unblocked — Design Gap)

Task 17 does NOT depend on upstream `785067...`, DB clone, or native binary (adapter hardening is adapter-level design/review task). The adapter's current state (basic path validation, no relative containment, no `MCP_ARGS`, no stdio transport, no execution context gating) is verified against the contract from archive references (`feat/hermes-native-provider` at `09a6973` / `ba6fe984` — different SHAs from user's contract references `407a9b1` / `22a5136` / `32c88ba`). The contract references may point to unretrieved upstream commits; the archive confirms previous adapter contracts (`provider.name`, namespace, DB path, tool names) but the exact commit references are not recoverable from this repo.

---

## 4. Evidence References

- `docs/archive/RUST_ARCHIVE_REF.md`: archive reference (`09a6973` / `ba6fe984`); previous adapter contracts documented; no `407a9b1` reference.
- `docs/HERMES_INTEGRATION.md`: provider lifecycle contract (`initialize` / `prefetch` / `sync_turn` / `on_session_end` / `on_pre_compress` / `shutdown`) — adapter missing.
- `docs/AGENT_SETUP.md`: `MNEMOSYNE_DB_PATH` env setup; adapter `resolve_db_path()` handles `sqlite:///` URLs but no containment check.
- `docs/MCP_CLIENT_CONFIGS.md`: MISSING (no adapter-level MCP args configuration reference).
- Adapter (`src/lib/storage.py` line 88+): basic `db_path` validation; `threading` import used for thread-local connections (not stdio transport); `sqlite3` direct access.
- Adapter (`src/lib/mnemosyne_client.py`): `remember`, `recall`, `list_memories`, `consolidate`, `graph` — 5 methods; no `prefetch`, `sync_turn`, `on_session_end`, `on_pre_compress`, `shutdown`, `context` (execution gating), `MCP_ARGS`.
- Contract discrepancy: user's contract references commits `407a9b1`, `22a5136`, `32c88ba`; archive uses `09a6973` / `ba6fe984`. These may be unretrieved upstream commits from `feat/hermes-native-provider`. Gap documented.

---

## 5. Verification Contract (Task 17)

- [x] Adapter boundary checks inspected: `storage.py` has basic `db_path` validation; `relative path containment` NOT implemented (`os.path.abspath` / containment missing); `configurable MCP_ARGS` NOT present (`mnemosyne_client.py` has no `MCP_ARGS` parameter); `thread-based stdio reader + queue` NOT present (no `threading.Thread` stdio reader; adapter uses synchronous SQLite access); `execution context gating` NOT present (no `cron` / `flush` / `subagent` / `background` / `skill_loop` filtering in adapter methods).
- [x] Archive reference (`feat/hermes-native-provider`) verified: `docs/archive/RUST_ARCHIVE_REF.md` confirms previous adapter contracts; commit SHAs differ from user's contract references (`09a6973` vs `407a9b1`); unretrieved upstream commits noted.
- [x] Provider lifecycle contract (`docs/HERMES_INTEGRATION.md`) verified: adapter does NOT implement `initialize`, `prefetch`, `sync_turn`, `on_session_end`, `on_pre_compress`, `shutdown`.
- [x] Blocker status: unblocked (adapter-level design/review); adapter gap fully documented.
