-- Migration 028: keep retrieval diagnostics and golden evaluation scoped.
ALTER TABLE retrieval_traces ADD COLUMN namespace TEXT;
CREATE INDEX IF NOT EXISTS idx_retrieval_traces_namespace_time
    ON retrieval_traces(namespace, created_at DESC);
