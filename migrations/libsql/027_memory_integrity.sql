-- Migration 027: centralized memory integrity, deduplication, facts, and repair metadata.
-- Existing content rows are preserved; the legacy maintenance table is rebuilt
-- transactionally below so its CHECK constraint can accept orphan repair.
-- The storage initializer adds/backfills content_hash idempotently because this
-- migration may encounter a partially upgraded legacy database.

CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    observed_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    superseded_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(memory_id, subject, predicate, object),
    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY(superseded_by) REFERENCES memory_facts(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_facts_lookup
    ON memory_facts(subject, predicate, is_active);
CREATE INDEX IF NOT EXISTS idx_memory_facts_memory
    ON memory_facts(memory_id);

-- Migration 019 predates orphan-repair scheduling. Rebuild its CHECK constraint
-- without dropping any rows so old databases can accept the new maintenance kind.
ALTER TABLE memory_maintenance_runs RENAME TO memory_maintenance_runs_027_old;
CREATE TABLE memory_maintenance_runs (
    id TEXT PRIMARY KEY NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    job_kind TEXT NOT NULL CHECK (job_kind IN ('stale_links', 'missing_citations', 'health_summary', 'orphan_repair')),
    namespace TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'timeout')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    item_limit INTEGER NOT NULL CHECK (item_limit > 0),
    retry_limit INTEGER NOT NULL CHECK (retry_limit >= 0),
    timeout_ms INTEGER NOT NULL CHECK (timeout_ms > 0),
    stale_after_days INTEGER NOT NULL CHECK (stale_after_days > 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    items_processed INTEGER NOT NULL DEFAULT 0 CHECK (items_processed >= 0),
    findings_count INTEGER NOT NULL DEFAULT 0 CHECK (findings_count >= 0),
    errors_count INTEGER NOT NULL DEFAULT 0 CHECK (errors_count >= 0),
    report_json TEXT,
    error_message TEXT
);
INSERT INTO memory_maintenance_runs SELECT * FROM memory_maintenance_runs_027_old;
DROP TABLE memory_maintenance_runs_027_old;
-- Recreate the indexes after dropping the renamed table. SQLite index names
-- are schema-global, so creating them before DROP would silently target the
-- old table and leave the replacement unindexed.
CREATE INDEX IF NOT EXISTS idx_memory_maintenance_runs_kind_time
    ON memory_maintenance_runs(job_kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_maintenance_runs_status
    ON memory_maintenance_runs(status, started_at DESC);
