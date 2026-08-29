-- Migration 019: bounded, observable maintenance run history.
-- Reports are advisory; canonical memory mutations remain explicit operations.

-- Lifecycle columns used by existing evolution scans are ensured by the
-- storage initializer, which can inspect older databases before altering them.

CREATE TABLE IF NOT EXISTS memory_maintenance_runs (
    id TEXT PRIMARY KEY NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    job_kind TEXT NOT NULL CHECK (job_kind IN ('stale_links', 'missing_citations', 'health_summary')),
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

CREATE INDEX IF NOT EXISTS idx_memory_maintenance_runs_kind_time
    ON memory_maintenance_runs(job_kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_maintenance_runs_status
    ON memory_maintenance_runs(status, started_at DESC);
