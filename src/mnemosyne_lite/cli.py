"""mnemosyne-lite CLI — the standalone store's command-line surface.

Not the Hermes provider (see integrations/hermes-provider/) and not the engine
CLI (mnemosyne-memory ships `mnemosyne`).

Provides: init, remember, recall, list, describe, bootstrap, backup, restore,
maintenance and diagnostics. No external LLM is required and there is no
subprocess overhead.
"""

import argparse
import contextlib
import json
import os
import sys
import time

from . import __version__  # one version source; never importlib.metadata("mnemosyne")
from .db_path import DEFAULT_DB, resolve_db_path  # (that name is the engine's distribution)
from .storage import PythonMemoryStorage, StorageError

# Commands that may create a store. Every other command must NOT fabricate an
# empty database at a mistyped path and then report "0 memories".
CREATING_COMMANDS = {"init", "remember"}


def _storage(args):
    db_path = _resolve_existing_db(args)
    return PythonMemoryStorage(db_path)


def _resolve_existing_db(args):
    """Resolve --db-path (env/DATABASE_URL aware) and expand ~.

    Refuses to invent a store for any command that is not allowed to create one,
    so a mistyped path cannot produce a fresh empty database that then reports
    "0 memories". Returns the resolved path and records it on args so later
    reporting (init, diagnostics) shows what was actually used.
    """
    db_path = os.path.expanduser(resolve_db_path(getattr(args, "db_path", None)))
    if getattr(args, "command", None) not in CREATING_COMMANDS and not os.path.exists(db_path):
        raise StorageError(
            f"no Mnemosyne database at {db_path} (nothing was created). "
            "Run 'mnemosyne-lite init' first, or point --db-path at an existing store."
        )
    args.db_path = db_path
    return db_path


def _emit_value(args, value):
    """Print one result: JSON when `--format json` was asked for, else its repr."""
    if getattr(args, "format", "text") == "json":
        print(json.dumps(value, default=str))
    else:
        print(value)


def _emit_rows(args, rows):
    """Print a row list as one JSON array or a readable aligned text table.

    The shapes differ on purpose. A machine-readable caller wants a single
    document it can parse; a human at a terminal wants labeled columns.
    `cards` is handled by the recall/list commands, which know the query
    and the totals the header needs.
    """
    if getattr(args, "format", "text") == "json":
        print(json.dumps(rows, default=str))
        return
    if not rows:
        print("No memories found.")
        return

    columns = list(rows[0])
    labels = [column.replace("_", " ").upper() for column in columns]
    values = [
        [
            str(row.get(column) if row.get(column) is not None else "-")
            .replace("\n", " ")
            .replace("\t", " ")
            for column in columns
        ]
        for row in rows
    ]
    widths = [max(len(label), max(len(row[i]) for row in values)) for i, label in enumerate(labels)]
    print("  ".join(label.ljust(width) for label, width in zip(labels, widths, strict=True)))
    print("  ".join("-" * width for width in widths))
    for row in values:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))


def cmd_init(args):
    """Initialize the database schema (idempotent)."""
    s = _storage(args)
    s.close()
    if getattr(args, "format", "text") == "json":
        _emit_value(args, {"ok": True, "initialized": args.db_path})
    else:
        print(f"Initialized: {args.db_path}")
    return 0


def cmd_remember(args):
    s = _storage(args)
    result = s.remember(args.content, args.namespace, args.importance, context=args.context)
    _emit_value(args, result)
    s.close()
    return 0


def cmd_recall(args):
    s = _storage(args)
    results = s.recall(
        args.query,
        namespace=args.namespace,
        max_results=args.max_results,
        min_importance=args.min_importance,
    )
    if getattr(args, "format", "text") == "cards":
        from .cards import DEFAULT_CARD_CHARS, render_recall_cards

        total = s.count_matching(
            args.query,
            namespace=args.namespace,
            min_importance=args.min_importance,
        )
        indexed = s.count()
        max_chars = getattr(args, "max_chars", None) or DEFAULT_CARD_CHARS
        print(render_recall_cards(results, args.query, total, indexed, max_chars))
    else:
        _emit_rows(args, results)
    s.close()
    return 0


def cmd_list(args):
    s = _storage(args)
    results = s.list_memories(namespace=args.namespace, limit=args.limit, sort_by=args.sort_by)
    if getattr(args, "format", "text") == "cards":
        from .cards import DEFAULT_CARD_CHARS, render_list_cards

        indexed = s.count()
        max_chars = getattr(args, "max_chars", None) or DEFAULT_CARD_CHARS
        print(render_list_cards(results, indexed, max_chars))
    else:
        _emit_rows(args, results)
    s.close()
    return 0


