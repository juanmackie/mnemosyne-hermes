-- Migration 025: durable raw turn transcript tier.
-- Raw turns remain auditable and full-text searchable, but are not memories.
CREATE TABLE IF NOT EXISTS session_transcripts (
    id TEXT PRIMARY KEY NOT NULL,
    namespace TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    content TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_transcripts_identity
    ON session_transcripts(namespace, session_id, turn_id)
    WHERE session_id IS NOT NULL AND turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_session_transcripts_session
    ON session_transcripts(namespace, session_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS session_transcripts_fts USING fts5(
    content,
    user_text,
    assistant_text,
    content='session_transcripts',
    content_rowid='rowid',
    tokenize='porter'
);

CREATE TRIGGER IF NOT EXISTS session_transcripts_ai AFTER INSERT ON session_transcripts BEGIN
    INSERT INTO session_transcripts_fts(rowid, content, user_text, assistant_text)
    VALUES (NEW.rowid, NEW.content, NEW.user_text, NEW.assistant_text);
END;
CREATE TRIGGER IF NOT EXISTS session_transcripts_ad AFTER DELETE ON session_transcripts BEGIN
    INSERT INTO session_transcripts_fts(session_transcripts_fts, rowid, content, user_text, assistant_text)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.user_text, OLD.assistant_text);
END;
CREATE TRIGGER IF NOT EXISTS session_transcripts_au AFTER UPDATE ON session_transcripts BEGIN
    INSERT INTO session_transcripts_fts(session_transcripts_fts, rowid, content, user_text, assistant_text)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.user_text, OLD.assistant_text);
    INSERT INTO session_transcripts_fts(rowid, content, user_text, assistant_text)
    VALUES (NEW.rowid, NEW.content, NEW.user_text, NEW.assistant_text);
END;
