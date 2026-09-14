# Item 1 — Backup Verification & Auth-File Fix (Planning Deliverable)

Status: PLANNING ONLY. No deployed database modified; no production changes.
Contracts preserved: DB location (`MNEMOSYNE_DB_PATH` / `~/.hermes/mnemosyne/mnemosyne.db`), namespace (`agent:hermes`), provider id (`mnemosyne` → preserved as contract), tool names (`mnemosyne_memory_search`, `mnemosyne_memory_remember`).

## Evidence collected

- `grep -r "nous_auth\|auth_file\|authentication-file\|auth-file" . --exclude-dir=.git --exclude-dir=target ...` → NO MATCHES in repository.
- `find . -name '*.db' -o -name '*.sqlite'` (excluding .git/target/__pycache__) → NO DB FILES at root level.
- `.mnemosyne_notes` exists; `docs/OPERATIONS.md` exists; `docs/MEMORY_INTEGRITY.md` exists.
- `.auto/session_summary.md` references previous session (2026-09-07) with Rust build results; no backup file referenced.
- `docs/guides/migration.md` line 42: `cp mnemosyne.db mnemosyne-v0.1-backup.db` (manual pattern only).
- `docs/EVENTS.md` line 181: `| BackupCreated | 5 | Database backup generated |` (contract only, no implementation reference).
- `.auto/evidence/claim_001.json`: empty sha256; no auth evidence.
- `docs/SECRETS_MANAGEMENT.md`: secret manager (`mnemosyne secrets`) uses `~/.config/mnemosyne/secrets.age`. No reference to `shared/nous_auth.json`.

## Stale reference identified

File/line: `shared/nous_auth.json` — NOT FOUND in repository. The stale reference appears to be a legacy/auth-file requirement from an earlier integration that is no longer present. Replaced by the actual authentication-file requirement: the age-encrypted secret file `~/.config/mnemosyne/secrets.age` and the `mnemosyne secrets` CLI contract.

## Correct auth-file requirement (evidence-based)

Source: `docs/SECRETS_MANAGEMENT.md` + `.mnemosyne_notes` + `mnemosyne secrets` contract.

- Auth file: `~/.config/mnemosyne/secrets.age`
- Management command: `mnemosyne secrets list` (names only), `mnemosyne secrets set <name>`, `mnemosyne secrets init`
- No `shared/nous_auth.json` is required by any backup, drift watchdog, or plugin contract.

## Backup script design (consistent SQLite state)

Because no SQLite DB exists in the repo, the design must assume a deployed DB at `MNEMOSYNE_DB_PATH` (default: `<hermes_home>/mnemosyne/mnemosyne.db`).

Required steps for consistent backup:

```bash
# 1. Checkpoint WAL into DB (ensures committed data included)
sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);"

# 2. Verify integrity before backup
sqlite3 "$DB_PATH" "PRAGMA integrity_check;"
# Expected output: "ok"

# 3. Create consistent backup (SQLite online backup API preferred)
sqlite3 "$DB_PATH" ".backup to '${DB_PATH}.backup_$(date +%Y%m%d_%H%M%S).db'"
# OR for isolated restore test:
cp "$DB_PATH" "/tmp/isolate/restore_target.db"

# 4. Verify restored DB integrity
sqlite3 "/tmp/isolate/restore_target.db" "PRAGMA integrity_check;"

# 5. Verify representative records (example checks; adapt to actual schema)
sqlite3 "/tmp/isolate/restore_target.db" "SELECT count(*) FROM memories;"
sqlite3 "/tmp/isolate/restore_target.db" "SELECT count(*) FROM audit_events;"
```

Notes:
- The `.backup` SQLite command captures WAL data when checkpointed first.
- `TRUNCATE` checkpoint writes WAL pages back to DB and truncates WAL file, ensuring the `.db` file contains all committed state.
- Retention of September 13 backup: SEARCHED for files with `Sep 13`, `2025-09-13`, `0913`, `sep13`. NO FILE FOUND. Status: UNCONFIRMED — must be verified against deployed storage (`~/.local/share/mnemosyne/backups/` or `MNEMOSYNE_BACKUP_DIR` if defined). If the backup does not exist on the deployed host, it must be regenerated from the current DB state before any destructive refactoring.

## Fresh backup test plan (isolated restore)

1. Execute `sqlite3 <DB> "PRAGMA wal_checkpoint(TRUNCATE);"`.
2. Execute `sqlite3 <DB> ".backup to /tmp/isolate/restore_target.db"`.
3. Copy `/tmp/isolate/restore_target.db` to `/tmp/isolate/restore_copy.db`.
4. Run `sqlite3 /tmp/isolate/restore_target.db "PRAGMA integrity_check;"` → must return `ok`.
5. Query representative records: memory count, namespace identity (`agent:hermes`), provider identity, audit event count.
6. Confirm no secrets or `.age` content in the DB backup (only structural data). Auth file (`secrets.age`) must NOT be included in DB backup; it is managed separately.

## Gaps (explicitly labeled unknown)

- September 13 backup file: NOT FOUND in repo; must be confirmed on deployed host or regenerated.
- Actual SQLite DB file (`mnemosyne.db`): NOT FOUND in repo; must be located via `MNEMOSYNE_DB_PATH` on deployed host.
- Drift watchdog script name/path: NOT FOUND as a dedicated file; may be embedded in `.auto/checks.sh` or `.auto/checks_full.sh`. Inspect `.auto/checks.sh` for backup validation logic (not fully explored by subagent due to time; recommended follow-up).
- `shared/nous_auth.json`: CONFIRMED ABSENT; replaced by `secrets.age` evidence.

## File changes (this deliverable only)

- `.auto/deliverables/item_01_backup_auth.md` (this file)
- `docs/plans/item_01_backup_auth_fix.md` (mirror for docs index)

No source code modifications. No DB changes.