def cmd_describe(args):
    """Summarise the store so an agent knows what to ask for.

    Read-only like the other inspect commands: a missing path is refused,
    never fabricated into an empty store.
    """
    s = _storage(args)
    doc = s.describe()
    doc = {"ok": True, "db_path": args.db_path, **doc}
    s.close()
    if getattr(args, "format", "text") == "json":
        print(json.dumps(doc, default=str))
        return 0
    print(f"Store: {doc['db_path']} · {doc['memory_count']} memories")
    namespaces = doc["namespaces"]
    if namespaces:
        print("Namespaces:")
        for ns, count in namespaces.items():
            print(f"  {ns}: {count}")
    else:
        print("Namespaces: (none)")
    date_range = doc.get("date_range", {})
    print(
        f"Date range: {date_range.get('min_created_at', '-')} .. "
        f"{date_range.get('max_created_at', '-')}"
    )
    print("Example calls:")
    for call in doc.get("example_calls", []):
        print(f"  {call}")
    return 0


def cmd_bootstrap(args):
    """Return bounded constraints, provenance and abstentions for a namespace.

    The facts/policies/guardrails/skills lists this used to print were always
    empty: they filtered on `m.get("memory_type")`, and this store has no
    `memory_type` column (that is the engine's schema). Printing four empty
    lists implied they had been searched. Category filtering needs a schema
    change; until then this reports only what the store can answer.
    """
    s = _storage(args)
    memories = s.list_memories(namespace=args.namespace, limit=args.limit, sort_by="importance")
    s.close()
    bootstrap = {
        "constraints": [f"namespace={args.namespace}" if args.namespace else "no namespace filter"],
        "provenance": [{"id": m["id"], "namespace": m["namespace"]} for m in memories],
        "abstentions": [],
    }
    _emit_value(args, bootstrap)
    return 0


def cmd_backup(args):
    """Backup the database using SQLite .backup API."""
    # Resolve through the same path the other commands use. Reading args.db_path
    # directly meant a missing --db-path/MNEMOSYNE_DB_PATH gave None, and the
    # command died with "stat: path should be string ... not NoneType".
    try:
        db_path = _resolve_existing_db(args)
    except StorageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    dest = args.output or (db_path + f".backup.{int(time.time())}")
    try:
        import sqlite3

        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(dest)
        src.backup(dst)
        dst.close()
        src.close()
        _emit_value(args, {"ok": True, "backup": dest, "source": db_path})
        return 0
    except Exception as e:
        print(f"ERROR: backup failed: {e}", file=sys.stderr)
        return 1


def cmd_mcp(args):
    """Run the newline-delimited MCP stdio server against an existing store."""
    # Resolve first: an MCP client must not be handed a freshly fabricated empty
    # store because the configured path was wrong.
    db_path = _resolve_existing_db(args)
    try:
        from mnemosyne_lite.mcp import serve
    except ImportError:  # imported as part of the installed package
        from .mcp import serve
    return serve(db_path)


# PRAGMA synchronous returns an int.
SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}


