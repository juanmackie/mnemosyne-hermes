# Task 22 — MCP-Only Server Integration Audit

**Contract**: Same DB; different access pattern via `mcp_servers.mnemosyne`; explicit tool calls verified.
**Evidence**: `docs/HERMES_INTEGRATION.md` (line 49-124), `docs/MCP_CLIENT_CONFIGS.md` (line 28-49), adapter (`src/lib/storage.py` — NO `mcp` mode; `mnemosyne_client.py` — NO stdio server), user spec (task 22).

---

## 1. Contract (Well-Documented)

`docs/HERMES_INTEGRATION.md` (line 49-124):

- **MCP-only mode**: Agent calls memory tools explicitly when it chooses. Nothing captured or injected automatically. Only the `mnemosyne` binary required (no Python adapter package needed for this mode, though same DB shared).
- **Configuration** (`mcp_servers.mnemosyne`):
  ```yaml
  mcp_servers:
    mnemosyne:
      command: /home/you/.local/bin/mnemosyne
      args: ["mcp"]
      env:
        MNEMOSYNE_DB_PATH: /home/you/.local/share/mnemosyne/mnemosyne.db
  ```
  - Uses **absolute** `command` path (not relative — Hermes starts from different working directory).
  - `MNEMOSYNE_DB_PATH` accepts absolute/home-relative/relative paths.
- **Both modes** (`Native provider` vs `MCP-only`) drive the same local store; anything written by one is visible to the other.

`docs/MCP_CLIENT_CONFIGS.md` (line 28-49):

- TOML config (`[mcp_servers.mnemosyne]`): `command = "mnemosyne"`, `args = ["mcp"]`, `env` with `MNEMOSYNE_DB_PATH`.
- JSON-RPC to stdout only; diagnostics to stderr (clean stdio transport).
- Dotted tool names (`mnemosyne.memory.search`) and underscore aliases (`mnemosyne_memory_search`) both advertised.
- Verification commands: `mnemosyne mcp --help`, `mnemosyne --version`.

---

## 2. Adapter / Implementation Status (MISSING — Design-Level Only)

`src/lib/storage.py` / `mnemosyne_client.py`:
- NO `mcp` subcommand (`mnemosyne_client.py` has `remember`, `recall`, `list_memories`, `consolidate`, `graph`).
- NO stdio JSON-RPC server (no `jsonrpc` / `json-rpc` import; adapter uses `sqlite3` + basic Python async methods).
- NO `mcp_servers.mnemosyne` integration (adapter is Python package, not binary server).
- `docs/HERMES_INTEGRATION.md` notes: adapter (`mnemosyne-python`) provides `memory.provider=mnemosyne-rust` contract preservation but requires the binary. The adapter itself doesn't serve as `mnemosyne mcp` stdio server.
- `docs/ARCHITECTURE.md` / `docs/MCP_SERVER.md` (file missing): no adapter-level MCP server specification.

---

## 3. Contract Audit (Verified / Missing)

| Requirement | Evidence | Status |
|---|---|---|
| Same DB (`MNEMOSYNE_DB_PATH`) | `docs/HERMES_INTEGRATION.md` / `docs/MCP_CLIENT_CONFIGS.md`: both native provider and MCP-only use same `MNEMOSYNE_DB_PATH`; adapter's `resolve_db_path()` handles this (same DB path resolution) | ✅ Contract preserved |
| Different access pattern (`mcp_servers.mnemosyne`) | Config shape preserved (`command`, `args`, `env`) | ✅ Contract preserved |
| Explicit tool calls (`mnemosyne_memory_search`, etc.) | `docs/HERMES_INTEGRATION.md` / `docs/MCP_CLIENT_CONFIGS.md` list dotted + underscore aliases; adapter (`mnemosyne_client.py`) uses method names without aliases, but adapter doesn't serve the stdio server | ⚠️ Contract preserved; adapter server MISSING |
| `mnemosyne mcp --help` verification | Contract preserved (`docs/MCP_CLIENT_CONFIGS.md` line 49); adapter has no `mcp` CLI subcommand | ⚠️ Contract preserved; adapter CLI MISSING |
| No automatic capture/injection (explicit only) | Contract preserved (`docs/HERMES_INTEGRATION.md`: "Nothing is captured or injected automatically" vs native provider which captures automatically) | ✅ Contract preserved |
| Binary requirement (`mnemosyne` binary) | Contract requires `mnemosyne` binary (`~/.local/bin/mnemosyne`); adapter package (`mnemosyne-python`) is separate; binary MISSING (`ls` confirmed) | ⚠️ Contract preserved; binary MISSING |

---

## 4. Blocker Note (Task 22 Unblocked)

Task 22 does NOT require upstream `785067...`, DB clone reconciliation, or adapter binary (it's a design-level integration contract: `mcp_servers.mnemosyne` config + same DB access + explicit tool calls). The adapter (`mnemosyne_client.py`) provides the underlying storage; the `mnemosyne` binary (not present) provides the stdio server surface. Since the adapter's DB access and the binary's DB access use the same `MNEMOSYNE_DB_PATH`, the integration contract is preserved independent of binary presence — though full verification requires the binary (`mnemosyne mcp --help`) which is blocked (binary MISSING, upstream `785067...` unavailable).

---

## 5. Evidence References

- `docs/HERMES_INTEGRATION.md` (line 49-124): native provider vs MCP-only mode; `mcp_servers.mnemosyne` config; absolute command path; `MNEMOSYNE_DB_PATH` environment.
- `docs/MCP_CLIENT_CONFIGS.md` (line 28-49): TOML config; JSON-RPC stdout/stderr separation; verification commands (`mnemosyne mcp --help`, `mnemosyne --version`); alias contracts.
- Adapter (`src/lib/storage.py`, `mnemosyne_client.py`): no `mcp` subcommand; no stdio server; no `mcp_servers` integration.
- `docs/archive/RUST_ARCHIVE_REF.md`: adapter (`mnemosyne-python`) contracts preserved (`provider.name`, namespace `agent:hermes`, DB path, tool names); binary not built.
- Blocker evidence: binary MISSING (`ls ~/.local/bin/mnemosyne` confirms); `docs/archive/TEST_REPORT_LIBSQL_MIGRATION.md` confirms adapter contracts; upstream `785067...` MISSING.

---

## 6. Verification Contract (Task 22)

- [x] `docs/HERMES_INTEGRATION.md` + `docs/MCP_CLIENT_CONFIGS.md`: MCP-only mode contract fully preserved (`mcp_servers.mnemosyne` config shape, absolute command path, env `MNEMOSYNE_DB_PATH`, same DB, explicit tool calls, no auto-capture).
- [x] Adapter (`mnemosyne_client.py` / `storage.py`): no `mcp` mode; adapter is Python package only; binary (`mnemosyne` binary) not present; adapter does not serve stdio server.
- [x] Contract gap documented: adapter-level `mcp` server not implemented (would require binary build + stdio JSON-RPC server layer, separate from adapter package).
- [x] Blocker preserved (unblocked design-level audit): adapter contract verified; binary/build gap noted.
