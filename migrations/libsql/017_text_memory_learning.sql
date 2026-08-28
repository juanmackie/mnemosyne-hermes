-- Additive schema for text turn learning, provenance, policies, and entities.
ALTER TABLE memories ADD COLUMN memory_class TEXT NOT NULL DEFAULT 'knowledge' CHECK(memory_class IN ('knowledge', 'interaction_policy'));
CREATE INDEX IF NOT EXISTS idx_memories_memory_class ON memories(memory_class);

CREATE TABLE IF NOT EXISTS memory_provenance (
    memory_id TEXT PRIMARY KEY NOT NULL,
    source_kind TEXT NOT NULL,
    source_memory_id TEXT,
    session_id TEXT,
    turn_id TEXT,
    source_role TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    evidence_quote TEXT NOT NULL,
    extractor_model TEXT,
    extraction_schema_version TEXT,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS interaction_policies (
    policy_memory_id TEXT PRIMARY KEY NOT NULL,
    polarity TEXT NOT NULL CHECK(polarity IN ('prefer', 'avoid')),
    guidance TEXT NOT NULL,
    applicability TEXT NOT NULL,
    signal TEXT NOT NULL CHECK(signal IN ('direct_preference', 'correction', 'dissatisfaction', 'approval')),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    anchors TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (policy_memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS interaction_policy_evidence (
    policy_memory_id TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (policy_memory_id, source_memory_id, evidence_quote),
    FOREIGN KEY (policy_memory_id) REFERENCES interaction_policies(policy_memory_id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

-- A caller-provided session/turn identity is the retry key for the raw source.
-- Derived rows also retain these fields, so source_memory_id distinguishes the
-- raw turn from its extracted children.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_provenance_raw_turn
ON memory_provenance(session_id, turn_id)
WHERE source_kind = 'turn' AND source_memory_id IS NULL
  AND session_id IS NOT NULL AND turn_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'related',
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    PRIMARY KEY (memory_id, normalized_name, role),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memory_entities_namespace_name ON memory_entities(namespace, normalized_name);
CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(normalized_name);

-- Integrity check used by diagnostics and migration fixtures.
CREATE VIEW IF NOT EXISTS text_learning_orphans AS
SELECT 'provenance' AS kind, p.memory_id AS id FROM memory_provenance p LEFT JOIN memories m ON m.id = p.memory_id WHERE m.id IS NULL
UNION ALL SELECT 'policy', p.policy_memory_id FROM interaction_policies p LEFT JOIN memories m ON m.id = p.policy_memory_id WHERE m.id IS NULL
UNION ALL SELECT 'policy_evidence', e.policy_memory_id FROM interaction_policy_evidence e LEFT JOIN interaction_policies p ON p.policy_memory_id = e.policy_memory_id WHERE p.policy_memory_id IS NULL
UNION ALL SELECT 'policy_evidence_source', e.source_memory_id FROM interaction_policy_evidence e LEFT JOIN memories m ON m.id = e.source_memory_id WHERE m.id IS NULL
UNION ALL SELECT 'entity', e.memory_id FROM memory_entities e LEFT JOIN memories m ON m.id = e.memory_id WHERE m.id IS NULL;
