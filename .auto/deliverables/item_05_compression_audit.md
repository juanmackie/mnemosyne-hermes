# Item 5 — Compression Durability & Audit Tracking Repair (Planning Deliverable)

Status: PLANNING ONLY. No source modifications applied; no DB writes; no audit records fabricated. Historical audit gaps remain explicitly unknown; no past events manufactured.
Contracts preserved: `memory.provider`, namespace `agent:hermes`, DB location, provider id (`mnemosyne-rust` / `mnemosyne`), checkpoint API version 2.

## Evidence: conflicting checkpoint reports

### Plugin source contract (`provider.py`, lines 286-319)

- `on_pre_compress()` is fail-closed (`CHECKPOINT_API_VERSION = 2`).
- Writes durable, content-addressed file (`digest + ".json"`) to `<storage_dir>/checkpoints/`.
- Uses `_atomic_write()` (lines 537-538 area): `tempfile.mkstemp`, `os.fsync(stream.fileno())`, `os.replace(tmp, path)`. This ensures fsynced, atomic write.
- Idempotent: if file exists (`os.path.isfile(path)`), returns `True` immediately.
- Raises `CheckpointError` on `OSError` (not partial success).
- `self.checkpoints_written += 1` only after successful write (idempotent skip does NOT increment count).

### Current integration gap (from subagent exploration + source inspection)

The adapter (`provider.py`) writes the checkpoint file, but there is NO integration with queued captures (`BackgroundWorker`) before compression. The contract says:

> `on_pre_compress()` writes a durable checkpoint and raises on failure rather than reporting partial success.

However, if captures are queued on `BackgroundWorker` (`_worker`) when `on_pre_compress()` is called, the adapter does NOT drain or checkpoint those queued captures before writing the checkpoint. This creates a potential data loss: queued captures that have not yet persisted to DB or MCP could be lost if the process shuts down during compression, even though the checkpoint file exists.

Proposed required drain/checkpoint behavior:

```python
# Proposed addition (not applied) to provider.py on_pre_compress or before it:
# If BackgroundWorker has queued captures, drain them first:
if self._worker.has_queued():
    try:
        self._worker.drain(timeout=self.config.shutdown_timeout)
    except TimeoutError:
        raise CheckpointError("mnemosyne-rust: pending captures not drained before checkpoint")
# Then proceed with checkpoint write.
```

Note: This requires either adding `has_queued()` and `drain()` methods to `BackgroundWorker` (`worker.py`) or exposing the queued state. Currently `BackgroundWorker` has `enqueue()` and `run()` but no public drain inspection. This is explicitly labeled as a design gap.

## Empty audit tables — evidence

From `.auto/unified_audit.json`:

- `db_audit_trail_rows`: 2
- `memory_evidence_rows`: 0
- `claims_linked_to_memory_evidence`: false
- `audit_log_growing`: true (only 2 rows; minimal growth)
- `unified_audit_version`: 1.0
- `hygiene_integrated`: true

From `.auto/evidence/claim_001.json`:

- `S1_memory_events`: 12
- `S2_mutation_journal`: 12
- `S3_integrity_runs`: 1
- `S4_memory_evidence`: 11
- `S5_audit_trail`: 3
- `verified_at`: 1699999999 (stale timestamp; not current)
- `sha256`: `e3b0c44298fc...` (empty hash — zero content hash, indicating empty or synthetic claim)

Contradiction: `claim_001.json` claims `S4_memory_evidence`: 11 and `S5_audit_trail`: 3, but `.auto/unified_audit.json` reports `memory_evidence_rows`: 0. The claim evidence does NOT match the unified audit. This indicates the audit tracking is broken or incomplete.

### Intended producers of audit records (contract evidence)

Based on `docs/MEMORY_INTEGRITY.md` and `provider.py`:

- `MemoryProvider` (`provider.py`) writes audit events for:
  - `update` (enrichment updates) — mentioned in `MEMORY_INTEGRITY.md`
  - `supersede` (conflict resolution) — mentioned in `MEMORY_INTEGRITY.md`
  - `on_memory_write()` record (diagnostics only, not audit event insertion)
- Maintenance runs (`docs/MEMORY_INTEGRITY.md`) should produce audit records for:
  - `OrphanRepair` (bounded projection repair)
  - Maintenance runs (persisted date idempotency key; exact projection counts recorded in maintenance report)
  - `update` audit events (rather than creating recall rows) when enrichment merges metadata
