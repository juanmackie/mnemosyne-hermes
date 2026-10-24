#!/usr/bin/env python3
"""Deterministic maintenance for a Mnemosyne memory database.

Runs under plain CPython 3.11+ with the standard library only. Every command is
safe to run against a live Hermes install: no command here needs the engine
imported, and the ones that do call the engine's own CLI in a subprocess.

Design rules (learned in production, encoded here):

* Quiet on success. Cron with ``--no-agent`` delivers stdout verbatim, so an
  empty stdout is a clean run and anything printed is worth reading.
* Fail loudly. A non-zero exit is the only signal a scheduled job has that
  something needs a human; never swallow it.
* Never invent a database. Read-only paths (``verify``, ``compact``, ``hygiene``)
  refuse to create a missing file; only ``backup`` treats "no DB yet" as a
  no-op, because a fresh install legitimately has no memory.
* Back up before you rewrite. ``compact`` takes a verified checkpoint first and
  aborts if the checkpoint is not clean.
* One owner per destructive action. ``compact`` (VACUUM) is the only command
  that rewrites the file, and it only runs above a freelist threshold.

Commands
--------
``backup``      Consistent snapshot + integrity check + retention pruning.
``verify``      quick_check + size/WAL/freelist thresholds. Read-only.
``consolidate`` Engine consolidation (``hermes mnemosyne sleep``).
``hygiene``     Engine noise audit, report only. Never deletes.
``compact``     Threshold-gated VACUUM with a verified checkpoint. Reboots WAL.
``report``      One-screen health summary (what the daily job prints on trouble).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_KEEP = 14
FREELIST_COMPACT_THRESHOLD = 0.20
FREELIST_WARN_THRESHOLD = 0.35
WAL_WARN_BYTES = 512 * 1024 * 1024
BACKUP_PREFIX = "mnemosyne-"


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def hermes_home(explicit: str | None = None) -> Path:
    for candidate in (explicit, os.environ.get("HERMES_HOME")):
        if candidate:
            return Path(candidate).expanduser()
    return Path.home() / ".hermes"


def _provider_resolver(home: Path):
    """Import the provider's own resolver so precedence can never drift.

    The repository ships the provider under ``integrations/hermes-provider``;
    an installed host keeps it at ``$HERMES_HOME/plugins/mnemosyne``. Either is
    fine — both are the same package. Returning ``None`` means the provider is
    not importable here (lite-only install), and the caller falls back.
    """
    repo_root = Path(__file__).resolve().parents[2]
    for candidate in (
        repo_root / "integrations" / "hermes-provider",
        home / "plugins" / "mnemosyne",
    ):
        if (candidate / "hermes_memory_provider" / "__init__.py").exists():
            sys_path = str(candidate)
        elif (candidate / "__init__.py").exists():  # symlinked package itself
            sys_path = str(candidate.parent)
        else:
            continue
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        break
    try:
        from hermes_memory_provider.configuration import resolve_db_path as resolve
    except Exception:
        return None
    return resolve


def _config_db_path(home: Path) -> str | None:
    """Read ``memory.mnemosyne.db_path`` without a YAML dependency.

    Deliberately independent of the provider's resolver: that resolver needs a
    YAML loader, and a plain interpreter (the one a cron wrapper may use) often
    has none. Failing over to the engine default in that case resolves a
    *different* database, which is the one failure mode this whole script exists
    to prevent. A tiny indentation-aware reader cannot disagree with the config.
    """
    try:
        lines = (home / "config.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    stack: list[tuple[int, str]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value.startswith("#"):
            value = ""
        if "#" in value:
            value = value.split("#", 1)[0].strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not value:
            stack.append((indent, key.strip()))
            continue
        path_keys = [name for _, name in stack]
        if key.strip() == "db_path" and path_keys[-2:] == ["memory", "mnemosyne"]:
            return value.strip("'\"")
    return None


def resolve_db_path(explicit: str | None, home: Path) -> Path:
    """Same precedence the provider uses: flag > config > env > engine default."""
    if explicit:
        return Path(explicit).expanduser()
    configured = _config_db_path(home)
    if configured:
        return Path(configured).expanduser()
    env = os.environ.get("MNEMOSYNE_DB_PATH")
    if env:
        return Path(env).expanduser()
    resolver = _provider_resolver(home)
    if resolver is not None:
        try:
            return Path(resolver(home))
        except Exception:
            pass
    data_dir = os.environ.get("MNEMOSYNE_DATA_DIR")
    base = Path(data_dir).expanduser() if data_dir else home / "mnemosyne" / "data"
    return base / "mnemosyne.db"


def backup_dir(explicit: str | None, home: Path) -> Path:
    for value in (explicit, os.environ.get("MNEMOSYNE_BACKUP_DIR")):
        if value:
            return Path(value).expanduser()
    return home / "backups" / "mnemosyne"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def say(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def ro_uri(path: Path) -> str:
    """Read-only SQLite URI that works on POSIX and Windows paths."""
    resolved = path.resolve().as_posix()
    if not resolved.startswith("/"):
        resolved = "/" + resolved  # C:/... -> /C:/... for the file: URI form
    return f"file:{resolved}?mode=ro"


def connect_readonly(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(db)
    return sqlite3.connect(ro_uri(db), uri=True, timeout=30)


def integrity(con: sqlite3.Connection, full: bool = False) -> tuple[bool, str]:
    pragma = "integrity_check" if full else "quick_check"
    row = con.execute(f"PRAGMA {pragma}").fetchone()
    result = row[0] if row else "no result"
    return result == "ok", result


def file_stats(db: Path) -> dict:
    """Read-only database facts. Never creates sidecars (URI mode=ro)."""
    stats: dict = {"db": str(db), "size_bytes": db.stat().st_size}
    wal = db.with_name(db.name + "-wal")
    stats["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    con = connect_readonly(db)
    try:
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()
    stats.update(
        page_count=page_count,
        freelist_count=freelist,
        page_size=page_size,
        journal_mode=journal,
        freelist_ratio=(freelist / page_count) if page_count else 0.0,
    )
    return stats


def engine_cmd(home: Path) -> list[str] | None:
    """Locate the engine CLI without importing it into this process.

    Order matters: an explicit override, then PATH, then the interpreter's own
    venv, then the venv Hermes itself lives in, then a bounded search of common
    virtualenv layouts. The engine is deliberately resolved as a binary: the
    maintenance script must keep working when the engine cannot be imported by
    whichever Python the scheduler happens to run it with.
    """
    explicit = os.environ.get("MNEMOSYNE_CLI")
    if explicit and Path(explicit).is_file():
        return [explicit]

    candidates: list[Path] = []
    which = shutil.which("mnemosyne")
    if which:
        candidates.append(Path(which))
    candidates.append(Path(sys.executable).parent / "mnemosyne")
    hermes = shutil.which("hermes")
    if hermes:
        candidates.append(Path(os.path.realpath(hermes)).parent / "mnemosyne")
    for pattern in (
        str(Path.home() / "mnemosyne-venv/bin/mnemosyne"),
        str(Path.home() / ".venvs/*/bin/mnemosyne"),
        str(home.parent / "*venv*/bin/mnemosyne"),
        str(home / "*venv*/bin/mnemosyne"),
    ):
        candidates.extend(Path(path) for path in sorted(glob.glob(pattern)))

    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)]
    return None


def run_engine(home: Path, args: list[str], db: Path) -> tuple[int, str]:
    """Run an engine CLI command against exactly one database, or refuse.

    The engine CLI resolves its target from ``MNEMOSYNE_DATA_DIR`` as
    ``<data_dir>/mnemosyne.db`` and ignores ``MNEMOSYNE_DB_PATH``. Two guards
    follow from that, and both exist because the failure they prevent is silent:

    * a database not named ``mnemosyne.db`` cannot be targeted by the CLI at all;
    * if ``<dir>/mnemosyne.db`` does not exist, the CLI will *create* an empty one
      and report success — consolidating nothing while looking healthy.
    """
    cmd = engine_cmd(home)
    if cmd is None:
        return 127, "engine CLI (mnemosyne) not found on PATH or next to this interpreter"
    if db.name != "mnemosyne.db":
        return 78, (
            f"engine CLI can only target a database named 'mnemosyne.db' (resolved '{db.name}'); "
            "use the standard layout or run the engine yourself"
        )
    if not db.exists():
        return 78, f"no database at {db}; refusing to let the engine create an empty one"
    env = dict(os.environ)
    # Pin the data directory so the CLI cannot fall back to its own default and
    # operate on a different (or brand new) database.
    env["MNEMOSYNE_DATA_DIR"] = str(db.parent)
    env["MNEMOSYNE_DB_PATH"] = str(db)
    proc = subprocess.run(
        cmd + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=1800,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def snapshot(db: Path, dest: Path) -> tuple[bool, str]:
    """Self-contained, verified copy of ``db`` at ``dest``.

    The copy inherits WAL mode from page 1; flattening it to ``DELETE`` makes
    the file usable on its own, which matters because a renamed ``.db`` without
    its ``-wal`` is a silently truncated snapshot.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = connect_readonly(db)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    for sidecar in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    check = sqlite3.connect(ro_uri(dest), uri=True)
    try:
        return integrity(check, full=True)
    finally:
        check.close()


