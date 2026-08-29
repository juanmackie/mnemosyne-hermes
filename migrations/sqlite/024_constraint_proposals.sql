-- Migration 024: owner-reviewed project constraint proposals.
-- Constraints are scoped guidance, not factual recall. Only approved rows are
-- eligible for the bootstrap read path.

CREATE TABLE IF NOT EXISTS constraint_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    namespace TEXT NOT NULL,
    text TEXT NOT NULL,
    scope TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
    valid_until TEXT,
    source_memory_ids TEXT NOT NULL DEFAULT '[]',
    evidence_quotes TEXT NOT NULL DEFAULT '[]',
    proposer TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'superseded')),
    created_at TEXT NOT NULL,
    approved_by TEXT,
    decided_at TEXT,
    decision_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_constraint_proposals_namespace_status
    ON constraint_proposals(namespace, status, priority DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_constraint_proposals_owner_status
    ON constraint_proposals(owner, status, created_at DESC);
