-- Schema extracted from a real store created by c928a03's PythonMemoryStorage.
-- Keep this frozen: upgrade tests must not derive v2 DDL from the current code.
-- Sparse rowids exercise the external-content FTS mapping after compaction.
BEGIN;
CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            namespace TEXT NOT NULL,
            importance INTEGER DEFAULT 5 CHECK(importance >= 0 AND importance <= 10),
            context TEXT,
            summary TEXT,
            keywords TEXT,
            created_at REAL DEFAULT 0,
            access_count INTEGER DEFAULT 0,
            last_accessed REAL,
            -- ASCII-lowered copy of content, written by remember() and
            -- backfilled by _ensure_content_lower(). Trailing position so the
            -- column order of SELECT * matches a migrated database.
            content_lower TEXT
        );

CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='rowid', tokenize='unicode61');

CREATE INDEX idx_memories_created ON memories(created_at);

CREATE INDEX idx_memories_lower_null ON memories(content_lower) WHERE content_lower IS NULL;

CREATE INDEX idx_memories_namespace ON memories(namespace);

CREATE INDEX idx_memories_ns_created ON memories(namespace, created_at);

CREATE INDEX idx_memories_rank ON memories(importance DESC, created_at DESC, content_lower, namespace);

CREATE INDEX idx_memories_recall ON memories(namespace, importance, created_at, content_lower);

CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content); END;

CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content); END;

CREATE TRIGGER memories_fts_au AFTER UPDATE OF content ON memories BEGIN INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content); INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content); END;

INSERT INTO memories (rowid, id, content, namespace, importance, context, summary, keywords, created_at, access_count, last_accessed, content_lower) VALUES
(5, 'v2-a', 'Legacy xylophone alpha', 'ns', 8, 'context alpha', 'summary alpha', 'xylophone,alpha', 1.0, 3, 2.0, 'legacy xylophone alpha'),
(17, 'v2-b', 'Legacy zeppelin beta', 'ns', 4, NULL, NULL, NULL, 3.0, 0, NULL, 'legacy zeppelin beta'),
(42, 'v2-c', 'Other xylophone gamma', 'other', 6, NULL, NULL, NULL, 4.0, 1, 5.0, 'other xylophone gamma');
PRAGMA user_version = 2;
COMMIT;
