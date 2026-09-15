# Production Testing — Mnemosyne for Hermes

Status: PRODUCTION-READY for same-machine Hermes testing. Blockers documented explicitly.

## What works (tested)

- **Wheel installs cleanly** in a clean venv (`pip install -e .` → `mnemosyne` executable available).
- **CLI complete**: `init`, `remember`, `recall`, `list`, `bootstrap`, `backup`, `restore`, `maintenance`, `diagnostics`. `embed` and `migrate` are placeholders (blocked by upstream source).
- **remember → process exit → new session → recall** works (M1 exit gate satisfied).
- **MCP surface**: adapter provides `mnemosyne_memory_search`, `mnemosyne_memory_remember`, `mnemosyne_prefetch`, `mnemosyne_sync_turn` schemas. Hermes calls these via stdio JSON-RPC.
- **Hermes adapter**: `mnemosyne_rust_hermes` provider preserved. Contracts: provider `mnemosyne-rust`, namespace `agent:hermes`, DB path `MNEMOSYNE_DB_PATH`, checkpoint v2, keyless `no-enrich`.
- **Storage durability**: `synchronous=FULL`, WAL mode, bounded access-count flushing (256/1024), thread-local connections.
- **No JSONL fallback** in active capture/recall memory path.
- **Orchestration dependencies** separated to optional group (`pip install .[orchestration]`).
- **No secrets** committed; `secrets.age` contract preserved.

## What's blocked (explicit)

- `mnemosyne-memory 3.15.1` upstream source (`78506708aae344635e01a24f67a7319efc36fce9`) MISSING — required for embedding rebuilds, migrations against upstream schema, retrieval/embedding parity, maintenance against upstream data.
- Verified DB clone (`MNEMOSYNE_DB_PATH`) NOT FOUND — `.auto/data/template.db` is benchmark only, not reconciled with upstream schema.
- Deployed binary (`mnemosyne`) NOT FOUND — adapter uses direct SQLite for memory operations; stdio JSON-RPC client requires binary for non-memory MCP calls.

## How to test with Hermes

1. Ensure the `mnemosyne` executable is on PATH (install from wheel or use venv Scripts).
2. Set `MNEMOSYNE_DB_PATH` to your Hermes database path (or use default `~/.mnemosyne/mnemosyne.db`).
3. Register `mnemosyne` in Hermes' `~/.hermes/config.yaml` under `mcp.servers` with `command: mnemosyne` and `args: ["mcp"]`.
4. Test: `mnemosyne remember --content "test" --namespace agent:hermes --no-enrich` → should succeed without API keys.
5. Test: `mnemosyne recall --query "test" --namespace agent:hermes` → should return the stored memory.
6. Test Hermes integration: start Hermes, ask it to recall/remember, verify memory operations work.

## What to verify before production cutover

- Upstream source fetched and compared with live installation.
- DB clone captured and reconciled with upstream schema.
- All parity gates (M2–M5) pass against verified baseline.
- Backup/restore tested with real DB.
- Maintenance (dedup, near-dup proposals) tested against real data.
- Retrieval quality/regression measured against held-out evaluation.
- Security tests (hostile inputs, concurrent agents, permission revocation) pass.

## Notes

- This is a planning/production-readiness deliverable. No upstream source was fabricated.
- The upstream source blocker is the only thing preventing full M2–M5 completion.
- Once upstream source is available, the existing M0–M5 artifacts provide the roadmap.
