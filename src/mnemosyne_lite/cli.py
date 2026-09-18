"""mnemosyne-lite CLI — standalone store/manual-testing entry point.

Not the Hermes provider (see integrations/hermes-provider/) and not the engine
CLI (mnemosyne-memory ships `mnemosyne`).

Provides: init, remember, recall, list, bootstrap, embed, migrate, backup, restore, maintenance, diagnostics.
No external LLM required for core memory operations; no subprocess overhead.
"""
import argparse
import sys
import os
import shutil
import time
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.storage import PythonMemoryStorage, StorageError          # noqa: E402
from lib.mnemosyne_client import resolve_db_path                    # noqa: E402

DEFAULT_DB = os.path.expanduser("~/.mnemosyne/mnemosyne.db")

# Commands that may create a store. Every other command must NOT fabricate an
# empty database at a mistyped path and then report "0 memories".
CREATING_COMMANDS = {"init", "remember"}


def _default_db_path():
    """MNEMOSYNE_DB_PATH if set, else None so resolve_db_path can consult and
    VALIDATE DATABASE_URL (passing the URL through as a path skipped the
    scheme check and produced a file literally named 'postgres://...')."""
    return os.getenv("MNEMOSYNE_DB_PATH") or None


def _storage(args):
    db_path = resolve_db_path(getattr(args, "db_path", None))
    if args.command not in CREATING_COMMANDS and not os.path.exists(db_path):
        raise StorageError(
            f"no Mnemosyne database at {db_path} (nothing was created). "
            "Run 'mnemosyne init' first, or point --db-path at an existing store."
        )
    # Later handlers (init, diagnostics) report the path that was actually used.
    args.db_path = db_path
    return PythonMemoryStorage(db_path)


def cmd_init(args):
    """Initialize the database schema (idempotent)."""
    s = _storage(args)
    s.close()
    print(f"Initialized: {args.db_path}")
    return 0


def cmd_remember(args):
    s = _storage(args)
    result = s.remember(args.content, args.namespace, args.importance, context=args.context)
    print(result)
    s.close()
    return 0


def cmd_recall(args):
    s = _storage(args)
    results = s.recall(args.query, namespace=args.namespace, max_results=args.max_results, min_importance=args.min_importance)
    for r in results:
        print(r)
    s.close()
    return 0


def cmd_list(args):
    s = _storage(args)
    results = s.list_memories(namespace=args.namespace, limit=args.limit, sort_by=args.sort_by)
    for r in results:
        print(r)
    s.close()
    return 0


def cmd_bootstrap(args):
    """Return bounded constraints, facts, policies, guardrails, skills, provenance, abstentions."""
    s = _storage(args)
    memories = s.list_memories(namespace=args.namespace, limit=args.limit, sort_by="importance")
    s.close()
    bootstrap = {
        "constraints": [f"namespace={args.namespace}" if args.namespace else "no namespace filter"],
        "facts": [m["content"] for m in memories if m.get("memory_type") == "fact"],
        "policies": [m["content"] for m in memories if m.get("memory_type") == "policy"],
        "guardrails": [m["content"] for m in memories if m.get("memory_type") == "guardrail"],
        "skills": [m["content"] for m in memories if m.get("memory_type") == "skill"],
        "provenance": [{"id": m["id"], "namespace": m["namespace"]} for m in memories],
        "abstentions": [],
    }
    print(bootstrap)
    return 0


def cmd_embed(args):
    """Placeholder for embedding rebuilds — requires upstream mnemosyne-memory 3.15.1 source."""
    print(f"ERROR: embed requires upstream mnemosyne-memory 3.15.1 source (blocked).", file=sys.stderr)
    print(f"DB: {args.db_path}", file=sys.stderr)
    return 1


def cmd_migrate(args):
    """Placeholder for migrations — requires upstream mnemosyne-memory 3.15.1 source."""
    print(f"ERROR: migrate requires upstream mnemosyne-memory 3.15.1 source (blocked).", file=sys.stderr)
    print(f"DB: {args.db_path}", file=sys.stderr)
    return 1