def cmd_restore(args):
    """Restore the database from a backup; replaces --db-path.

    Accepts both formats this project produces: a real SQLite backup file (what
    `mnemosyne-lite backup` writes) and a gzipped SQL dump. Feeding a .gz to
    sqlite3.connect() used to fail with "file is not a database", which told
    the user nothing.

    The restore is staged rather than in place. The source is replayed into a
    fresh temporary file next to the destination, that file is opened as a
    store to prove it really is one, and only then is it moved over the
    destination. Before this, a dump was replayed straight onto the live
    database (colliding with its tables), nothing checked that the source was a
    Mnemosyne store, and a bad restore destroyed the previous contents with no
    way back.
    """
    backup_path = args.backup
    if not os.path.exists(backup_path):
        print(f"ERROR: backup not found: {backup_path}", file=sys.stderr)
        return 1
    dest = os.path.expanduser(resolve_db_path(getattr(args, "db_path", None)))
    if os.path.exists(dest):
        try:
            PythonMemoryStorage.validate_existing_file(dest)
        except StorageError as e:
            print(f"ERROR: restore destination is not a Mnemosyne store: {e}", file=sys.stderr)
            return 1
    dest_dir = os.path.dirname(dest)
    if dest_dir:
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            print(f"ERROR: cannot create {dest_dir}: {e}", file=sys.stderr)
            return 1

    if not getattr(args, "yes", False):
        # A closed/piped stdin raises EOFError rather than answering "no", and
        # must not traceback: no confirmation means no restore.
        prompt = f"Replace {dest} with {backup_path}? [y/N]: "
        try:
            if getattr(args, "format", "text") == "json":
                print(prompt, end="", file=sys.stderr)
                answer = input()
            else:
                answer = input(prompt)
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            if getattr(args, "format", "text") == "json":
                _emit_value(args, {"ok": False, "cancelled": True, "destination": dest})
            else:
                print("Cancelled; nothing changed.")
            return 1

    import gzip
    import sqlite3

    safety_backup = None
    if os.path.exists(dest):
        safety_backup = f"{dest}.pre-restore.{int(time.time())}"
        try:
            src = sqlite3.connect(dest)
            dst = sqlite3.connect(safety_backup)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        except Exception as e:
            print(f"ERROR: could not write the safety backup {safety_backup}: {e}", file=sys.stderr)
            return 1
        if getattr(args, "format", "text") != "json":
            print(f"Safety backup: {safety_backup}")

    staged = f"{dest}.restore-tmp.{os.getpid()}"
    try:
        with open(backup_path, "rb") as fh:
            gzipped = fh.read(2) == b"\x1f\x8b"
        if gzipped or backup_path.endswith(".gz"):
            with gzip.open(backup_path, "rt", encoding="utf-8", errors="replace") as fh:
                sql = fh.read()
            conn = sqlite3.connect(staged)
            try:
                # A `sqlite3 .dump` file carries BEGIN/COMMIT, so the script is
                # applied atomically; a truncated dump rolls back.
                conn.executescript(sql)
                conn.commit()
            finally:
                conn.close()
        else:
            # mode=ro: validating must not rewrite the backup itself.
            src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            dst = sqlite3.connect(staged)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

        # Proves the staged file is a store this project can open: a foreign or
        # truncated source is refused here, before the destination is touched.
        # Older stores still get their required pre-upgrade backup. Name it
        # after the permanent destination so it survives as a useful backup,
        # rather than an orphan named after the temporary staging file.
        PythonMemoryStorage(staged, migration_backup_base=dest).close()
        os.replace(staged, dest)
    except StorageError as e:
        print(f"ERROR: {backup_path} is not a Mnemosyne store: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: restore failed: {e}", file=sys.stderr)
        return 1
    finally:
        # Best effort: a leftover staged file is not worth failing the restore
        # for, and the next run reuses the same name only after this process
        # exits (the name carries the pid).
        with contextlib.suppress(OSError):
            os.remove(staged)
    if getattr(args, "format", "text") == "json":
        _emit_value(
            args,
            {
                "ok": True,
                "restored": dest,
                "backup": backup_path,
                "safety_backup": safety_backup,
            },
        )
    else:
        print(f"Restored: {dest} from {backup_path}")
    return 0


def cmd_maintenance(args):
    """Run maintenance: dedup proposals, or remove exact duplicates with --auto-apply."""
    s = _storage(args)
    if getattr(args, "auto_apply", False):
        # Read-only pass first: show what would go, then require confirmation.
        preview = s.consolidate(namespace=args.namespace)
        groups = preview.get("exact_duplicate_groups", 0)
        if not groups:
            _emit_value(args, preview)
            s.close()
            return 0
        output = sys.stderr if getattr(args, "format", "text") == "json" else sys.stdout
        for candidate in preview.get("candidates", []):
            print(
                f"  duplicate group member {candidate['id']} "
                f"[{candidate['namespace']}]: {candidate['preview']}",
                file=output,
            )
        if not args.yes:
            # A closed/piped stdin raises EOFError rather than answering "no",
            # and must not traceback: no confirmation means no deletion.
            prompt = f"Delete duplicates in {groups} exact-duplicate group(s)? [y/N]: "
            try:
                if getattr(args, "format", "text") == "json":
                    print(prompt, end="", file=sys.stderr)
                    answer = input()
                else:
                    answer = input(prompt)
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                if getattr(args, "format", "text") == "json":
                    _emit_value(args, {"ok": False, "cancelled": True, "preview": preview})
                else:
                    print("Cancelled; nothing deleted.")
                s.close()
                return 1
        result = s.consolidate(namespace=args.namespace, auto_apply=True)
    else:
        result = s.consolidate(namespace=args.namespace)
    _emit_value(args, result)
    s.close()
    return 0


