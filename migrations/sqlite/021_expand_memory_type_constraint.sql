-- Migration 021: make the StandardSQLite memory-type constraint match the
-- complete workflow enum. Older databases may still carry the pre-workflow
-- CHECK constraint from the original CREATE TABLE.

PRAGMA foreign_keys = OFF;

DROP TRIGGER IF EXISTS memories_ai;
DROP TRIGGER IF EXISTS memories_ad;
DROP TRIGGER IF EXISTS memories_au;
DROP VIEW IF EXISTS active_memories;
DROP VIEW IF EXISTS important_memories;
DROP VIEW IF EXISTS recent_memories;
DROP VIEW IF EXISTS memory_stats;
DROP VIEW IF EXISTS text_learning_orphans;

CREATE TABLE memories_type_new (
    id TEXT PRIMARY KEY NOT NULL,
    namespace TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL,
    tags TEXT NOT NULL,
    context TEXT NOT NULL,
    memory_type TEXT NOT NULL CHECK(memory_type IN (
        'architecture_decision', 'code_pattern', 'bug_fix', 'configuration',
        'constraint', 'entity', 'insight', 'reference', 'preference', 'task',
        'agent_event', 'constitution', 'feature_spec', 'implementation_plan',
        'task_breakdown', 'quality_checklist', 'clarification'
    )),
    importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 10),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    related_files TEXT NOT NULL DEFAULT '[]',
    related_entities TEXT NOT NULL DEFAULT '[]',
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0, 1)),
    superseded_by TEXT,
    embedding_model TEXT NOT NULL,
    memory_class TEXT NOT NULL DEFAULT 'knowledge' CHECK(memory_class IN ('knowledge', 'interaction_policy')),
    archived_at INTEGER,
    FOREIGN KEY (superseded_by) REFERENCES memories_type_new(id)
);

INSERT INTO memories_type_new (
    id, namespace, created_at, updated_at, content, summary, keywords, tags,
    context, memory_type, importance, confidence, related_files,
    related_entities, access_count, last_accessed_at, expires_at, is_archived,
    superseded_by, embedding_model, memory_class, archived_at
)
SELECT
    id, namespace, created_at, updated_at, content, summary, keywords, tags,
    context, memory_type, importance, confidence, related_files,
    related_entities, access_count, last_accessed_at, expires_at, is_archived,
    superseded_by, embedding_model, COALESCE(memory_class, 'knowledge'),
    archived_at
FROM memories;

DROP TABLE memories;
ALTER TABLE memories_type_new RENAME TO memories;

CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_memory_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
CREATE INDEX IF NOT EXISTS idx_memories_is_archived ON memories(is_archived);
CREATE INDEX IF NOT EXISTS idx_memories_superseded_by ON memories(superseded_by);
CREATE INDEX IF NOT EXISTS idx_memories_namespace_type ON memories(namespace, memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_namespace_archived ON memories(namespace, is_archived);
CREATE INDEX IF NOT EXISTS idx_memories_type_importance ON memories(memory_type, importance DESC);
CREATE INDEX IF NOT EXISTS idx_memories_memory_class ON memories(memory_class);

INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');

CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, summary, keywords, tags, context)
    VALUES (NEW.rowid, NEW.content, NEW.summary, NEW.keywords, NEW.tags, NEW.context);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, keywords, tags, context)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.summary, OLD.keywords, OLD.tags, OLD.context);
END;

CREATE TRIGGER memories_au AFTER UPDATE ON memories
WHEN OLD.content != NEW.content
  OR OLD.summary != NEW.summary
  OR OLD.keywords != NEW.keywords
  OR OLD.tags != NEW.tags
  OR OLD.context != NEW.context
BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, keywords, tags, context)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.summary, OLD.keywords, OLD.tags, OLD.context);
    INSERT INTO memories_fts(rowid, content, summary, keywords, tags, context)
    VALUES (NEW.rowid, NEW.content, NEW.summary, NEW.keywords, NEW.tags, NEW.context);
END;

CREATE VIEW active_memories AS SELECT * FROM memories WHERE is_archived = 0;
CREATE VIEW important_memories AS SELECT * FROM memories WHERE importance >= 8 AND is_archived = 0;
CREATE VIEW recent_memories AS
SELECT * FROM memories WHERE updated_at >= datetime('now', '-30 days') ORDER BY updated_at DESC;
CREATE VIEW memory_stats AS
SELECT namespace, COUNT(*) AS total_count,
       SUM(CASE WHEN is_archived = 0 THEN 1 ELSE 0 END) AS active_count,
       SUM(CASE WHEN is_archived = 1 THEN 1 ELSE 0 END) AS archived_count,
       AVG(importance) AS avg_importance, MAX(updated_at) AS last_updated
FROM memories GROUP BY namespace;
CREATE VIEW text_learning_orphans AS
SELECT 'provenance' AS kind, p.memory_id AS id FROM memory_provenance p LEFT JOIN memories m ON m.id = p.memory_id WHERE m.id IS NULL
UNION ALL SELECT 'policy', p.policy_memory_id FROM interaction_policies p LEFT JOIN memories m ON m.id = p.policy_memory_id WHERE m.id IS NULL
UNION ALL SELECT 'policy_evidence', e.policy_memory_id FROM interaction_policy_evidence e LEFT JOIN interaction_policies p ON p.policy_memory_id = e.policy_memory_id WHERE p.policy_memory_id IS NULL
UNION ALL SELECT 'policy_evidence_source', e.source_memory_id FROM interaction_policy_evidence e LEFT JOIN memories m ON m.id = e.source_memory_id WHERE m.id IS NULL
UNION ALL SELECT 'entity', e.memory_id FROM memory_entities e LEFT JOIN memories m ON m.id = e.memory_id WHERE m.id IS NULL;

PRAGMA foreign_keys = ON;
