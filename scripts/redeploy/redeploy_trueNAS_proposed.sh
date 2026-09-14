#!/bin/bash
# scripts/redeploy/redeploy_trueNAS_proposed.sh — proposed redeploy (design only)
# Planning deliverable item 7. Not executed (Repo-only authorization; no deployed ops).
# Contracts preserved: DB path, namespace, provider identity, pinned dependencies.
set -euo pipefail

echo "=== Proposed TrueNAS redeploy procedure (design) ==="
echo "Status: Design only — NOT EXECUTED against deployed host."
echo "Archive reference: feat/hermes-native-provider (@ 09a6973) / previous stable main (@ ba6fe984)"
echo "Note: Container digest / binary sha256 is UNKNOWN (empty sha256 in .auto/evidence/claim_001.json)."
echo "Note: Deployed DB path is UNKNOWN (DB file not present in repo; must be located via MNEMOSYNE_DB_PATH)."

echo "=== Step 1: Pre-update backup (design) ==="
echo "Would execute: sqlite3 <DB> 'PRAGMA wal_checkpoint(TRUNCATE);'"
echo "Would execute: sqlite3 <DB> '.backup to /srv/backups/mnemosyne/<timestamp>/mnemosyne_pre_update.db'"
echo "Would verify: sqlite3 /srv/backups/mnemosyne/<timestamp>/mnemosyne_pre_update.db 'PRAGMA integrity_check;'"
echo "Note: Backup must include committed WAL data (TRUNCATE checkpoint ensures this)."

echo "=== Step 2: Stop current services ==="
echo "Would execute: docker stop mnemosyne-hermes || systemctl stop mnemosyne-hermes || true"
echo "Wait timeout: MNEMOSYNE_SHUTDOWN_TIMEOUT (default 5s)"

echo "=== Step 3: Deploy new image / binary ==="
echo "Would deploy: docker run -d --name mnemosyne-hermes -e MNEMOSYNE_DB_PATH=/data/mnemosyne.db -e MNEMOSYNE_NAMESPACE=agent:hermes <digest>:mnemosyne-hermes"
echo "Note: Digest must be verified; currently UNKNOWN."

echo "=== Step 4: Smoke checks (proposed) ==="
echo "  1. Binary responds (MNEMOSYNE_BIN --version or mcp --help)"
echo "  2. Plugin registers (python -c 'import importlib.metadata; ... python-provider ...')"
echo "  3. DB connection resolves (MNEMOSYNE_DB_PATH exists and writable)"
echo "  4. Namespace preserved (MNEMOSYNE_NAMESPACE = agent:hermes)"
echo "  5. Provider identity preserved (provider.id = python-provider / future mnemosyne)"
echo "  6. Basic tool responds (mnemosyne_memory_remember / mnemosyne_memory_search returns without exception)"
echo "  7. Checkpoint directory writable (<DB>/checkpoints/ exists and writable)"
echo "  8. Audit table exists/grows (sqlite3 <DB> 'SELECT count(*) FROM audit_events;' must return non-zero)"
echo "Note: Smoke check 8 requires item 5 audit tracking repair (not fully executed)."

echo "=== Step 5: Rollback path (design only) ==="
echo "  Would restore DB from /srv/backups/mnemosyne/<pre_update_timestamp>/mnemosyne_pre_update.db"
echo "  Would restart previous image/service (digest or binary reference must be preserved)"
echo "  Would verify rollback integrity (sqlite3 <DB> 'PRAGMA integrity_check;' + representative records)"
echo "Status: Design complete. Not executed."
