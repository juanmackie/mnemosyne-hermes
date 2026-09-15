# M5 — Maintenance, Security, Self-Hosting (Deliverable)

Status: DESIGN PRESERVED / IMPLEMENTATION BLOCKED. Security contracts preserved (`secrets.age` age-encrypted; `mnemosyne secrets init/set/list`; no `.env*` or `.age` in repo). Backup/integrity design (`sqlite3 .backup`, `PRAGMA integrity_check`, WAL checkpoint `TRUNCATE`) preserved in `scripts/baseline/verify_baseline_install.sh` and `docs/plans/item_01_backup_auth_fix.md`. Self-hosting design (`mnemosyne serve`, MCP stdio/HTTP) preserved (`MCP_SERVER.md`, `docs/HERMES_INTEGRATION.md`). Owner control contracts preserved (inspect, correct, approve, forget, restore through CLI/MCP first).

Blocker: Upstream source MISSING; verified DB clone NOT FOUND; deployed binary NOT FOUND. Maintenance execution (dry-run, approval, version-checked application, rollback) and security integration tests (hostile inputs, concurrent agents, permission revocation) require upstream source and live DB.

Evidence: Adapter (`provider.py`) uses direct SQLite (no subprocess for memory); `secrets.age` contract confirmed (`docs/SECRETS_MANAGEMENT.md`, `.mnemosyne_notes`). No `shared/nous_auth.json` (confirmed absent, replaced by `secrets.age`).
Not done: Maintenance execution verification; concurrent worker lease tests; rollback rehearsal; backup restoration test; permission revocation test.
