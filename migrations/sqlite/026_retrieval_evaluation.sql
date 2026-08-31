-- Mnemosyne retrieval diagnostics and bounded evaluation.
-- Raw query text is never stored; traces use a SHA-256 query hash.
CREATE TABLE IF NOT EXISTS retrieval_traces (
    id TEXT PRIMARY KEY NOT NULL,
    query_hash TEXT NOT NULL,
    rewritten_terms TEXT NOT NULL,
    keyword_candidates INTEGER NOT NULL DEFAULT 0,
    vector_candidates INTEGER NOT NULL DEFAULT 0,
    graph_candidates INTEGER NOT NULL DEFAULT 0,
    effective_weights TEXT NOT NULL,
    fallback_reasons TEXT NOT NULL,
    result_ids TEXT NOT NULL,
    used_result_ids TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retrieval_traces_query ON retrieval_traces(query_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS retrieval_golden_items (
    id TEXT PRIMARY KEY NOT NULL,
    query_hash TEXT NOT NULL,
    query_terms TEXT NOT NULL,
    relevant_memory_ids TEXT NOT NULL,
    namespace TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(query_hash, namespace)
);
CREATE INDEX IF NOT EXISTS idx_retrieval_golden_query ON retrieval_golden_items(query_hash);

CREATE TABLE IF NOT EXISTS retrieval_evaluation_runs (
    id TEXT PRIMARY KEY NOT NULL,
    sample_count INTEGER NOT NULL,
    precision_at_5 REAL NOT NULL,
    phrasing_miss_rate REAL NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_adaptive_weights (
    profile TEXT PRIMARY KEY NOT NULL,
    weights TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    last_evaluated_at INTEGER NOT NULL
);
INSERT OR IGNORE INTO retrieval_adaptive_weights
    (profile, weights, sample_count, last_evaluated_at)
VALUES ('default', '{"keyword":0.40,"vector":0.35,"graph":0.18}', 0, 0);

CREATE TABLE IF NOT EXISTS retrieval_evaluation_config (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO retrieval_evaluation_config(key, value) VALUES
    ('enabled', 'true'),
    ('diagnostics_enabled', 'true'),
    ('weekly_interval_seconds', '604800'),
    ('max_samples', '100'),
    ('min_samples_for_adaptation', '20'),
    ('fallback_alert_rate', '0.05');
