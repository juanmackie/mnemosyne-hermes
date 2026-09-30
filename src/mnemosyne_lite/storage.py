"""
Python-native memory storage backend.

Replaces the Rust CLI subprocess approach with direct SQLite access.
Provides async-compatible storage for Mnemosyne memories.

Uses sqlite3 from stdlib — no external dependencies needed.

Thread-safe: each operation opens its own connection (SQLite constraint).
For concurrent access, use WAL mode and external locking.
"""

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


def _is_lock_error(exc: Exception) -> bool:
    """True for SQLITE_BUSY / SQLITE_LOCKED, which are worth retrying."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


# A character FTS5's unicode61 tokenizer would keep: a letter or a digit. `_` is
# deliberately excluded because that tokenizer treats it as a separator.
_FTS_TOKEN_CHAR = re.compile(r"[^\W_]")


def _fts_match_expression(query: str) -> str | None:
    """Build an FTS5 MATCH expression from user text, or None to fall back.

    Every whitespace-separated token is emitted as a quoted phrase, so FTS5
    operators in the input (`AND`, `OR`, `NEAR`, `*`, `^`, `:`, `-`) are read as
    text rather than syntax, and a double quote in the input is doubled instead
    of ending the phrase.

    None means the query has no letter or digit at all. FTS5's tokenizer would
    find nothing to match and return an empty result, which is
    indistinguishable from a real miss, so the caller searches it literally.
    """
    if not _FTS_TOKEN_CHAR.search(query):
        return None
    return " ".join('"' + token.replace('"', '""') + '"' for token in query.split())


class StorageError(RuntimeError):
    """Raised when the store cannot be opened at all (locked, unreadable)."""


class StorageSchemaError(StorageError):
    """Raised when a database file is not a Mnemosyne lite store.

    The file is left byte-identical: this is raised by a read-only
    classification that runs before any DDL, DML or persistent pragma.
    """


class PythonMemoryStorage:
    """
    Python-native memory storage using SQLite.

    Drop-in replacement for the Rust CLI backend.
    Provides the same interface as MnemosyneClient but with
    direct database access — no subprocess overhead.

    Thread safety: Each operation opens its own connection.
    For concurrent writes, enable WAL mode (default).
    """

    # Schema generation recorded in PRAGMA user_version. A store carrying a
    # HIGHER version was written by a newer release: refuse it rather than
    # downgrade-migrate it.
    #
    # 1 -> 2 added FTS5; 2 -> 3 removes content_lower and its redundant indexes.
    SCHEMA_VERSION = 3
    # Versions this release opens and migrates forward. 0 is a legacy lite
    # store written before the sentinel existed, or an empty file.
    MIGRATABLE_SCHEMA_VERSIONS = (0, 1, 2)

    # The exact column set this store writes to `memories`. Anything else is
    # not ours and is refused untouched.
    #
    # This is an allowlist, not a "required columns" check, on purpose: the
    # mnemosyne-memory engine's own `memories` table has 24 columns *including*
    # namespace, so a namespace check adopts a live engine bank and rewrites
    # every row (measured: 137 engine DBs in ~/.mnemosyne, all silently mutated).
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

    # Full-text index over `memories.content`, plus the triggers that keep it in
    # sync. This is an external-content FTS5 table: it stores the index only and
    # reads the text back from `memories`, so the corpus is not duplicated.
    #
    # `AFTER UPDATE OF content` (not plain AFTER UPDATE) matters: the buffered
    # access-count flush UPDATEs access_count/last_accessed on every recall
    # batch, and reindexing the row for that would be pure churn.
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
    # The schema generation that introduced the index. A store classified below
    # this needs `rebuild` once; above it, the triggers have kept the index
    # current, so opening stays O(1) instead of reindexing the corpus.
    FTS_SCHEMA_VERSION = 2

    # Tuples, not lists: shared class-level constants must not be mutable.
    INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace)",
        "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_ns_created ON memories(namespace, created_at)",
    )

    # Obsolete indexes, dropped by name on open. Indexes whose *columns* change
    # are handled in _init_schema instead (it compares the stored DDL and
    # rebuilds), because IF NOT EXISTS matches on the name only.
    DROPPED_INDEXES = (
        "DROP INDEX IF EXISTS idx_memories_ns_rank",
        # idx_memories_importance lured the planner into walking
        # the importance index in random row order for unfiltered
        # recall (a bound LIKE hid the leading wildcard, so the
        # planner assumed LIKE-opt may apply). A sequential scan
        # + temp b-tree sort is ~4x faster; list(sort=importance)
        # sorts cheaply without it at this scale.
        "DROP INDEX IF EXISTS idx_memories_importance",
        "DROP INDEX IF EXISTS idx_memories_recall",
        "DROP INDEX IF EXISTS idx_memories_rank",
        "DROP INDEX IF EXISTS idx_memories_lower_null",
    )

    def __init__(self, db_path: str, *, migration_backup_base: str | None = None):
        """
        Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file
            migration_backup_base: Permanent path used to name pre-upgrade
                backups when opening a staged restore. Defaults to db_path.

        Raises:
            StorageError: If the database directory cannot be created, or the
                file cannot be opened (locked, unreadable, or not a store).
            StorageSchemaError: If the file exists but is not a lite store; the
                file is left byte-identical in that case.
        """
        if not db_path:
            raise ValueError("db_path is required")
        if db_path == ":memory:":
            raise ValueError(":memory: databases are not supported; use a filesystem path")
        self.db_path = db_path
        self._migration_backup_base = migration_backup_base or db_path
        self._ensure_db_dir()
        self._local = threading.local()
        self._init_schema_resilient()

    @property
    def _recall_cache(self):
        """The store's only cache: recall results, per thread.

        key -> (version, rows, ids), validated against `_search_version`, so a
        local write or another connection's commit invalidates it, and
        `_flush_accesses` patches the rows whose access_count it changed.

        Per thread, not shared: the version an entry is validated against is
        built from the calling thread's own connection (`PRAGMA data_version`
        plus that connection's `total_changes`), so an entry is only meaningful
        beside the connection that wrote it. With one shared dict, two
        connections whose version tuples happened to agree read each other's
        rows, and a thread could serve a result from a DB state it had never
        observed while `count()` already saw the write.

        The pre-FTS5 design also cached a full copy of every memory's text per
        thread, so memory grew with the corpus times the thread count. The
        full-text index made that snapshot unnecessary: this memo only holds the
        rows a thread actually asked for.
        """
        cache = getattr(self._local, "recall_cache", None)
        if cache is None:
            cache = self._local.recall_cache = {}
        return cache

    # Cold start used to die here: 9/16 simultaneous opens crashed on the review
    # host (journal_mode=WAL returns SQLITE_BUSY when another connection holds an
    # incompatible mode, and a long-lived server constructs storage outside its
    # request loop, so the whole process failed at startup). Retry the whole
    # schema/migration step, then fail with a message that names the cause.
    # ponytail: each attempt can wait the 5s busy timeout, so a sustained
    # exclusive lock takes ~15s to fail — fine for a cold start; lower
    # WAL/classification busy timeouts if a server must start faster under
    # contention.
    OPEN_ATTEMPTS = 2
    OPEN_DELAY = 0.1

    def _init_schema_resilient(self):
        """Run _init_schema, retrying while another process holds the file."""
        last: Exception | None = None
        for attempt in range(self.OPEN_ATTEMPTS):
            try:
                self._init_schema()
                return
            except sqlite3.OperationalError as e:
                if not _is_lock_error(e):
                    raise
                last = e
                time.sleep(self.OPEN_DELAY * (attempt + 1))
        raise StorageError(
            f"could not open {self.db_path}: the database stayed locked after "
            f"{self.OPEN_ATTEMPTS} attempts (last error: {last}). Another Mnemosyne "
            "process may be mid-migration; retry, or remove a stale -wal/-shm pair."
        ) from last

    def _ensure_db_dir(self):
        """Ensure the database directory exists.

        A bare OSError here reached the CLI as a traceback, because `main()`
        only catches StorageError/ValueError. Name the directory and the cause
        instead, and keep the refusal style the rest of the store uses.
        """
        db_dir = os.path.dirname(self.db_path)
        if not db_dir:
            return
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError as e:
            raise StorageError(
                f"cannot create the database directory {db_dir}: {e}. "
                "Point --db-path at a writable location."
            ) from e

    def _connect(self, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
        """Open a connection WITHOUT changing any persistent database state.

        Only per-connection settings are applied, so this is safe to run
        against a file we may end up refusing (the journal mode is persistent
        and therefore deliberately left untouched here: see _configure).
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        # Named access for _row_to_dict; Row still supports row[0] indexing, so
        # the index-based callers keep working.
        conn.row_factory = sqlite3.Row
        # Set the busy timeout before anything that can take a lock. SQLite's
        # connect() timeout covers it, but making it explicit keeps the retry
        # path in _configure honest.
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms or self.BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    BUSY_TIMEOUT_MS = 5000
    # Classification is a cheap read that must not stall a cold start, so it
    # waits less than a real query would.
    CLASSIFY_BUSY_TIMEOUT_MS = 1000
    # How long to keep retrying the one-off journal_mode=WAL switch. A
    # concurrent connection holding an incompatible mode makes it return
    # SQLITE_BUSY, and 16 simultaneous cold opens were measured crashing on the
    # review host. The switch is persistent and only needed once, so a bounded
    # retry (then tolerance if the file is already WAL) is enough. Each attempt
    # uses a SHORT busy timeout, or the retry loop would multiply the 5s wait.
    WAL_SWITCH_ATTEMPTS = 10
    WAL_SWITCH_DELAY = 0.05
    WAL_SWITCH_BUSY_TIMEOUT_MS = 500

    def _configure(self, conn: sqlite3.Connection) -> None:
        """Apply the persistent + per-connection settings of a live store."""
        mode = None
        conn.execute(f"PRAGMA busy_timeout={self.WAL_SWITCH_BUSY_TIMEOUT_MS}")
        for attempt in range(self.WAL_SWITCH_ATTEMPTS):
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                break
            except sqlite3.Error:
                if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                    # Another connection already converted the file; nothing to do.
                    mode = "wal"
                    break
                if attempt == self.WAL_SWITCH_ATTEMPTS - 1:
                    raise
                time.sleep(self.WAL_SWITCH_DELAY)
        if mode is not None and mode.lower() != "wal":
            logger.warning("Could not enable WAL on %s (mode=%s)", self.db_path, mode)
        conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        # WAL + NORMAL skips an fsync per commit (only at checkpoint). Still
        # crash-safe for this store, and commits were the bulk of warm recall
        # cost once connection setup was cached.
        # NORMAL: in WAL mode, commits skip fsync (still crash-safe via WAL).
        # FULL was the dominant cost in remember p99 (~1.8ms → target <1ms).
        conn.execute("PRAGMA synchronous=NORMAL")
        # Memory-mapped I/O for faster reads.
        conn.execute("PRAGMA mmap_size=268435456")
        # Auto-checkpoint runs a PASSIVE checkpoint *inside* whichever commit
        # crosses the page threshold -- and recall commits (the access-count
        # bump), so a read could stall for 12-481ms (measured p99 11.3ms, max
        # 481ms over 300 recalls). Checking in on the write path instead keeps
        # the WAL bounded without ever paying that cost on a read.
        conn.execute("PRAGMA wal_autocheckpoint=0")

    def _new_conn(self) -> sqlite3.Connection:
        """Open a fresh connection with WAL mode and safe settings."""
        conn = self._connect()
        self._configure(conn)
        return conn

    def _conn(self) -> sqlite3.Connection:
        """Return this thread's cached connection, opening it on first use.

        Opening a connection costs a WAL pragma round-trip, and the old code
        did it (twice) on every recall -- that dominated warm recall latency.
        SQLite connections are not shareable across threads, so cache one per
        thread. Use close() to release it.
        """
        # try/except beats getattr-with-default on the every-recall path
        # (CPython 3.11+: zero cost until the attribute is actually missing).
        try:
            conn = self._local.conn
            if conn is not None:
                return conn
        except AttributeError:
            pass
        conn = self._new_conn()
        self._local.conn = conn
        # A reconnect resets both numbers a version is built from, so an entry
        # left by the previous connection could otherwise match the new one.
        self._local.recall_cache = None
        # First connection for this thread: seed the counters so the recall
        # hot path can read them directly (no getattr-with-default per call).
        # close() leaves them in place — ignored_changes must outlive a
        # reconnect within the same thread.
        if not hasattr(self._local, "pending_hits"):
            self._local.pending_hits = 0
        if not hasattr(self._local, "ignored_changes"):
            self._local.ignored_changes = 0
        return conn

    def close(self) -> None:
        """Flush buffered access counts and close this thread's connection."""
        self._flush_accesses()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # Checkpoint when the WAL outgrows this. Called from the write paths only.
    WAL_CHECKPOINT_BYTES = 4 * 1024 * 1024
    # Only check WAL size every N writes to avoid expensive stat calls on every remember.
    WAL_CHECKPOINT_INTERVAL = 10

    def _maybe_checkpoint(self) -> None:
        """Fold a large WAL back into the database once it outgrows a bound.

        With wal_autocheckpoint off, nothing would checkpoint until close(),
        and an ingest of a few thousand memories was measured leaving a 71MB
        WAL next to a 700KB database. Checkpointing is called from the write
        paths and from a flush, never from the read work itself, so a recall
        only ever pays it once per several thousand calls. TRUNCATE is
        best-effort: it returns busy without raising if a reader is active,
        and the next write retries.
        """
        try:
            # The counter lives on the thread-local (one per connection), NOT on
            # self: reading `self._checkpoint_counter` cross-thread never matched
            # the value just written, so with wal_autocheckpoint=0 nothing was
            # ever checkpointed until close() and the WAL grew unbounded
            # (281MB WAL next to a 36KB database, measured).
            counter = getattr(self._local, "_checkpoint_counter", 0) + 1
            self._local._checkpoint_counter = counter
            if counter % self.WAL_CHECKPOINT_INTERVAL != 0:
                return
            wal = self.db_path + "-wal"
            if os.path.exists(wal) and os.path.getsize(wal) >= self.WAL_CHECKPOINT_BYTES:
                self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.Error):
            logger.warning("WAL checkpoint skipped", exc_info=True)

    # Apply buffered access counts once this many id-tuple batches are
    # pending, or once this many hits are pending (a workload that recalls
    # the same few memories forever would never reach the batch cap — the
    # hits cap catches it). Batches, not distinct ids: per-id bookkeeping
    # was removed from the recall hot path and is expanded only at flush.
    # Single-id recalls give identical cadence to the old distinct-id cap;
    # multi-id recalls bound earlier via ACCESS_FLUSH_HITS.
    ACCESS_FLUSH_DISTINCT = 256
    ACCESS_FLUSH_HITS = 1024

    # Bound on the recall result memo (entries, not bytes).
    RECALL_CACHE_MAX = 256

    def _pending(self) -> list[tuple[str, ...]]:
        """This thread's not-yet-written access counts, as a list of id tuples.

        Each recall appends its result-id tuple (increment of +1 per id);
        per-id totals are expanded only when a flush runs. Appending a tuple
        is ~0.1us on the memo-hit path where a per-id Counter.update cost
        ~0.9us — and warm recall p50 lives entirely on that path.
        """
        # pending is never set to None (flush swaps in a fresh list), so
        # AttributeError is the only miss case — try/except is the cheap path.
        try:
            return self._local.pending
        except AttributeError:
            pending = self._local.pending = []
            return pending

    def _flush_accesses(self) -> None:
        """Write out buffered recall access counts. Best effort by design.

        Called from the write paths, from readers of access_count, and from
        close(). On error the batch is dropped: a stale popularity count is
        not worth retrying, and retrying would let the buffer grow unbounded.
        """
        pending = self._pending()
        if not pending:
            return
        batch, self._local.pending = pending, []
        self._local.pending_hits = 0
        # Expand batches to per-id totals only here — flushes are rare
        # (hundreds of recalls apart), so the per-id work is amortized.
        counts: dict[str, int] = {}
        for ids in batch:
            for mem_id in ids:
                counts[mem_id] = counts.get(mem_id, 0) + 1
        now = time.time()
        try:
            conn = self._conn()
            before = conn.total_changes
            conn.executemany(
                "UPDATE memories SET access_count = access_count + ?, last_accessed = ? WHERE id = ?",
                [(hits, now, mem_id) for mem_id, hits in counts.items()],
            )
            conn.commit()
            # Record how many rows this flush touched so _search_version can
            # ignore them: access-count writes do not change searchable content.
            # _conn() ran above on this thread, so ignored_changes exists.
            self._local.ignored_changes += conn.total_changes - before
            committed = True
        except sqlite3.Error:
            logger.warning("Failed to apply buffered access counts", exc_info=True)
            committed = False
        if committed:
            # Patch memoized rows in place instead of clearing the memo.
            # The flush knows exactly which ids changed and by how much, and
            # a version-valid row was read from this same DB state: a local
            # flush does not bump _search_version (ignored_changes cancels
            # total_changes, and PRAGMA data_version only moves on OTHER
            # connections' commits). Clearing forced a full re-query on the
            # next recall per shape — those post-flush misses are what kept
            # the flush-heavy shapes' p50 1.5-2x above the pure-memo shapes.
            # Entries already stale for other reasons stay stale (their
            # entry[0] no longer matches) and are discarded on next read.
            # Only this thread's memo is patched (it is per-thread); other
            # threads' entries are discarded on their next read because this
            # commit moves their PRAGMA data_version.
            try:
                for entry in self._recall_cache.values():
                    for r in entry[1]:
                        inc = counts.get(r["id"])
                        if inc is not None:
                            r["access_count"] = r["access_count"] + inc
                            r["last_accessed"] = now
            except Exception:
                # Best effort: never risk serving stale rows if patching
                # fails — fall back to the old invalidation.
                self._recall_cache.clear()
        else:
            # Failed UPDATE: the DB may hold partial work — old conservative
            # invalidation applies.
            self._recall_cache.clear()
        self._maybe_checkpoint()

    def _classify_schema(self, conn: sqlite3.Connection) -> tuple[str, int]:
        """Return ('fresh' | 'ours', user_version) for this file, or raise.

        Reads only — no DDL, no DML, no persistent pragma — so a refusal leaves
        the file byte-identical. Runs BEFORE the WAL switch in _init_schema,
        because changing the journal mode is itself a write.

        The version is returned because the migration needs it: a store
        classified below FTS_SCHEMA_VERSION predates the full-text index and
        must rebuild it once, while a current one must not.
        """
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        except sqlite3.DatabaseError as e:
            # A lock is transient, not a schema verdict: re-raise it as
            # OperationalError so _init_schema_resilient retries instead of
            # telling the user their file is not SQLite.
            if _is_lock_error(e):
                raise sqlite3.OperationalError(str(e)) from e
            raise StorageSchemaError(
                f"{self.db_path} is not a SQLite database ({e}). Nothing was modified."
            ) from e
        # The sentinel is checked before the table scan so a file stamped by
        # another tool is refused whatever its tables look like.
        if version > self.SCHEMA_VERSION:
            raise StorageSchemaError(
                f"{self.db_path} was written by a newer Mnemosyne "
                f"(user_version={version} > {self.SCHEMA_VERSION}). "
                "Refusing to downgrade it; nothing was modified."
            )
        if version not in (*self.MIGRATABLE_SCHEMA_VERSIONS, self.SCHEMA_VERSION):
            raise StorageSchemaError(
                f"{self.db_path} carries an unrecognised user_version={version}. "
                "Nothing was modified."
            )
        if not tables:
            return "fresh", version
        if "memories" not in tables:
            raise StorageSchemaError(
                f"{self.db_path} is not a Mnemosyne store: it has tables "
                f"{sorted(tables)[:5]} but no 'memories' table. "
                "Nothing was modified."
            )
        columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(memories)"))
        accepted_columns = (
            (self.MEMORY_COLUMNS,)
            if version == self.SCHEMA_VERSION
            else (
                self.MEMORY_COLUMNS,
                self.PRE_V3_MEMORY_COLUMNS,
            )
        )
        if columns not in accepted_columns:
            extra = sorted(set(columns) - set(self.PRE_V3_MEMORY_COLUMNS))
            missing = sorted(set(self.MEMORY_COLUMNS) - set(columns))
            detail = f"unexpected columns {extra}" if extra else f"missing columns {missing}"
            raise StorageSchemaError(
                f"{self.db_path} is not a Mnemosyne store: 'memories' has "
                f"{len(columns)} columns ({detail}). Nothing was modified."
            )
        return "ours", version

    def _remove_content_lower(self, conn: sqlite3.Connection) -> None:
        """Rebuild the table without relying on SQLite's DROP COLUMN parser.

        The v2 CREATE TABLE contains comments before its final column, which
        older SQLite versions rewrite into invalid SQL when dropping it.
        Preserve rowids and associated objects while swapping in the v3 table;
        the caller's transaction makes the swap atomic.
        """
        objects = [
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name = 'memories' "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
            )
        ]
        conn.execute(self.SCHEMA.replace("IF NOT EXISTS memories", "memories_v3", 1))
        columns = ", ".join(("rowid", *self.MEMORY_COLUMNS))
        conn.execute(f"INSERT INTO memories_v3 ({columns}) SELECT {columns} FROM memories")
        conn.execute("DROP TABLE memories")
        conn.execute("ALTER TABLE memories_v3 RENAME TO memories")
        for ddl in objects:
            conn.execute(ddl)

    def _init_schema(self):
        """Create or migrate the store in ONE transaction, or change nothing.

        Ordering matters. The classification reads first (so a foreign file is
        refused before the persistent WAL switch). Older lite stores receive an
        automatic backup before any write; schema DDL and the version bump then
        commit together.
        """
        init_conn = self._connect(self.CLASSIFY_BUSY_TIMEOUT_MS)
        try:
            kind, classified_version = self._classify_schema(init_conn)
            if kind == "ours" and classified_version < self.SCHEMA_VERSION:
                backup_path = f"{self._migration_backup_base}.pre-v3.{time.time_ns()}.bak"
                backup_conn = sqlite3.connect(backup_path)
                try:
                    init_conn.backup(backup_conn)
                finally:
                    backup_conn.close()
            self._configure(init_conn)
            if classified_version < self.SCHEMA_VERSION:
                # Table replacement must not cascade into referencing tables.
                # This connection closes after migration; live connections
                # enable foreign keys in _connect. Check them before commit.
                init_conn.execute("PRAGMA foreign_keys=OFF")
            init_conn.execute("BEGIN IMMEDIATE")
            compact_after_migration = False
            try:
                init_conn.execute(self.SCHEMA)
                for ddl in self.FTS_DDL:
                    init_conn.execute(ddl)
                if classified_version < self.FTS_SCHEMA_VERSION:
                    # A store from before the index (or a legacy one from before
                    # the sentinel) has no indexed rows: build it now, inside
                    # the same transaction, so the corpus is never searchable
                    # through a half-built index.
                    init_conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                for drop_sql in self.DROPPED_INDEXES:
                    init_conn.execute(drop_sql)
                columns = {row[1] for row in init_conn.execute("PRAGMA table_info(memories)")}
                if "content_lower" in columns:
                    compact_after_migration = True
                    self._remove_content_lower(init_conn)
                for ddl in self.INDEXES:
                    name = ddl.split("IF NOT EXISTS", 1)[1].split()[0]
                    row = init_conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
                    ).fetchone()
                    # IF NOT EXISTS matches on the name only, so an index created
                    # by an older version keeps its old column list. Compare the
                    # stored DDL (SQLite drops the IF NOT EXISTS phrase) and
                    # rebuild when the columns changed.
                    desired = " ".join(ddl.replace("IF NOT EXISTS ", "").split())
                    if row and " ".join((row[0] or "").split()) != desired:
                        init_conn.execute(f"DROP INDEX {name}")
                    init_conn.execute(ddl)
                if (
                    compact_after_migration
                    and init_conn.execute("PRAGMA foreign_key_check").fetchone()
                ):
                    raise sqlite3.IntegrityError("schema migration would violate a foreign key")
                init_conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
                init_conn.commit()
                if compact_after_migration:
                    # Table replacement releases old pages to SQLite's freelist;
                    # VACUUM returns that space to the filesystem, but may
                    # renumber hidden rowids. Rebuild the external-content FTS
                    # index so its rowid mapping stays aligned afterwards.
                    init_conn.execute("VACUUM")
                    init_conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                    init_conn.commit()
            except Exception:
                init_conn.rollback()
                raise
        except (sqlite3.Error, StorageError):
            # Not logged here: the exception message is the report, and callers
            # (CLI, provider) surface it. Logging it again printed the same
            # refusal twice.
            raise
        finally:
            init_conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a database row to a memory dict, by column NAME.

        This used to map row[0]..row[9] positionally, so any migration that
        reordered or inserted a column would silently shift every field into
        the wrong key. sqlite3.Row addresses columns by name; the classification
        in _classify_schema guarantees these columns are present.
        """
        return {key: row[key] for key in self.MEMORY_COLUMNS}

    def remember(
        self, content: str, namespace: str, importance: int, context: str | None = None
    ) -> dict[str, Any]:
        """
        Store a memory.

        Args:
            content: Memory content (required, max 100K chars)
            namespace: Namespace (required)
            importance: Importance score 0-10
            context: Optional context information

        Returns:
            dict: Memory metadata (id, success)

        Raises:
            ValueError: If content or namespace is empty
            sqlite3.Error: If database write fails
        """
        if not content or not content.strip():
            raise ValueError("content cannot be empty")
        if not namespace or not namespace.strip():
            raise ValueError("namespace cannot be empty")
        if len(content) > 100_000:
            raise ValueError("content cannot exceed 100000 characters")
        if not (0 <= importance <= 10):
            raise ValueError(f"importance must be 0-10, got {importance}")

        mem_id = hashlib.sha256(f"{content}:{namespace}:{time.time()}".encode()).hexdigest()[:16]

        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO memories
                   (id, content, namespace, importance, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    content,
                    namespace,
                    importance,
                    context,
                    time.time(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Collision — regenerate ID and retry once
            mem_id = hashlib.sha256(
                f"{content}:{namespace}:{time.time()}:retry".encode()
            ).hexdigest()[:16]
            conn.execute(
                """INSERT INTO memories
                   (id, content, namespace, importance, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    content,
                    namespace,
                    importance,
                    context,
                    time.time(),
                ),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to store memory")
            raise

        self._flush_accesses()
        self._maybe_checkpoint()

        return {
            "id": mem_id,
            "content": content[:200],  # Truncate in response
            "namespace": namespace,
            "importance": importance,
            "success": True,
        }

    def _search_version(self, conn):
        # data_version detects other connections; total_changes detects this
        # one, minus changes already attributed to access-count flushes (see
        # _flush_accesses): UPDATEs of access_count/last_accessed change no
        # searchable text, so they must not invalidate the recall memo.
        # Callers all hold a connection from _conn() on this thread, which
        # seeds ignored_changes — direct read, no getattr.
        return (
            conn.execute("PRAGMA data_version").fetchone()[0],
            conn.total_changes - self._local.ignored_changes,
        )

    def _recall_fulltext(self, conn, match, namespace, max_results, min_importance):
        """FTS5 MATCH, ordered by BM25 with a stable tie-break.

        BM25 alone leaves equally-scoring rows in an arbitrary order, so a
        `LIMIT` could drop a different row between two identical queries. The
        tie-break is the store's own importance/recency order.
        """
        sql = (
            "SELECT m.* FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ?"
        )
        params: list[Any] = [match]
        if namespace:
            sql += " AND m.namespace = ?"
            params.append(namespace)
        if min_importance is not None:
            sql += " AND m.importance >= ?"
            params.append(min_importance)
        # `m.id` last: BM25 ties are common (identical text, identical scores),
        # and without a unique final key `LIMIT` can drop a different row between
        # two identical queries.
        sql += " ORDER BY bm25(memories_fts), m.importance DESC, m.created_at DESC, m.id LIMIT ?"
        params.append(max_results)
        return conn.execute(sql, params).fetchall()

    def _recall_substring(self, conn, query, namespace, max_results, min_importance):
        """Literal substring scan, for queries FTS5 cannot tokenize.

        A query with no letters or digits (`%`, `_`, `***`) has no FTS token to
        match, and FTS5 answers it with an empty result rather than an error —
        indistinguishable from a real miss. `instr(lower(content), lower(?))`
        takes the punctuation literally while folding ASCII case without a
        duplicated text column.
        """
        sql = "SELECT * FROM memories WHERE instr(lower(content), lower(?)) > 0"
        params: list[Any] = [query]
        if namespace:
            sql += " AND namespace = ?"
            params.append(namespace)
        if min_importance is not None:
            sql += " AND importance >= ?"
            params.append(min_importance)
        # `id` last for the same reason as the full-text path: without a unique
        # final key, equally-ranked rows come back in an arbitrary order and
        # `LIMIT` can drop a different one between two identical queries.
        sql += " ORDER BY importance DESC, created_at DESC, id LIMIT ?"
        params.append(max_results)
        return conn.execute(sql, params).fetchall()

    def recall(
        self,
        query: str,
        namespace: str | None = None,
        max_results: int = 10,
        min_importance: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search memories by keyword.

        Matching is SQLite full text (FTS5, ranked by BM25 with the store's
        importance/recency order as the tie-break). A query containing no
        letters or digits is searched literally as a substring instead, because
        it has no token for FTS5 to match.

        Args:
            query: Search term (required)
            namespace: Optional namespace filter
            max_results: Max results (1-100)
            min_importance: Minimum importance filter

        Returns:
            List of matching memories, best match first

        Raises:
            ValueError: If query is empty
            StorageError: If the store cannot be read
        """
        # isspace() answers the same emptiness question as strip() without
        # allocating a copy of the query on every recall.
        if not query or query.isspace():
            raise ValueError("query cannot be empty")

        # Two compares instead of two builtin calls; same clamp semantics.
        if max_results < 1:
            max_results = 1
        elif max_results > 100:
            max_results = 100

        conn = self._conn()
        memo_version = self._search_version(conn)
        key = (query, namespace, max_results, min_importance)
        entry = self._recall_cache.get(key)
        if entry is not None and entry[0] == memo_version:
            cached, ids = entry[1], entry[2]
            if cached:
                pending = self._pending()
                pending.append(ids)
                hits = self._local.pending_hits + len(ids)
                self._local.pending_hits = hits
                if len(pending) >= self.ACCESS_FLUSH_DISTINCT or hits >= self.ACCESS_FLUSH_HITS:
                    self._flush_accesses()
            # r.copy() == dict(r) (same shallow copy) but ~20% cheaper —
            # this return is the hot path of every memo hit.
            return [r.copy() for r in cached]
        try:
            match = _fts_match_expression(query)
            if match is None:
                rows = self._recall_substring(conn, query, namespace, max_results, min_importance)
            else:
                rows = self._recall_fulltext(conn, match, namespace, max_results, min_importance)
        except sqlite3.Error as e:
            # Fail closed: "no match" and "store broken" must not look the same.
            # Returning [] here contradicted the refusal path (a foreign or
            # unreadable store is refused loudly everywhere else).
            logger.exception("Recall query failed")
            raise StorageError(f"recall failed on {self.db_path}: {e}") from e

        results = [self._row_to_dict(row) for row in rows]
        ids = tuple(r["id"] for r in results)
        cache = self._recall_cache
        if len(cache) >= self.RECALL_CACHE_MAX:
            with contextlib.suppress(StopIteration, KeyError):
                cache.pop(next(iter(cache)))
        cache[key] = (memo_version, results, ids)

        # Record the access in memory instead of writing it here: the UPDATE +
        # commit was 60% of a median recall (p50 0.310 -> 0.119ms) and it was
        # the write that let a WAL checkpoint stall a read. See _flush_accesses().
        if results:
            pending = self._pending()
            pending.append(ids)
            hits = self._local.pending_hits + len(results)
            self._local.pending_hits = hits
            if len(pending) >= self.ACCESS_FLUSH_DISTINCT or hits >= self.ACCESS_FLUSH_HITS:
                self._flush_accesses()

        # Fresh copies: the memo must keep pristine rows. r.copy() over
        # dict(r): identical shallow copy, measurably cheaper (50-row:
        # 4.07us vs 5.64us).
        return [r.copy() for r in results]

    def list_memories(
        self, namespace: str | None = None, limit: int = 20, sort_by: str = "recent"
    ) -> list[dict[str, Any]]:
        """
        List memories.

        Args:
            namespace: Optional namespace filter
            limit: Max results (1-1000)
            sort_by: Sort order (recent, importance, access)

        Returns:
            List of memories
        """
        sort_map = {
            "recent": "created_at DESC",
            "importance": "importance DESC",
            "access": "access_count DESC",
        }
        if sort_by not in sort_map:
            raise ValueError(f"unknown sort_by {sort_by!r}; choose recent, importance, or access")
        limit = max(1, min(1000, limit))
        # This is where buffered access counts become visible (sort_by="access").
        self._flush_accesses()

        conn = self._conn()
        try:
            sql = "SELECT * FROM memories"
            params: list[Any] = []

            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            sql += f" ORDER BY {sort_map[sort_by]}"

            sql += " LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.exception("List query failed")
            raise StorageError(f"list failed on {self.db_path}: {e}") from e

        return [self._row_to_dict(row) for row in rows]

    def consolidate(self, namespace: str | None = None, auto_apply: bool = False) -> dict[str, Any]:
        """
        Consolidate similar memories (dedup by content prefix).

        Args:
            namespace: Optional namespace filter
            auto_apply: Actually delete duplicates if True

        Returns:
            Consolidation results

        Two different rules, deliberately kept apart:

        * ``duplicate_groups`` is a HEURISTIC proposal — rows sharing a
          namespace and the first 50 lowercased characters. Useful to review,
          never a delete criterion.
        * ``exact_duplicate_groups`` is the DELETE rule — rows whose FULL
          content is byte-identical within one namespace. The prefix rule used
          to drive deletion, which removed real memories that merely started
          alike ("Q3 revenue was 12%" vs "Q3 revenue was 13%").
        """
        conn = self._conn()
        try:
            sql = "SELECT id, content, namespace, importance, created_at FROM memories"
            params: list[Any] = []
            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            # A failed read used to report "0 duplicate groups", i.e. a clean
            # bill of health for a store it could not read.
            logger.exception("Consolidate read failed")
            raise StorageError(f"consolidate read failed on {self.db_path}: {e}") from e

        proposals: dict[str, list[str]] = {}
        exact: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            proposals.setdefault(f"{row[2]}:{row[1][:50].lower()}", []).append(row[0])
            exact.setdefault(f"{row[2]}:{row[1]}", []).append(row)

        proposal_groups = {k: v for k, v in proposals.items() if len(v) > 1}
        exact_groups = {k: v for k, v in exact.items() if len(v) > 1}
        removed = 0

        if auto_apply and exact_groups:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for group in exact_groups.values():
                    # Keep the most important row, ties broken by the earliest
                    # write so repeated runs are deterministic.
                    best = max(group, key=lambda r: (r[3] or 0, -(r[4] or 0)))
                    for row in group:
                        if row[0] == best[0]:
                            continue
                        conn.execute("DELETE FROM memories WHERE id = ?", (row[0],))
                        removed += 1
                conn.commit()
            except sqlite3.Error as e:
                conn.rollback()
                logger.exception("Consolidate delete failed")
                raise StorageError(f"consolidate delete failed on {self.db_path}: {e}") from e
            self._flush_accesses()
            self._maybe_checkpoint()

        return {
            "total": len(rows),
            "duplicate_groups": len(proposal_groups),
            "exact_duplicate_groups": len(exact_groups),
            # Bounded preview so a caller can show what --auto-apply would
            # remove before confirming.
            "candidates": []
            if auto_apply
            else [
                {"id": row[0], "namespace": row[2], "preview": row[1][:80]}
                for group in exact_groups.values()
                for row in group
            ][:50],
            "removed": removed,
            "auto_applied": auto_apply,
        }

    def count(self, namespace: str | None = None) -> int:
        """
        Count memories.

        Args:
            namespace: Optional namespace filter

        Returns:
            Number of memories
        """
        conn = self._conn()
        try:
            if namespace:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?", (namespace,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.exception("Count query failed")
            raise StorageError(f"count failed on {self.db_path}: {e}") from e
