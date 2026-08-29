-- Migration 020: durable, owner-routed memory change proposals.
-- Proposals are review artifacts; only an explicit accepted -> applied
-- transition may mutate the canonical memories row.

CREATE TABLE IF NOT EXISTS memory_change_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    namespace TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    base_updated_at TEXT NOT NULL,
    before_content TEXT NOT NULL,
    proposed_content TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    source_memory_ids TEXT NOT NULL,
    source_revisions TEXT NOT NULL DEFAULT '[]',
    evidence_quotes TEXT NOT NULL,
    proposer TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'dismissed', 'applied', 'failed')),
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    applied_at TEXT,
    error_message TEXT,
    FOREIGN KEY (target_memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_change_proposals_status_time
    ON memory_change_proposals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_change_proposals_owner
    ON memory_change_proposals(owner, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_change_proposals_target
    ON memory_change_proposals(target_memory_id, created_at DESC);