def cmd_backup(args) -> int:
    db = resolve_db_path(args.db_path, hermes_home(args.hermes_home))
    if not db.exists():
        if args.verbose:
            say(f"backup: no database at {db}; nothing to do")
        return 0
    target_dir = backup_dir(args.backup_dir, hermes_home(args.hermes_home))
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = f"-{args.label}" if args.label else ""
    final = target_dir / f"{BACKUP_PREFIX}{stamp}{label}.db"
    partial = final.with_suffix(".partial")
    ok, result = snapshot(db, partial)
    if not ok:
        partial.unlink(missing_ok=True)
        warn(f"backup FAILED: snapshot integrity check returned {result}")
        return 1
    partial.replace(final)
    pruned = _prune(target_dir, args.keep) if not args.no_prune else []
    if args.verbose:
        say(
            f"backup: {final} ({final.stat().st_size} bytes)"
            + (f"; pruned {len(pruned)}" if pruned else "")
        )
    return 0


def _prune(target_dir: Path, keep: int) -> list[Path]:
    """Delete only snapshots this tool wrote, newest ``keep`` retained."""
    if keep <= 0:
        return []
    owned = sorted(
        (p for p in target_dir.glob(f"{BACKUP_PREFIX}*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for path in owned[keep:]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def cmd_verify(args) -> int:
    db = resolve_db_path(args.db_path, hermes_home(args.hermes_home))
    if not db.exists():
        warn(f"verify FAILED: no database at {db}")
        return 1
    con = connect_readonly(db)
    try:
        ok, result = integrity(con, full=args.full)
    finally:
        con.close()
    stats = file_stats(db)
    problems = []
    if not ok:
        problems.append(f"integrity check returned {result}")
    if stats["freelist_ratio"] > FREELIST_WARN_THRESHOLD:
        problems.append(
            f"freelist {stats['freelist_ratio']:.1%} of {stats['page_count']} pages — run `compact`"
        )
    if stats["wal_bytes"] > WAL_WARN_BYTES:
        problems.append(f"WAL is {stats['wal_bytes'] / 1024**2:.0f} MiB — checkpoint it")
    if args.json:
        say(json.dumps({**stats, "integrity": result, "problems": problems}, indent=2))
    elif problems:
        for problem in problems:
            say(f"verify: {problem}")
    elif args.verbose:
        say(
            f"verify: ok — {stats['size_bytes'] / 1024**2:.1f} MiB, "
            f"freelist {stats['freelist_ratio']:.1%}, WAL {stats['wal_bytes'] / 1024**2:.1f} MiB"
        )
    return 1 if not ok else 0


def cmd_consolidate(args) -> int:
    """Engine consolidation: working -> episodic -> BEAM tiers.

    The engine CLI runs database-wide, which is what a scheduled job needs. The
    Hermes wrapper is session-scoped, so it is only a fallback and must be told
    ``--all-sessions`` explicitly.
    """
    db = resolve_db_path(args.db_path, hermes_home(args.hermes_home))
    if not db.exists():
        if args.verbose:
            say(f"consolidate: no database at {db}; nothing to do")
        return 0
    if db.name != "mnemosyne.db":
        say(
            f"consolidate: skipped — engine tooling only targets a database named 'mnemosyne.db' "
            f"(resolved '{db.name}')"
        )
        return 0
    cmd = engine_cmd(hermes_home(args.hermes_home))
    if cmd is not None:
        rc, output = run_engine(hermes_home(args.hermes_home), ["sleep"], db)
    else:
        hermes = shutil.which("hermes")
        if not hermes:
            say("consolidate: skipped, neither the engine CLI nor hermes was found on PATH")
            return 0
        proc = subprocess.run(
            [hermes, "mnemosyne", "sleep", "--all-sessions"],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        rc, output = proc.returncode, (proc.stdout + proc.stderr).strip()
    if rc in (78, 127):
        # Reachability problem, not a memory problem: report it in the delivered
        # output but do not page every night for it.
        say(f"consolidate: skipped — {output}")
        return 0
    if rc != 0:
        warn("consolidate FAILED:")
        warn("\n".join(output.splitlines()[-8:]))
        return 1
    if args.verbose and output:
        say(f"consolidate: {output.splitlines()[-1]}")
    return 0


def cmd_hygiene(args) -> int:
    """Noise audit only. Never call ``hygiene clean`` from a scheduled job."""
    db = resolve_db_path(args.db_path, hermes_home(args.hermes_home))
    if not db.exists():
        if args.verbose:
            say(f"hygiene: no database at {db}; nothing to do")
        return 0
    rc, output = run_engine(hermes_home(args.hermes_home), ["hygiene", "audit", "--json"], db)
    if rc in (78, 127):
        say(f"hygiene: skipped — {output}")
        return 0
    if rc != 0:
        warn("hygiene FAILED:")
        warn("\n".join(output.splitlines()[-8:]))
        return 1
    candidates = _hygiene_count(output)
    if candidates is None:
        if args.verbose:
            say("hygiene: audit ran, summary not parsable")
        return 0
    if candidates > 0 or args.verbose:
        say(f"hygiene: {candidates} noise candidate(s) — review with `mnemosyne hygiene audit`")
    return 0


def _hygiene_count(output: str) -> int | None:
    """Pull a candidate count out of JSON, falling back to the text line."""
    try:
        payload = json.loads(output)
    except Exception:
        for line in output.splitlines():
            if "noise candidate" in line:
                for token in line.replace(",", " ").split():
                    if token.isdigit():
                        return int(token)
        return None
    for key in ("candidates", "total_candidates", "noise_candidates", "count"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, int):
                    return inner
    return None


def cmd_compact(args) -> int:
    """VACUUM only above the freelist threshold, with a verified checkpoint."""
    db = resolve_db_path(args.db_path, hermes_home(args.hermes_home))
    if not db.exists():
        warn(f"compact FAILED: no database at {db}")
        return 1
    stats = file_stats(db)
    ratio = stats["freelist_ratio"]
    if ratio < args.threshold and not args.force:
        if args.verbose:
            say(f"compact: skipped, freelist {ratio:.1%} below {args.threshold:.0%}")
        return 0

    free_bytes = shutil.disk_usage(db.parent).free
    needed = stats["size_bytes"] * 2
    if free_bytes < needed:
        say(
            f"compact: skipped, needs ~{needed / 1024**2:.0f} MiB free, {free_bytes / 1024**2:.0f} MiB available"
        )
        return 0

    # 1. Verified checkpoint before touching the live file.
    checkpoint = Path(args.backup_dir or backup_dir(None, hermes_home(args.hermes_home)))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = checkpoint / f"{BACKUP_PREFIX}{stamp}-precompact.db"
    ok, result = snapshot(db, snapshot_path)
    if not ok:
        warn(f"compact ABORTED: checkpoint failed integrity ({result})")
        return 1

    # 2. VACUUM. It needs an exclusive moment; a busy live DB fails cleanly and
    #    the next scheduled run picks it up. That is a skip, not an error.
    try:
        con = sqlite3.connect(db, timeout=30, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("VACUUM")
            ok, result = integrity(con, full=True)
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            say("compact: skipped, database busy (a writer held it); will retry next run")
            return 0
        warn(f"compact FAILED: {exc}")
        return 1
    if not ok:
        warn(f"compact FAILED: integrity after VACUUM returned {result} — restore {snapshot_path}")
        return 1
    after = file_stats(db)
    say(
        f"compact: {stats['size_bytes'] / 1024**2:.1f} MiB -> {after['size_bytes'] / 1024**2:.1f} MiB, "
        f"freelist {ratio:.1%} -> {after['freelist_ratio']:.1%}; checkpoint {snapshot_path.name}"
    )
    if not args.no_prune:
        _prune(checkpoint, args.keep)
    return 0


def cmd_report(args) -> int:
    db = resolve_db_path(args.db_path, hermes_home(args.hermes_home))
    if not db.exists():
        say(f"mnemosyne: no database at {db} (nothing stored yet)")
        return 0
    con = connect_readonly(db)
    try:
        ok, result = integrity(con)
        counts = {}
        for table in ("memories", "working_memory", "episodic_memory"):
            try:
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                continue
    finally:
        con.close()
    stats = file_stats(db)
    rows = ", ".join(f"{name}={count}" for name, count in counts.items()) or "no known tables"
    say(
        f"mnemosyne db={db} size={stats['size_bytes'] / 1024**2:.1f}MiB "
        f"freelist={stats['freelist_ratio']:.1%} wal={stats['wal_bytes'] / 1024**2:.1f}MiB "
        f"integrity={result} rows({rows})"
    )
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db-path", help="explicit database path (default: provider resolution)")
    parser.add_argument("--hermes-home", help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    parser.add_argument(
        "--backup-dir", help="backup directory (default: $HERMES_HOME/backups/mnemosyne)"
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"snapshots to retain (default {DEFAULT_KEEP})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="print success detail too")
    # Repeat the common flags on every subcommand so both `script -v verify` and
    # `script verify -v` work. SUPPRESS keeps the top-level value from being reset.
    common = argparse.ArgumentParser(add_help=False)
    for flags, kwargs in (
        (("--db-path",), {"help": argparse.SUPPRESS}),
        (("--hermes-home",), {"help": argparse.SUPPRESS}),
        (("--backup-dir",), {"help": argparse.SUPPRESS}),
        (("--keep",), {"type": int, "help": argparse.SUPPRESS}),
        (("--verbose", "-v"), {"action": "store_true", "help": argparse.SUPPRESS}),
    ):
        common.add_argument(*flags, default=argparse.SUPPRESS, **kwargs)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser(
        "backup", parents=[common], help="consistent snapshot + integrity check + retention"
    )
    backup.add_argument("--label", help="suffix for the snapshot name, e.g. daily")
    backup.add_argument("--no-prune", action="store_true", help="keep every snapshot")

    verify = sub.add_parser(
        "verify", parents=[common], help="integrity and size thresholds (read-only)"
    )
    verify.add_argument(
        "--full", action="store_true", help="full integrity_check instead of quick_check"
    )
    verify.add_argument("--json", action="store_true")

    sub.add_parser(
        "consolidate", parents=[common], help="engine consolidation (tiers, dedup indexes)"
    )

    sub.add_parser("hygiene", parents=[common], help="engine noise audit (report only)")

    compact = sub.add_parser(
        "compact", parents=[common], help="threshold-gated VACUUM with checkpoint"
    )
    compact.add_argument("--threshold", type=float, default=FREELIST_COMPACT_THRESHOLD)
    compact.add_argument("--force", action="store_true", help="compact regardless of freelist")
    compact.add_argument("--no-prune", action="store_true")

    sub.add_parser("report", parents=[common], help="one-screen health summary")

    for name in ("backup", "verify", "consolidate", "hygiene", "compact", "report"):
        sub.choices[name].set_defaults(func=globals()[f"cmd_{name}"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # An explicit home that does not exist is a typo, not an empty install: the
    # provider resolver would otherwise fall through to MNEMOSYNE_DATA_DIR and
    # silently maintain a different database.
    if args.hermes_home and not Path(args.hermes_home).expanduser().is_dir():
        warn(f"{args.command} FAILED: --hermes-home {args.hermes_home} is not a directory")
        return 2
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        warn(f"{args.command} FAILED: no database at {exc}")
        return 1
    except sqlite3.Error as exc:
        warn(f"{args.command} FAILED: sqlite error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
