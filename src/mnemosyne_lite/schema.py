"""Declarative schema transition metadata for the lite SQLite store."""

# Each edge lists work introduced by the destination schema version. The
# initializer still owns the transaction and backup ordering; this table keeps
# version policy separate from that I/O shell.
MIGRATIONS = (
    (0, 1, ("create_memories",)),
    (1, 2, ("create_fts", "rebuild_fts")),
    (2, 3, ("remove_content_lower",)),
)
SCHEMA_VERSION = MIGRATIONS[-1][1]
MIGRATABLE_SCHEMA_VERSIONS = tuple(source for source, _target, _actions in MIGRATIONS)


def actions_from(version: int) -> tuple[str, ...]:
    """Return the ordered transition actions needed to reach the latest schema."""
    return tuple(
        action for source, _target, actions in MIGRATIONS if source >= version for action in actions
    )


MEMORY_COLUMNS = (
    "id",
    "content",
    "namespace",
    "importance",
    "context",
    "summary",
    "keywords",
    "created_at",
    "access_count",
    "last_accessed",
)
PRE_V3_MEMORY_COLUMNS = MEMORY_COLUMNS + ("content_lower",)
FTS_SCHEMA_VERSION = 2

SCHEMA = """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        namespace TEXT NOT NULL,
        importance INTEGER DEFAULT 5 CHECK(importance >= 0 AND importance <= 10),
        context TEXT,
        summary TEXT,
        keywords TEXT,
        created_at REAL DEFAULT 0,
        access_count INTEGER DEFAULT 0,
        last_accessed REAL
    )
"""
FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
    "content, content='memories', content_rowid='rowid', tokenize='unicode61')",
    "CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN "
    "INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content); END",
    "CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN "
    "INSERT INTO memories_fts(memories_fts, rowid, content) "
    "VALUES ('delete', old.rowid, old.content); END",
    "CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE OF content ON memories "
    "BEGIN INSERT INTO memories_fts(memories_fts, rowid, content) "
    "VALUES ('delete', old.rowid, old.content); "
    "INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content); END",
)
RECALL_KEYS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS recall_keys (
        rowid INTEGER PRIMARY KEY,
        importance INTEGER,
        created_at REAL,
        id TEXT
    )
"""
RECALL_KEYS_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS recall_keys_ai AFTER INSERT ON memories BEGIN "
    "INSERT INTO recall_keys (rowid, importance, created_at, id) "
    "VALUES (new.rowid, new.importance, new.created_at, new.id); END",
    "CREATE TRIGGER IF NOT EXISTS recall_keys_ad AFTER DELETE ON memories BEGIN "
    "DELETE FROM recall_keys WHERE rowid = old.rowid; END",
    "CREATE TRIGGER IF NOT EXISTS recall_keys_au "
    "AFTER UPDATE OF importance, created_at, id ON memories "
    "BEGIN UPDATE recall_keys SET importance = new.importance, "
    "created_at = new.created_at, id = new.id WHERE rowid = old.rowid; END",
)
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_ns_created ON memories(namespace, created_at)",
)
DROPPED_INDEXES = (
    "DROP INDEX IF EXISTS idx_memories_ns_rank",
    "DROP INDEX IF EXISTS idx_memories_namespace",
    "DROP INDEX IF EXISTS idx_memories_importance",
    "DROP INDEX IF EXISTS idx_memories_recall",
    "DROP INDEX IF EXISTS idx_memories_rank",
    "DROP INDEX IF EXISTS idx_memories_lower_null",
)
