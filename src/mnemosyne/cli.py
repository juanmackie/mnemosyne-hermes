"""Mnemosyne CLI — production-ready entry point for Hermes testing.

Provides: init, remember, recall, list, bootstrap, embed, migrate, backup, restore, maintenance, diagnostics.
No external LLM required for core memory operations; no subprocess overhead.
"""
import argparse
import sys
import os
import time

from lib.storage import PythonMemoryStorage

DEFAULT_DB = os.path.expanduser("~/.mnemosyne/mnemosyne.db")


def _storage(args):
    return PythonMemoryStorage(os.path.expanduser(args.db_path))


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


def cmd_restore(args):
    """Restore the database from a backup."""
    backup_path = args.backup
    if not os.path.exists(backup_path):
        print(f"ERROR: backup not found: {backup_path}", file=sys.stderr)
        return 1
    dest = args.db_path
    try:
        import sqlite3
        src = sqlite3.connect(backup_path)
        dst = sqlite3.connect(dest)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"Restored: {dest} from {backup_path}")
        return 0
    except Exception as e:
        print(f"ERROR: restore failed: {e}", file=sys.stderr)
        return 1


def cmd_maintenance(args):
    """Run maintenance: dedup, near-dup proposals, audit."""
    s = _storage(args)
    result = s.consolidate(namespace=args.namespace, auto_apply=args.auto_apply)
    s.close()
    print(result)
    return 0


def cmd_diagnostics(args):
    """Print diagnostics: counts, timings, versions, queue state."""
    db_path = args.db_path
    diag = {
        "db_path": db_path,
        "db_exists": os.path.exists(db_path),
        "db_size_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "version": "2.4.0",
        "storage": "PythonMemoryStorage",
        "synchronous": "FULL",
        "wal": True,
        "queue_state": "no upstream source — queue not applicable",
        "blocked": "mnemosyne-memory 3.15.1 upstream source MISSING",
    }
    if os.path.exists(db_path):
        s = _storage(args)
        diag["memory_count"] = s.count()
        diag["namespace_counts"] = {}
        for ns in set(m.get("namespace") for m in s.list_memories(limit=1000)):
            diag["namespace_counts"][ns] = s.count(namespace=ns)
        s.close()
    print(diag)
    return 0


def cmd_mcp(args):
    from .mcp import serve
    return serve(os.path.expanduser(args.db_path))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mnemosyne", description="Mnemosyne memory CLI (production-ready for Hermes testing)")
    parser.add_argument("--version", action="version", version="mnemosyne 2.4.0")
    parser.add_argument("--db-path", default=os.getenv("MNEMOSYNE_DB_PATH", DEFAULT_DB))
    sub = parser.add_subparsers(dest="command")

    p_mcp = sub.add_parser("mcp", aliases=["serve"], help="Run the MCP stdio server")
    p_mcp.set_defaults(func=cmd_mcp)

    p_init = sub.add_parser("init", help="Initialize database schema")
    p_init.set_defaults(func=cmd_init)

    p_rem = sub.add_parser("remember", help="Store a memory")
    p_rem.add_argument("--content", required=True)
    p_rem.add_argument("--namespace", default="agent:hermes")
    p_rem.add_argument("--importance", type=int, default=5)
    p_rem.add_argument("--context", default=None)
    p_rem.add_argument("--no-enrich", action="store_true", help="Compatibility flag; core memory never calls an LLM")
    p_rem.set_defaults(func=cmd_remember)

    p_rec = sub.add_parser("recall", help="Search memories")
    p_rec.add_argument("--query", required=True)
    p_rec.add_argument("--namespace", default=None)
    p_rec.add_argument("--max-results", type=int, default=10)
    p_rec.add_argument("--min-importance", type=int, default=0)
    p_rec.set_defaults(func=cmd_recall)

    p_lst = sub.add_parser("list", help="List memories")
    p_lst.add_argument("--namespace", default=None)
    p_lst.add_argument("--limit", type=int, default=20)
    p_lst.add_argument("--sort-by", default="recent")
    p_lst.set_defaults(func=cmd_list)

    p_boot = sub.add_parser("bootstrap", help="Return bounded constraints, facts, policies, guardrails, skills, provenance, abstentions")
    p_boot.add_argument("--namespace", default=None)
    p_boot.add_argument("--limit", type=int, default=100)
    p_boot.set_defaults(func=cmd_bootstrap)

    p_embed = sub.add_parser("embed", help="Rebuild embeddings (requires upstream source — blocked)")
    p_embed.set_defaults(func=cmd_embed)

    p_migrate = sub.add_parser("migrate", help="Run migrations (requires upstream source — blocked)")
    p_migrate.set_defaults(func=cmd_migrate)

    p_backup = sub.add_parser("backup", help="Backup database")
    p_backup.add_argument("--output", default=None)
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Restore database from backup")
    p_restore.add_argument("--backup", required=True)
    p_restore.set_defaults(func=cmd_restore)

    p_maint = sub.add_parser("maintenance", help="Run maintenance (dedup, near-dup proposals)")
    p_maint.add_argument("--namespace", default=None)
    p_maint.add_argument("--auto-apply", action="store_true")
    p_maint.set_defaults(func=cmd_maintenance)

    p_diag = sub.add_parser("diagnostics", help="Print diagnostics")
    p_diag.set_defaults(func=cmd_diagnostics)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())