- Checkpoint writes (`provider.py`) do NOT currently write to audit table; they only write file-based checkpoints.

### Repair proposal (design only)

Wire committed mutations into audit records:

1. **Checkpoint audit record**: When `on_pre_compress()` writes a checkpoint (`digest + ".json"`), insert an audit event:
   - `event_type`: `checkpoint`
   - `digest`: SHA-256 digest of payload
   - `path`: relative path to checkpoint file
   - `provider`: `mnemosyne-rust` (or future `mnemosyne`)
   - `timestamp`: current UTC
2. **Memory mutation audit records**: Ensure `on_memory_write()` writes to audit table (not just internal `self.last_memory_write`). Currently it returns `False` and only records internally; it must produce an audit row with `action`, `target`, `provider`.
3. **Maintenance audit records**: Wire `OrphanRepair` and maintenance runs (from `docs/MEMORY_INTEGRITY.md` or `.auto/checks_full.sh` logic) into audit events with exact projection counts.
4. **Audit verification**: After any mutation or maintenance, run:
   ```sql
   SELECT event_type, count(*) FROM audit_events GROUP BY event_type;
   SELECT event_type, digest FROM audit_events WHERE digest IS NOT NULL;
   SELECT event_type, provider, timestamp FROM audit_events ORDER BY timestamp DESC LIMIT 10;
   ```
   Ensure counts grow after operations.

Note: This proposal requires a database schema that includes `audit_events`. The `docs/MEMORY_INTEGRITY.md` references audit events (`update`, `supersede`, `BackupCreated`), but the actual table schema must be verified (not fully read from migrations). This is an explicit design gap.

## Test rollback and retry behavior

Current adapter (`provider.py`):
- `_atomic_write()` uses `os.replace()` and `os.unlink(tmp)` on exception. If `os.replace()` fails (e.g., disk full, permission error), the original file remains intact; no partial file is left.
- Retry behavior: no retry loop in adapter; `MNEMOSYNE_REQUEST_TIMEOUT` applies to MCP request, not to file write. If file write fails, `CheckpointError` is raised immediately.
- Rollback: if `CheckpointError` is raised during `on_pre_compress()`, the compression is aborted; no checkpoint file exists for the failed digest (idempotent check prevents partial file). This is the correct fail-closed behavior.

Proposed test:

```bash
# Planned rollback/retry test (not executed)
# 1. Create a temporary DB and plugin instance.
# 2. Call on_pre_compress() with a payload; verify checkpoint file exists.
# 3. Corrupt the checkpoint directory (simulate disk failure by changing permissions).
# 4. Call on_pre_compress() with different payload; verify CheckpointError raised.
# 5. Restore permissions; verify original checkpoint still intact (no corruption).
# 6. Call on_pre_compress() with original payload; verify idempotent return (True) and file intact.
```

Historical gaps: The audit table's previous contents (before current session) are UNKNOWN. The claim_001.json timestamp (`1699999999`) is stale. No historical checkpoint files are tracked in the repository. All historical audit gaps must remain explicitly unknown and not manufactured.

## Gaps (explicitly unknown; no manufacturing)

- Whether `audit_events` table exists in the deployed DB schema: UNKNOWN (must verify via `sqlite3 .db ".schema"` or `migrations/` inspection).
- Whether `OrphanRepair` and maintenance runs currently produce audit records: UNKNOWN (evidence suggests contracts require it, but `.auto/unified_audit.json` shows 2 rows only; likely broken or unimplemented).
- Whether `BackgroundWorker` has queued captures during `on_pre_compress()`: UNKNOWN (no drain mechanism exists; requires design change).
- Whether rollback/retry behavior has been tested against real disk failures: UNKNOWN (only code inspection performed).
- Whether historical checkpoint files exist for comparison: UNKNOWN (not tracked in repo).
- Whether past mutation events are recoverable from any backup: UNKNOWN (September 13 backup unconfirmed; no historical DB evidence in repo).

## Deliverable artifacts

- `.auto/deliverables/item_05_compression_audit.md` (this file)
- `docs/plans/item_05_compression_audit.md` (mirror)
- Proposed design notes (not applied): drain/checkpoint integration for `BackgroundWorker`, audit table wiring for mutations and maintenance, checkpoint audit event insertion.