def cmd_backup(args):
    """Backup the database using SQLite .backup API."""
    db_path = args.db_path
    if not os.path.exists(db_path):
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1
    dest = args.output or (db_path + f".backup.{int(time.time())}")
    try:
        import sqlite3
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(dest)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"Backup: {dest}")
        return 0
    except Exception as e:
        print(f"ERROR: backup failed: {e}", file=sys.stderr)
        return 1


def _package_version():
    """Version from package metadata, so diagnostics never prints a stale literal."""
    try:
        from importlib.metadata import version
        return version("mnemosyne")
    except Exception:
        return "unknown"


# PRAGMA synchronous returns an int.
SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}


def cmd_restore(args):
    """Restore the database from a backup; overwrites --db-path.

    Accepts both formats this project produces: a real SQLite backup file (what
    `mnemosyne backup` writes) and a gzipped SQL dump (what the ops scripts
    write). Feeding a .gz to sqlite3.connect() used to fail with "file is not a
    database", which told the user nothing.
    """
    backup_path = args.backup
    if not os.path.exists(backup_path):
        print(f"ERROR: backup not found: {backup_path}", file=sys.stderr)
        return 1
    dest = resolve_db_path(getattr(args, "db_path", None))
    dest_dir = os.path.dirname(dest)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    try:
        import gzip
        import sqlite3
        with open(backup_path, "rb") as fh:
            gzipped = fh.read(2) == b"\x1f\x8b"
        if gzipped or backup_path.endswith(".gz"):
            sql = gzip.open(backup_path, "rt", encoding="utf-8", errors="replace").read()
            conn = sqlite3.connect(dest)
            try:
                # A `sqlite3 .dump` file carries BEGIN/COMMIT, so the script is
                # applied atomically; a truncated dump rolls back.
                conn.executescript(sql)
                conn.commit()
            finally:
                conn.close()
        else:
            src = sqlite3.connect(backup_path)
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        print(f"Restored: {dest} from {backup_path}")
        return 0
    except Exception as e:
        print(f"ERROR: restore failed: {e}", file=sys.stderr)
        return 1


def cmd_maintenance(args):
    """Run maintenance: dedup proposals, or remove exact duplicates with --auto-apply."""
    s = _storage(args)
    if getattr(args, "auto_apply", False):
        # Read-only pass first: show what would go, then require confirmation.
        preview = s.consolidate(namespace=args.namespace)
        groups = preview.get("exact_duplicate_groups", 0)
        if not groups:
            print(preview)
            s.close()
            return 0
        for candidate in preview.get("candidates", []):
            print(f"  duplicate group member {candidate['id']} "
                  f"[{candidate['namespace']}]: {candidate['preview']}")
        if not args.yes:
            # A closed/piped stdin raises EOFError rather than answering "no",
            # and must not traceback: no confirmation means no deletion.
            try:
                answer = input(
                    f"Delete duplicates in {groups} exact-duplicate group(s)? [y/N]: "
                )
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled; nothing deleted.")
                s.close()
                return 1
        result = s.consolidate(namespace=args.namespace, auto_apply=True)
    else:
        result = s.consolidate(namespace=args.namespace)
    print(result)
    s.close()
    return 0


