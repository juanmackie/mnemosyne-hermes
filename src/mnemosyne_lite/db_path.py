"""Database path resolution for the lite store.

Kept apart from `storage` so the rule for *which* file this process opens is
readable on its own: an explicit path wins, then DATABASE_URL (validated),
then the default under the user's home.
"""

import os
from urllib.parse import urlparse

# The lite store's own default. It used to be ~/.mnemosyne/mnemosyne.db, which
# is the engine's directory: two different products sharing one folder made it
# impossible to tell which store a path referred to, and a lite command could
# be pointed at an engine bank. Stores created before this change stay where
# they are — point --db-path or MNEMOSYNE_DB_PATH at them, or move the file.
DEFAULT_DB = os.path.expanduser("~/.mnemosyne-lite/mnemosyne.db")


def _filesystem_path(path: str) -> str:
    if path == ":memory:":
        raise ValueError(":memory: databases are not supported; use a filesystem path")
    return path


def resolve_db_path(db_path: str | None = None) -> str:
    """Resolve a usable SQLite file path from an explicit path or DATABASE_URL.

    SQLite paths must name a filesystem file. A URL like
    ``sqlite:///var/lib/mnemosyne.db`` is stripped to its path; any other scheme
    (``postgres://``, ``mysql://``, ...) and the connection-local ``:memory:``
    database are rejected. The store opens one connection per thread, so an
    in-memory database would give each thread a different store.

    Args:
        db_path: Explicit path. When omitted, ``DATABASE_URL`` is consulted,
            then ``~/.mnemosyne-lite/mnemosyne.db``.

    Returns:
        A filesystem path.

    Raises:
        ValueError: If DATABASE_URL uses an unsupported scheme or selects
            ``:memory:``.
    """
    if db_path:
        return _filesystem_path(db_path)

    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        return DEFAULT_DB

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme not in ("sqlite", "sqlite3", "file"):
            raise ValueError(
                f"Unsupported DATABASE_URL scheme {parsed.scheme!r}; "
                "Mnemosyne storage is SQLite-only (use sqlite:///path or a plain path)"
            )
        # sqlite:///abs/path -> /abs/path; sqlite:///rel -> rel; sqlite:// -> ''
        path = parsed.path or parsed.netloc
        if parsed.netloc and parsed.netloc != "localhost":
            path = parsed.netloc + parsed.path
        if path in ("", "/"):
            return _filesystem_path(":memory:")
        return _filesystem_path(path)

    return _filesystem_path(raw)
