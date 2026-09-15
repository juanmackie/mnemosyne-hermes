# M0 Parity Matrix — Capability → Implementation → Evidence

All rows reflect the state at repo commit `48a2197` with M0 deliverables complete. No upstream `mnemosyne-memory 3.15.1` source present; no deployed binary present; no DB clone present.

| Capability | Live / Baseline Evidence | Implementation Status | Test / Evidence Path | Blocker / Note |
|---|---|---|---|---|
| Provider ID (`mnemosyne`) | `docs/plans/item_02_python_baseline.md`: contract preserved (`mnemosyne-rust` adapter) | Design only; adapter preserved, binary unavailable | `mnemosyne_rust_hermes/config.py` contract assertions | Missing deployed binary (`MNEMOSYNE_BIN`) |
| Namespace (`agent:hermes`) | Adapter contract (`provider.py`, `.mnemosyne_notes`) | Preserved; no live DB to verify | `verify_baseline_install.sh` (design) | No DB file at `MNEMOSYNE_DB_PATH` |
| DB path (`MNEMOSYNE_DB_PATH`) | Contract reference only | Design only; no DB clone captured | `docs/plans/item_01_backup_auth_fix.md` integrity design | Actual DB file not present |
| Memory search (`mnemosyne_memory_search`) | Contract preserved | Design only; binary unavailable | Adapter test design (`tests/test_provider.py`) | Binary unavailable |
| Memory remember (`mnemosyne_memory_remember`) | Contract preserved | Design only; binary unavailable | Adapter test design | Binary unavailable |
| Checkpoint version (2) | Contract preserved (`CHECKPOINT_API_VERSION`) | Preserved; no live checkpoint to verify | `mnemosyne_rust_hermes/provider.py` snippet | No live instance |
| Prefetch (`mnemosyne_prefetch`) | Contract preserved (`PREFETCH_TOOL`) | Design only | Adapter contract reference | Binary unavailable |
| Sync turn (`mnemosyne_sync_turn`) | Contract preserved (`SYNC_TURN_TOOL`) | Design only | Adapter contract reference | Binary unavailable |
| Schema / Migrations | `migrations/` exists; `.auto/evidence/link_type_fix.patch` exists | Design only; upstream source missing | `migrations/` contents (not verified against upstream) | Upstream source `mnemosyne-memory 3.15.1` missing |
| Embedding identity / model rev | `.mnemosyne_notes`: `bge-small-en-v1.5` referenced | Design only; no live embeddings verified | Not executed (no DB) | DB and upstream source missing |
| Capture pipeline (remember → process → recall) | Contract described in `docs/plans/item_02_python_baseline.md` | Not implemented in this deliverable; requires M3 | `docs/plans/item_02_python_baseline.md` design notes | M3 not started |
| Retrieval / FTS5 / vector / graph | Contract described in `ARCHITECTURE.md`, `docs/plans/item_06_retrieval_baseline.md` | Not implemented; requires M4 | Not executed | M4 not started |
| Maintenance / consolidation / supersede | Contract references in `docs/plans/item_05_compression_audit.md` | Not implemented; requires M5 | Not executed | M5 not started |
| Backup / restore / integrity | Design in `docs/plans/item_01_backup_auth_fix.md`, `scripts/baseline/verify_baseline_install.sh` | Design complete; not executed (no DB) | Integrity check design (`sqlite3 PRAGMA integrity_check`) | No DB file |
| Security / auth / secrets (`secrets.age`) | Confirmed (`docs/SECRETS_MANAGEMENT.md`, `.mnemosyne_notes`) | Design preserved; no `.env*` or `.age` committed | No `shared/nous_auth.json` found; `secrets.age` contract confirmed | No secrets in repo (good) |
| Self-host (`mnemosyne serve`, MCP stdio/HTTP) | `MCP_SERVER.md`, `docs/HERMES_INTEGRATION.md` describe contract | Not rebuilt; adapter preserved only | Not executed (no binary) | Binary unavailable |

This matrix is a design artifact. Once upstream source and DB clone exist, every row must be updated with real verification results before M1 can proceed.
