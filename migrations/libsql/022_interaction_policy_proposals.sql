-- Migration 022: owner-reviewed interaction-policy proposals.
-- Extracted response guidance is never canonical until an owner applies it.

CREATE TABLE IF NOT EXISTS interaction_policy_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    namespace TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    polarity TEXT NOT NULL CHECK (polarity IN ('prefer', 'avoid')),
    guidance TEXT NOT NULL,
    applicability TEXT NOT NULL,
    signal TEXT NOT NULL CHECK (signal IN ('direct_preference', 'correction', 'dissatisfaction', 'approval')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    anchors TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    proposer TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'dismissed', 'applied', 'failed')),
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    applied_at TEXT,
    error_message TEXT,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_interaction_policy_proposals_status_time
    ON interaction_policy_proposals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_policy_proposals_owner
    ON interaction_policy_proposals(owner, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_policy_proposals_source
    ON interaction_policy_proposals(source_memory_id, created_at DESC);
