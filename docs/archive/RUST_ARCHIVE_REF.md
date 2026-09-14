# Rust Implementation Archive Reference

Archived as part of planning deliverable item 8 (archive Rust, pivot to Python-only runtime).
This is a design/reference file, not an executed deployment change.

Archive references (immutable Git):
- Feature branch: `feat/hermes-native-provider`
- HEAD commit: `09a697398672e5a74928bd47ccea6028e563cfc3`
- Previous stable main: `ba6fe984` (from `.mnemosyne_notes`, 2026-09-07 build)

Files preserved at archive reference (`feat/hermes-native-provider` @ `09a6973`):
- `src/` (full Rust source — RETIRED from mainline; preserved at archive ref)
- `Cargo.toml`, `Cargo.lock`, `build.rs`
- `tests/` (Rust integration/e2e tests)
- `Makefile` (Rust build/test/check targets)
- `migrations/libsql/` and `migrations/sqlite/` (schema versions at archive time)
- `docs/ARCHITECTURE.md`, `docs/HERMES_INTEGRATION.md`, `docs/ORCHESTRATION.md`, `docs/MCP_SERVER.md`

Retirement actions applied to repo (design/execution per user authorization):
- `docs/archive/RUST_ARCHIVE_REF.md` (this file) — reference only; no deployment change.
- `src/` — RETIRED (deleted from mainline per full retirement authorization).
- `Makefile` — Rust targets retired (`build`, `test`, `lint`, `format`, `doctor` for Rust binary).
- `scripts/rebuild-and-update-install.sh` — retired (replaced by Python install: `python -m pip install .`).
- `.github/workflows/ci.yml` — Rust CI steps retired (replaced by Python CI proposal from item 4).
- `pyproject.toml` — `maturin` build-backend retired (if `mnemosyne_core` PyO3 bridge is retired with Rust).
- `README.md` — rewritten with archive note, Python-only quickstart, preserved contracts, unported features.
- `ARCHITECTURE.md` — updated with archive reference, Python-only runtime note, preserved adapter contracts, unported Rust features.
- `AGENTS.md` — updated with archive reference, Python verification commands, preserved contracts, deployed vs unported distinction.

Contracts preserved:
- Provider id: `mnemosyne-rust` (preserved; future `mnemosyne` must preserve)
- Namespace: `agent:hermes`
- DB path: `MNEMOSYNE_DB_PATH` or `<hermes_home>/mnemosyne/mnemosyne.db`
- Tool names: `mnemosyne_memory_search`, `mnemosyne_memory_remember`
- Namespace, DB location, persisted identifiers preserved.
- Keyless local memory preserved; enrichment optional.

Notes:
- No database migration to Rust performed.
- No nomic switch applied.
- No unrelated infrastructure overhaul performed.
- Historical audit gaps remain explicitly unknown (`audit_events` table status unknown; claim_001 contradiction preserved; Sept 13 backup unconfirmed).