def cmd_diagnostics(args):
    """Print diagnostics: counts, timings, versions, queue state."""
    # Diagnostics is the command you run when the store is missing or suspect,
    # so it reports the resolved path instead of refusing like the read
    # commands do. Storage is only opened when the file really exists.
    db_path = resolve_db_path(getattr(args, "db_path", None))
    diag = {
        "db_path": db_path,
        "db_exists": os.path.exists(db_path),
        "db_size_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "version": _package_version(),
        "storage": "PythonMemoryStorage",
        # Filled from the live connection below; they used to be hardcoded
        # ("synchronous": "FULL") while every connection ran NORMAL.
        "synchronous": None,
        "journal_mode": None,
        "schema_version": None,
        "wal": None,
        "queue_state": "no upstream source — queue not applicable",
        "blocked": "mnemosyne-memory 3.15.1 upstream source MISSING",
    }
    if os.path.exists(db_path):
        s = _storage(args)
        diag["memory_count"] = s.count()
        diag["namespace_counts"] = {}
        for ns in set(m.get("namespace") for m in s.list_memories(limit=1000)):
            diag["namespace_counts"][ns] = s.count(namespace=ns)
        conn = s._conn()
        sync_value = conn.execute("PRAGMA synchronous").fetchone()[0]
        diag["synchronous"] = SYNCHRONOUS_NAMES.get(sync_value, sync_value)
        diag["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        diag["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        diag["wal"] = str(diag["journal_mode"]).lower() == "wal"
        s.close()
    print(diag)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mnemosyne-lite", description="Mnemosyne lite CLI (standalone store; not a Hermes provider)")
    parser.add_argument("--db-path", default=_default_db_path(),
                        help=f"SQLite database path (default: {DEFAULT_DB})")
    # Subcommands accept --db-path too, so `mnemosyne recall --db-path X`
    # works as well as the global form. SUPPRESS keeps the subparser default
    # from overwriting a path given before the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db-path", default=argparse.SUPPRESS,
                        help="SQLite database path")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", parents=[common], help="Initialize database schema")
    p_init.set_defaults(func=cmd_init)

    p_rem = sub.add_parser("remember", parents=[common], help="Store a memory")
    p_rem.add_argument("--content", required=True)
    p_rem.add_argument("--namespace", default="agent:hermes")
    p_rem.add_argument("--importance", type=int, default=5)
    p_rem.add_argument("--context", default=None)
    p_rem.set_defaults(func=cmd_remember)

    p_rec = sub.add_parser("recall", parents=[common], help="Search memories")
    p_rec.add_argument("--query", required=True)
    p_rec.add_argument("--namespace", default=None)
    p_rec.add_argument("--max-results", type=int, default=10)
    p_rec.add_argument("--min-importance", type=int, default=0)
    p_rec.set_defaults(func=cmd_recall)

    p_lst = sub.add_parser("list", parents=[common], help="List memories")
    p_lst.add_argument("--namespace", default=None)
    p_lst.add_argument("--limit", type=int, default=20)
    p_lst.add_argument("--sort-by", default="recent")
    p_lst.set_defaults(func=cmd_list)

    p_boot = sub.add_parser("bootstrap", parents=[common], help="Return bounded constraints, facts, policies, guardrails, skills, provenance, abstentions")
    p_boot.add_argument("--namespace", default=None)
    p_boot.add_argument("--limit", type=int, default=100)
    p_boot.set_defaults(func=cmd_bootstrap)

    p_embed = sub.add_parser("embed", parents=[common], help="Rebuild embeddings (requires upstream source — blocked)")
    p_embed.set_defaults(func=cmd_embed)

    p_migrate = sub.add_parser("migrate", parents=[common], help="Run migrations (requires upstream source — blocked)")
    p_migrate.set_defaults(func=cmd_migrate)

    p_backup = sub.add_parser("backup", parents=[common], help="Backup database")
    p_backup.add_argument("--output", default=None)
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", parents=[common], help="Restore database from backup")
    p_restore.add_argument("--backup", required=True)
    p_restore.set_defaults(func=cmd_restore)

    p_maint = sub.add_parser("maintenance", parents=[common], help="Run maintenance (dedup, near-dup proposals)")
    p_maint.add_argument("--namespace", default=None)
    p_maint.add_argument("--auto-apply", action="store_true",
                         help="Delete exactly-identical duplicates (asks for confirmation)")
    p_maint.add_argument("--yes", action="store_true",
                         help="Skip the confirmation prompt for --auto-apply")
    p_maint.set_defaults(func=cmd_maintenance)

    p_diag = sub.add_parser("diagnostics", parents=[common], help="Print diagnostics")
    p_diag.set_defaults(func=cmd_diagnostics)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (StorageError, ValueError) as e:
        # Refused store (foreign schema, lock, unreadable file), bad DATABASE_URL
        # or a rejected argument: one line, no traceback. The message says what
        # was and was not modified.
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())