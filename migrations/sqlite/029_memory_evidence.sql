-- Migration 029: append-only evidence associations for merged statements.
--
-- memory_provenance holds exactly ONE primary provenance row per memory
-- (memory_id is its primary key). When a near-duplicate, evidence-bearing
-- statement is merged into an existing memory (merge_integrity_parent), a
-- single provenance row cannot represent both statements, so the merge
-- previously overwrote the parent's attribution and earlier evidence
-- disappeared. This table retains an append-only evidence association for
-- EVERY statement merged into a memory.
--
-- * PK (memory_id, source_memory_id, evidence_quote) makes re-merging the
--   same statement idempotent, so appends never duplicate.
-- * ON DELETE SET NULL on source_memory_id: deleting an individual source
--   clears that reference but keeps the surviving observation, so content is
--   never misattributed to a different source.
-- * ON DELETE CASCADE on memory_id: deleting the owning memory drops its
--   evidence associations with it.
CREATE TABLE IF NOT EXISTS memory_evidence (
    memory_id TEXT NOT NULL,
    source_memory_id TEXT,
    evidence_quote TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (memory_id, source_memory_id, evidence_quote),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_source
    ON memory_evidence(source_memory_id);
