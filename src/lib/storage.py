"""
Python-native memory storage backend.

Replaces the Rust CLI subprocess approach with direct SQLite access.
Provides async-compatible storage for Mnemosyne memories.

Uses sqlite3 from stdlib — no external dependencies needed.

Thread-safe: each operation opens its own connection (SQLite constraint).
For concurrent access, use WAL mode and external locking.
"""
import sqlite3
import os
import threading
import hashlib
import time
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


def _is_lock_error(exc: Exception) -> bool:
    """True for SQLITE_BUSY / SQLITE_LOCKED, which are worth retrying."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@dataclass
class MemoryRecord:
    id: str
    content: str
    namespace: str
    importance: int
    context: Optional[str] = None
    summary: Optional[str] = None
    keywords: Optional[List[str]] = None
    created_at: Optional[float] = None
    access_count: int = 0
    last_accessed: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


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
    SCHEMA_VERSION = 1

    # The exact column set this store writes to `memories` (pre- and
    # post-content_lower). Anything else is not ours and is refused untouched.
    #
    # This is an allowlist, not a "required columns" check, on purpose: the
    # mnemosyne-memory engine's own `memories` table has 24 columns *including*
    # namespace, so a namespace check adopts a live engine bank and rewrites
    # every row (measured: 137 engine DBs in ~/.mnemosyne, all silently mutated).
    MEMORY_COLUMNS = (
        "id", "content", "namespace", "importance", "context",
        "summary", "keywords", "created_at", "access_count", "last_accessed",
    )
    MIGRATED_MEMORY_COLUMNS = MEMORY_COLUMNS + ("content_lower",)

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
            last_accessed REAL,
            -- ASCII-lowered copy of content, written by remember() and
            -- backfilled by _ensure_content_lower(). Trailing position so the
            -- column order of SELECT * matches a migrated database.
            content_lower TEXT
        )
    """

    # Case folding for the content_lower column and for recall queries.
    # Both SQLite's lower() (used by the backfill and by remember's INSERT) and
    # LIKE's case-insensitivity are ASCII-only, so this table is the exact
    # match. Python's str.lower() must not be used: it folds non-ASCII too,
    # which would make a search match where LIKE would not.
    ASCII_LOWER = str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

    INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace)",
        "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_ns_created ON memories(namespace, created_at)",
        # Matches the recall query shape (namespace filter + ORDER BY importance
        # DESC, created_at DESC) *and* carries the searched column, so the scan
        # can filter and walk rows in output order without touching the table.
        # Without it, every scanned row cost a table fetch (~0.4ms of the
        # ~0.8ms tail for selective queries); with it, only the returned rows
        # are fetched. Costs one content-sized index (see README notes).
        "CREATE INDEX IF NOT EXISTS idx_memories_recall ON memories(namespace, importance, created_at, content_lower)",
        # Same trick for UNFILTERED recall (no namespace): the ORDER BY streams
        # straight from this index with no temp b-tree sort, the LIKE/instr
        # filter is evaluated from the index columns, and LIMIT stops the scan
        # as soon as enough matches are found (common terms match early:
        # ~0.8ms -> ~0.1ms). ponytail: if the planner ever stops picking it,
        # prefer dropping it over INDEXED BY heroics.
        "CREATE INDEX IF NOT EXISTS idx_memories_rank ON memories(importance DESC, created_at DESC, content_lower, namespace)",
        # Partial index over only the rows _ensure_content_lower() has to fix.
        # Normally empty, which makes the "is a backfill needed?" probe an O(1)
        # covering-index seek instead of a full table scan on every open.
        "CREATE INDEX IF NOT EXISTS idx_memories_lower_null ON memories(content_lower) WHERE content_lower IS NULL",
    ]

    # Obsolete indexes, dropped by name on open. Indexes whose *columns* change
    # are handled in _init_schema instead (it compares the stored DDL and
    # rebuilds), because IF NOT EXISTS matches on the name only.
    DROPPED_INDEXES = ["DROP INDEX IF EXISTS idx_memories_ns_rank",
                       # idx_memories_importance lured the planner into walking
                       # the importance index in random row order for unfiltered
                       # recall (a bound LIKE hid the leading wildcard, so the
                       # planner assumed LIKE-opt may apply). A sequential scan
                       # + temp b-tree sort is ~4x faster; list(sort=importance)
                       # sorts cheaply without it at this scale.
                       "DROP INDEX IF EXISTS idx_memories_importance"]

    def __init__(self, db_path: str):
        """
        Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file

        Raises:
            OSError: If database directory cannot be created
            sqlite3.Error: If database cannot be initialized
        """
        if not db_path:
            raise ValueError("db_path is required")
        self.db_path = db_path
        self._ensure_db_dir()
        self._local = threading.local()
        self._init_schema_resilient()

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
        last: Optional[Exception] = None
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
        """Ensure the database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _connect(self, busy_timeout_ms: Optional[int] = None) -> sqlite3.Connection:
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
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
            self._local.search_cache = None
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

    # Apply buffered access counts once this many distinct memories are pending,
    # or once this many hits are pending (a workload that recalls the same few
    # memories forever would never reach the distinct cap).
    ACCESS_FLUSH_DISTINCT = 256
    ACCESS_FLUSH_HITS = 1024

    def _pending(self) -> Dict[str, int]:
        """This thread's not-yet-written access counts, as {memory id: hits}."""
        pending = getattr(self._local, "pending", None)
        if pending is None:
            pending = self._local.pending = {}
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
        batch, self._local.pending = pending, {}
        self._local.pending_hits = 0
        try:
            now = time.time()
            conn = self._conn()
            before = conn.total_changes
            conn.executemany(
                "UPDATE memories SET access_count = access_count + ?, last_accessed = ? WHERE id = ?",
                [(hits, now, mem_id) for mem_id, hits in batch.items()]
            )
            conn.commit()
            # Record how many rows this flush touched so _search_version can
            # ignore them: access-count writes do not change searchable content.
            ignored = getattr(self._local, "ignored_changes", 0)
            self._local.ignored_changes = ignored + (conn.total_changes - before)
        except sqlite3.Error:
            logger.warning("Failed to apply buffered access counts", exc_info=True)
        self._maybe_checkpoint()

    def _classify_schema(self, conn: sqlite3.Connection) -> str:
        """Return 'fresh' or 'ours' for this file, or raise StorageSchemaError.

        Reads only — no DDL, no DML, no persistent pragma — so a refusal leaves
        the file byte-identical. Runs BEFORE the WAL switch in _init_schema,
        because changing the journal mode is itself a write.
        """
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")
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
        if version not in (0, self.SCHEMA_VERSION):
            raise StorageSchemaError(
                f"{self.db_path} carries an unrecognised user_version={version}. "
                "Nothing was modified."
            )
        if not tables:
            return "fresh"
        if "memories" not in tables:
            raise StorageSchemaError(
                f"{self.db_path} is not a Mnemosyne store: it has tables "
                f"{sorted(tables)[:5]} but no 'memories' table. "
                "Nothing was modified."
            )
        columns = tuple(
            row[1] for row in conn.execute("PRAGMA table_info(memories)")
        )
        if columns not in (self.MEMORY_COLUMNS, self.MIGRATED_MEMORY_COLUMNS):
            extra = sorted(set(columns) - set(self.MIGRATED_MEMORY_COLUMNS))
            missing = sorted(set(self.MEMORY_COLUMNS) - set(columns))
            detail = (f"unexpected columns {extra}" if extra
                      else f"missing columns {missing}")
            raise StorageSchemaError(
                f"{self.db_path} is not a Mnemosyne store: 'memories' has "
                f"{len(columns)} columns ({detail}). Nothing was modified."
            )
        return "ours"

    def _init_schema(self):
        """Create or migrate the store in ONE transaction, or change nothing.

        Ordering matters. The classification reads first (so a foreign file is
        refused before the persistent WAL switch), and the DDL + backfill + the
        user_version bump commit together. Python's sqlite3 autocommits DDL not
        DML, so without the explicit BEGIN IMMEDIATE an ALTER TABLE survives a
        later failure — the bug that permanently rewrote foreign databases.
        """
        init_conn = self._connect(self.CLASSIFY_BUSY_TIMEOUT_MS)
        try:
            self._classify_schema(init_conn)
            self._configure(init_conn)
            init_conn.execute("BEGIN IMMEDIATE")
            try:
                init_conn.execute(self.SCHEMA)
                self._ensure_content_lower(init_conn)
                for drop_sql in self.DROPPED_INDEXES:
                    init_conn.execute(drop_sql)
                for ddl in self.INDEXES:
                    name = ddl.split("IF NOT EXISTS", 1)[1].split()[0]
                    row = init_conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (name,)
                    ).fetchone()
                    # IF NOT EXISTS matches on the name only, so an index created
                    # by an older version keeps its old column list. Compare the
                    # stored DDL (SQLite drops the IF NOT EXISTS phrase) and
                    # rebuild when the columns changed.
                    desired = " ".join(ddl.replace("IF NOT EXISTS ", "").split())
                    if row and " ".join((row[0] or "").split()) != desired:
                        init_conn.execute(f"DROP INDEX {name}")
                    init_conn.execute(ddl)
                init_conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
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

    def _ensure_content_lower(self, conn: sqlite3.Connection) -> None:
        """Create and backfill the ASCII-lowered copy of content.

        recall() searches `content_lower` with instr() rather than `content`
        with LIKE: LIKE's case-insensitive pattern matcher costs ~1.3-1.6x a
        plain substring search per row, and for the full-scan shapes (rare
        term, phrase, no match) that per-row cost *is* the whole query once the
        ordered index removes the sort. Results are unchanged -- SQLite's
        lower() folds exactly the ASCII range LIKE folds.

        Rows written by a pre-migration version leave the column NULL, and
        instr(NULL, ...) is NULL, which would silently drop those rows from
        results, so any NULL row triggers a backfill.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        if "content_lower" not in columns:
            conn.execute("ALTER TABLE memories ADD COLUMN content_lower TEXT")
        # Fast probe: served by the partial index on content_lower IS NULL once
        # it exists, and returns on the first row right after the ALTER above.
        if conn.execute(
            "SELECT 1 FROM memories WHERE content_lower IS NULL LIMIT 1"
        ).fetchone():
            conn.execute(
                "UPDATE memories SET content_lower = lower(content) "
                "WHERE content_lower IS NULL"
            )
        # No commit here: the caller owns the transaction so the ALTER and the
        # backfill land with the schema version bump or not at all.

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a database row to a memory dict, by column NAME.

        This used to map row[0]..row[9] positionally, so any migration that
        reordered or inserted a column would silently shift every field into
        the wrong key. sqlite3.Row addresses columns by name; the classification
        in _classify_schema guarantees these columns are present.
        """
        return {key: row[key] for key in self.MEMORY_COLUMNS}

    def remember(
        self,
        content: str,
        namespace: str,
        importance: int,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
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
        if not (0 <= importance <= 10):
            raise ValueError(f"importance must be 0-10, got {importance}")

        mem_id = hashlib.sha256(
            f"{content}:{namespace}:{time.time()}".encode()
        ).hexdigest()[:16]

        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO memories
                   (id, content, content_lower, namespace, importance, context, created_at)
                   VALUES (?, ?, lower(?), ?, ?, ?, ?)""",
                (mem_id, content[:100000], content[:100000], namespace, importance, context, time.time())
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Collision — regenerate ID and retry once
            mem_id = hashlib.sha256(
                f"{content}:{namespace}:{time.time()}:retry".encode()
            ).hexdigest()[:16]
            conn.execute(
                """INSERT INTO memories
                   (id, content, content_lower, namespace, importance, context, created_at)
                   VALUES (?, ?, lower(?), ?, ?, ?, ?)""",
                (mem_id, content[:100000], content[:100000], namespace, importance, context, time.time())
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
            "success": True
        }

    def _search_version(self, conn):
        # data_version detects other connections; total_changes detects this
        # one, minus changes already attributed to access-count flushes (see
        # _flush_accesses): UPDATEs of access_count/last_accessed never touch
        # content_lower, so they must not invalidate the content snapshot.
        return (conn.execute("PRAGMA data_version").fetchone()[0],
                conn.total_changes - getattr(self._local, "ignored_changes", 0))

    # ponytail: linear native substring scans avoid building a posting index;
    # consider a persistent substring index only for much larger corpora.
    # Per-query candidate lists are cached too: they are a pure function of
    # (snapshot, folded query), and any local or concurrent write changes the
    # version and forces a fresh scan. Cap keeps long-running threads bounded.
    SEARCH_QUERY_CACHE_MAX = 64
    _SEARCH_MISS = object()

    def _search_candidates(self, conn, query):
        """Cache the normalized snapshot and per-query candidate id lists.

        SQLite remains authoritative: candidates only narrow the fetch, and
        every returned row is still read through SQL.
        """
        version = self._search_version(conn)
        cache = getattr(self._local, "search_cache", None)
        if cache is None or cache[0] != version:
            # (version, {query: candidates}, snapshot)
            cache = (version, {}, conn.execute("SELECT id, content_lower FROM memories").fetchall())
            self._local.search_cache = cache
        else:
            cached = cache[1].get(query, self._SEARCH_MISS)
            if cached is not self._SEARCH_MISS:
                return cached, version
        candidates = []
        for mem_id, content in cache[2]:
            if content and query in content:
                candidates.append(mem_id)
                if len(candidates) > 128:
                    candidates = None
                    break
        if len(cache[1]) >= self.SEARCH_QUERY_CACHE_MAX:
            cache[1].clear()
        cache[1][query] = candidates
        return candidates, version

    def recall(
        self,
        query: str,
        namespace: Optional[str] = None,
        max_results: int = 10,
        min_importance: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Search memories by keyword match.

        Args:
            query: Search term (required)
            namespace: Optional namespace filter
            max_results: Max results (1-100)
            min_importance: Minimum importance filter

        Returns:
            List of matching memories

        Raises:
            ValueError: If query is empty
        """
        if not query or not query.strip():
            raise ValueError("query cannot be empty")

        max_results = max(1, min(100, max_results))

        conn = self._conn()
        try:
            sql = "SELECT * FROM memories WHERE instr(content_lower, ?) > 0"
            # instr() is case-sensitive, so fold the query the same way the
            # stored column was folded (ASCII-only, like LIKE). instr also
            # takes the query literally, so '%' and '_' no longer need escaping.
            params = [query.translate(self.ASCII_LOWER)]

            if namespace:
                sql += " AND namespace = ?"
                params.append(namespace)

            if min_importance is not None:
                sql += " AND importance >= ?"
                params.append(min_importance)

            sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
            params.append(max_results)

            # Keep the original query for short/common queries and concurrent writes.
            candidates = None
            query_lower = params[0]
            if not namespace and len(query_lower) >= 3 and not conn.in_transaction:
                candidates, version = self._search_candidates(conn, query_lower)
            if candidates is not None and len(candidates) <= 128:
                rows = []
                if candidates:
                    predicate = " AND id IN (" + ",".join("?" for _ in candidates) + ")"
                    narrowed = sql.replace(" ORDER BY", predicate + " ORDER BY", 1)
                    narrowed = narrowed.removesuffix(" LIMIT ?")
                    rows = conn.execute(narrowed, params[:-1] + list(candidates)).fetchall()
                ranks = {(row[3], row[7]) for row in rows}
                # Preserve the original plan's tie ordering, including LIMIT ties.
                # A writer may also commit during index building/filtering/fetching.
                if len(ranks) != len(rows) or self._search_version(conn) != version:
                    rows = conn.execute(sql, params).fetchall()
                else:
                    rows = rows[:max_results]
            else:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.exception("Recall query failed")
            return []

        results = [self._row_to_dict(row) for row in rows]

        # Record the access in memory instead of writing it here: the UPDATE +
        # commit was 60% of a median recall (p50 0.310 -> 0.119ms) and it was
        # the write that let a WAL checkpoint stall a read. See _flush_accesses().
        if results:
            pending = self._pending()
            for r in results:
                pending[r["id"]] = pending.get(r["id"], 0) + 1
            hits = getattr(self._local, "pending_hits", 0) + len(results)
            self._local.pending_hits = hits
            if len(pending) >= self.ACCESS_FLUSH_DISTINCT or hits >= self.ACCESS_FLUSH_HITS:
                self._flush_accesses()

        return results

    def list_memories(
        self,
        namespace: Optional[str] = None,
        limit: int = 20,
        sort_by: str = "recent"
    ) -> List[Dict[str, Any]]:
        """
        List memories.

        Args:
            namespace: Optional namespace filter
            limit: Max results (1-1000)
            sort_by: Sort order (recent, importance, access)

        Returns:
            List of memories
        """
        limit = max(1, min(1000, limit))
        # This is where buffered access counts become visible (sort_by="access").
        self._flush_accesses()

        conn = self._conn()
        try:
            sql = "SELECT * FROM memories"
            params = []

            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            sort_map = {
                "recent": "created_at DESC",
                "importance": "importance DESC",
                "access": "access_count DESC",
            }
            sql += f" ORDER BY {sort_map.get(sort_by, 'created_at DESC')}"

            sql += " LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.exception("List query failed")
            return []

        return [self._row_to_dict(row) for row in rows]

    def consolidate(
        self,
        namespace: Optional[str] = None,
        auto_apply: bool = False
    ) -> Dict[str, Any]:
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
            params = []
            if namespace:
                sql += " WHERE namespace = ?"
                params.append(namespace)

            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.exception("Consolidate read failed")
            return {"total": 0, "duplicate_groups": 0, "exact_duplicate_groups": 0,
                    "removed": 0, "auto_applied": auto_apply}

        proposals: Dict[str, List[str]] = {}
        exact: Dict[str, List[sqlite3.Row]] = {}
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
            except sqlite3.Error:
                conn.rollback()
                logger.exception("Consolidate delete failed")
                removed = 0
            self._flush_accesses()
            self._maybe_checkpoint()

        return {
            "total": len(rows),
            "duplicate_groups": len(proposal_groups),
            "exact_duplicate_groups": len(exact_groups),
            # Bounded preview so a caller can show what --auto-apply would
            # remove before confirming.
            "candidates": [] if auto_apply else [
                {"id": row[0], "namespace": row[2], "preview": row[1][:80]}
                for group in exact_groups.values() for row in group
            ][:50],
            "removed": removed,
            "auto_applied": auto_apply
        }

    def graph(
        self,
        query: Optional[str] = None,
        namespace: Optional[str] = None,
        depth: int = 1
    ) -> Dict[str, Any]:
        """
        Get memory graph structure.

        Args:
            query: Optional search query (not implemented, reserved)
            namespace: Optional namespace filter
            depth: Graph depth (0 = no edges, 1 = namespace edges)

        Returns:
            Graph structure with nodes and edges
        """
        memories = self.list_memories(
            namespace=namespace,
            limit=1000,
            sort_by="recent"
        )

        nodes = [
            {
                "id": m["id"],
                "content": m["content"][:100],
                "namespace": m["namespace"],
                "importance": m["importance"]
            }
            for m in memories
        ]

        edges = []
        if depth > 0:
            by_namespace: Dict[str, List[str]] = {}
            for mem in memories:
                by_namespace.setdefault(mem["namespace"], []).append(mem["id"])

            for ns, ids in by_namespace.items():
                for i in range(len(ids) - 1):
                    edges.append({
                        "source": ids[i],
                        "target": ids[i + 1],
                        "type": "namespace"
                    })

        return {
            "nodes": nodes,
            "edges": edges,
            "total_memories": len(memories)
        }

    def count(self, namespace: Optional[str] = None) -> int:
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
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                    (namespace,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            logger.exception("Count query failed")
            return 0