def cmd_diagnostics(args):
    """Print diagnostics: counts, sizes, versions and live PRAGMA state."""
    # Diagnostics is the command you run when the store is missing or suspect,
    # so it reports the resolved path instead of refusing like the read
    # commands do. Storage is only opened when the file really exists.
    db_path = resolve_db_path(getattr(args, "db_path", None))
    diag = {
        "db_path": db_path,
        "db_exists": os.path.exists(db_path),
        "db_size_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "version": __version__,
        "storage": "PythonMemoryStorage",
        # Filled from the live connection below; they used to be hardcoded
        # ("synchronous": "FULL") while every connection ran NORMAL.
        "synchronous": None,
        "journal_mode": None,
        "schema_version": None,
        "wal": None,
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
    _emit_value(args, diag)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mnemosyne-lite",
        description="Mnemosyne lite CLI (standalone store; not a Hermes provider)",
    )
    parser.add_argument("--version", action="version", version=f"mnemosyne-lite {__version__}")
    parser.add_argument(
        "--db-path",
        # None so resolve_db_path can consult and VALIDATE DATABASE_URL
        # (passing the URL through as a path skipped the scheme check and
        # produced a file literally named 'postgres://...').
        default=(os.getenv("MNEMOSYNE_DB_PATH") or None),
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "cards"),
        default="text",
        help="Output: text (aligned table, default), json, or cards (capped cited cards)",
    )
    # Subcommands accept --db-path and --format too, so
    # `mnemosyne-lite recall --db-path X --format json` works as well as the
    # global form. SUPPRESS keeps the subparser default from overwriting a value
    # given before the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db-path", default=argparse.SUPPRESS, help="SQLite database path")
    common.add_argument(
        "--format",
        choices=("text", "json", "cards"),
        default=argparse.SUPPRESS,
        help="Output: text (aligned table, default), json, or cards",
    )
    sub = parser.add_subparsers(dest="command")

    p_mcp = sub.add_parser(
        "mcp", parents=[common], aliases=["serve"], help="Run the MCP stdio server"
    )
    p_mcp.set_defaults(func=cmd_mcp)

    p_init = sub.add_parser("init", parents=[common], help="Initialize database schema")
    p_init.set_defaults(func=cmd_init)

    p_rem = sub.add_parser("remember", parents=[common], help="Store a memory")
    p_rem.add_argument("--content", required=True)
    # Neutral default: this surface is not the Hermes provider, so writing into
    # the provider's `agent:hermes` namespace by default was misleading. Pass
    # --namespace agent:hermes to keep writing where earlier versions put it.
    p_rem.add_argument("--namespace", default="default")
    p_rem.add_argument("--importance", type=int, default=5)
    p_rem.add_argument("--context", default=None)
    p_rem.set_defaults(func=cmd_remember)

    p_rec = sub.add_parser("recall", parents=[common], help="Search memories")
    p_rec.add_argument("--query", required=True)
    p_rec.add_argument("--namespace", default=None)
    p_rec.add_argument("--max-results", type=int, default=10)
    p_rec.add_argument("--min-importance", type=int, default=0)
    p_rec.add_argument(
        "--max-chars",
        type=int,
        default=500,
        help="Per-card content cap for --format cards (truncation marked with …)",
    )
    p_rec.set_defaults(func=cmd_recall)

    p_lst = sub.add_parser("list", parents=[common], help="List memories")
    p_lst.add_argument("--namespace", default=None)
    p_lst.add_argument("--limit", type=int, default=20)
    p_lst.add_argument("--sort-by", default="recent")
    p_lst.add_argument(
        "--max-chars",
        type=int,
        default=500,
        help="Per-card content cap for --format cards (truncation marked with …)",
    )
    p_lst.set_defaults(func=cmd_list)

    p_desc = sub.add_parser(
        "describe", parents=[common], help="Summarise the store before searching"
    )
    p_desc.set_defaults(func=cmd_describe)

    p_boot = sub.add_parser(
        "bootstrap",
        parents=[common],
        help="Return bounded constraints, provenance and abstentions",
    )
    p_boot.add_argument("--namespace", default=None)
    p_boot.add_argument("--limit", type=int, default=100)
    p_boot.set_defaults(func=cmd_bootstrap)

    p_backup = sub.add_parser("backup", parents=[common], help="Backup database")
    p_backup.add_argument("--output", default=None)
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", parents=[common], help="Restore database from backup")
    p_restore.add_argument("--backup", required=True)
    p_restore.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p_restore.set_defaults(func=cmd_restore)

    p_maint = sub.add_parser(
        "maintenance", parents=[common], help="Run maintenance (dedup, near-dup proposals)"
    )
    p_maint.add_argument("--namespace", default=None)
    p_maint.add_argument(
        "--auto-apply",
        action="store_true",
        help="Delete exactly-identical duplicates (asks for confirmation)",
    )
    p_maint.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt for --auto-apply"
    )
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
