-- Migration 030: consolidation bookkeeping for `mnemosyne consolidate`.
--
-- Exact-duplicate dedup supersedes (never deletes) memory rows. This
-- migration stores the run record and the staging table for derivative
-- rows (dangling link edges, superseded/orphan embeddings) that are
-- moved out of the retrieval lanes instead of hard-deleted.

CREATE TABLE IF NOT EXISTS consolidation_runs (
    id TEXT PRIMARY KEY NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    groups_found INTEGER NOT NULL,
    duplicates_superseded INTEGER NOT NULL,
    edges_repointed INTEGER NOT NULL,
    edges_tombstoned INTEGER NOT NULL,
    vectors_tombstoned INTEGER NOT NULL,
    lineage_repointed INTEGER NOT NULL,
    orphan_links_staged INTEGER NOT NULL,
    active_before INTEGER NOT NULL,
    active_after INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS consolidation_tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_consolidation_tombstones_run
    ON consolidation_tombstones(run_id);
