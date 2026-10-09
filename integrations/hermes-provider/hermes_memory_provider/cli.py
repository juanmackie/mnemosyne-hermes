"""CLI commands for Mnemosyne memory provider.

Available via: hermes mnemosyne <subcommand>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# T7: see the matching guard in __init__.py. Only add the repo sibling dir when
# it really holds the package; a copied install must not shadow the engine.
_mnemosyne_root = Path(__file__).resolve().parent.parent
if (_mnemosyne_root / "hermes_memory_provider").is_dir() and str(_mnemosyne_root) not in sys.path:
    sys.path.insert(0, str(_mnemosyne_root))

# Supported Hermes range (T8): `hermes mnemosyne ...` depends on Hermes' plugin
# CLI discovery internals, so the range is a contract, not a preference.
# LOCAL PATCH (P14): only 0.18.2 and 0.19.0 were available and audited;
# do not claim support for unpublished/unverified 0.20+ releases.
SUPPORTED_HERMES_RANGE = ">=0.18,<0.20"
TESTED_HERMES_VERSIONS = ("0.18.2", "0.19.0")


def detect_hermes_version():
    """Best-effort Hermes version; None when undetectable."""
    try:
        import importlib.metadata as _md
        return _md.version("hermes-agent")
    except Exception:
        pass
    try:
        # LOCAL PATCH (P12): import_module rather than `import agent`. Same
        # lookup at runtime, but the `agent` module only exists inside a Hermes
        # install, so a static import here reads as a hard dependency on a
        # module this package does not declare.
        import importlib as _il
        return getattr(_il.import_module("agent"), "__version__", None)
    except Exception:
        return None


def check_hermes_version(version):
    """Return (ok, message) for a detected Hermes version (T8)."""
    tested = ", ".join(TESTED_HERMES_VERSIONS)
    if not version:
        return False, (
            f"could not detect Hermes version; supported range is {SUPPORTED_HERMES_RANGE} "
            f"(tested {tested})"
        )
    try:
        parts = tuple(int(x) for x in str(version).split(".")[:2])
    except (TypeError, ValueError):
        parts = ()
    if len(parts) == 2 and (0, 18) <= parts < (0, 20):
        return True, f"Hermes {version} is within the supported range {SUPPORTED_HERMES_RANGE}"
    return False, (
        f"Hermes {version} is outside the supported range {SUPPORTED_HERMES_RANGE} "
        f"(tested {tested})"
    )


def engine_version():
    try:
        import mnemosyne
        return getattr(mnemosyne, "__version__", "unknown")
    except Exception:
        return "unknown"


def _read_configured_db_path(hermes_home):
    try:
        from mnemosyne.hermes_config import read_hermes_config_key
        val = read_hermes_config_key(hermes_home, "db_path")
        if val:
            return str(Path(str(val)).expanduser())
    except Exception:
        pass
    env = os.environ.get("MNEMOSYNE_DB_PATH")
    if env:
        return str(Path(env).expanduser())
    return None


def resolve_effective_db_path(hermes_home=None):
    """Resolve the DB path the provider will use (T3 + T6).

    Precedence: memory.mnemosyne.db_path > MNEMOSYNE_DB_PATH > engine default
    (MNEMOSYNE_DATA_DIR > $HERMES_HOME > ~/.hermes).
    """
    home = hermes_home or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    explicit = _read_configured_db_path(home)
    if explicit:
        return explicit
    try:
        from mnemosyne.core.beam import _default_db_path
        return str(_default_db_path())
    except Exception:
        return None


def describe_memory_location(db_path, hermes_home):
    """Return header lines naming where memory actually lives (T6)."""
    home = hermes_home or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    lines = [
        f"memory DB: {db_path or 'unresolved'}",
        f"provider package: {Path(__file__).resolve().parent}",
        f"engine version: {engine_version()}",
        f"HERMES_HOME: {home}",
    ]
    if db_path:
        try:
            db = Path(str(db_path)).expanduser().resolve()
            home_path = Path(str(home)).expanduser().resolve()
            if home_path not in db.parents:
                lines.append(
                    "WARNING: memory DB is OUTSIDE $HERMES_HOME; logs and memory are split. "
                    "Unset MNEMOSYNE_DATA_DIR/MNEMOSYNE_DB_PATH or set memory.mnemosyne.db_path "
                    "under $HERMES_HOME."
                )
        except Exception:
            pass
    return lines


def _file_digest(path):
    """sha256 as base64url without padding — the wheel RECORD / manifest format."""
    import base64
    import hashlib

    data = hashlib.sha256(Path(path).read_bytes()).digest()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _copy_payload_digests(plugin_path):
    """Hash payload files, excluding only the root marker and runtime caches."""
    import os

    root = Path(plugin_path)
    files = {}
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise ValueError(f"provider copy contains a symlink: {current_path / directory}")
        directories[:] = [name for name in directories if name != "__pycache__"]
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"provider copy contains a non-regular file: {path}")
            relative = path.relative_to(root).as_posix()
            if relative == "PROVENANCE.json":
                continue
            files[relative] = _file_digest(path)
    return files


def _verified_copy(plugin_path, expected_source=None):
    """Return (ok, detail) for a complete, unchanged installer-created copy.

    Windows cannot create a directory symlink without Developer Mode, so
    `install.sh --copy` writes a versioned digest inventory for every package
    file. `__pycache__` directories and only the root marker are excluded.
    Python adds bytecode caches at runtime; top-level `.pyc` and nested
    marker-named files are included. The inventory also rejects missing,
    additional, and symlinked payload files.
    """
    # LOCAL PATCH (P13): verified copies must cover the complete importable
    # package, not just __init__.py and cli.py.
    marker = Path(plugin_path) / "PROVENANCE.json"
    if marker.is_symlink() or not marker.is_file():
        return False, (
            f"plugin target {plugin_path} is not the canonical provider "
            "(expected .../integrations/hermes-provider/hermes_memory_provider) "
            "and has no regular PROVENANCE.json, so it cannot be verified as a copy. "
            "Run `./install.sh --copy` to create one."
        )
    try:
        import json

        recorded = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(recorded, dict) or recorded.get("format_version") != 1:
            return False, f"{marker} uses an unsupported provenance format; reinstall with ./install.sh --copy"
        files = recorded.get("files")
        if not isinstance(files, dict) or not files or not all(
            isinstance(name, str) and isinstance(digest, str)
            for name, digest in files.items()
        ):
            return False, f"{marker} records no valid file digests"
        if expected_source is not None:
            source = Path(recorded.get("copied_from", "")).resolve()
            if source != Path(expected_source).resolve():
                return False, f"{marker} was not created from the current canonical provider source"
        actual_files = _copy_payload_digests(plugin_path)
    except Exception as e:
        return False, f"{marker} is unreadable or invalid: {e}"

    if set(files) != set(actual_files):
        missing = sorted(set(files) - set(actual_files))
        added = sorted(set(actual_files) - set(files))
        return False, f"{marker} payload inventory changed (missing={missing}, added={added})"
    for name, expected in files.items():
        actual = actual_files[name]
        if actual != expected:
            return False, (
                f"{Path(plugin_path) / name} does not match the digest recorded in {marker} "
                f"({actual} != {expected}). The copy was edited after it was "
                "installed; re-run ./install.sh --copy."
            )
    return True, f"a verified copy of the canonical provider (from {recorded.get('copied_from', 'unknown')})"


def check_provider_provenance(plugin_path):
    """Return (ok, message) for a $HERMES_HOME/plugins/mnemosyne target (T7)."""
    resolved = Path(os.path.realpath(str(plugin_path)))
    parts = resolved.parts
    if "integrations" in parts:
        idx = parts.index("integrations")
        if idx + 1 < len(parts) and parts[idx + 1] == "hermes":
            return False, (
                f"plugin target {resolved} is the RETIRED provider tree (integrations/hermes)"
            )

    canonical = (
        resolved.name == "hermes_memory_provider"
        and resolved.parent.name == "hermes-provider"
        and resolved.parent.parent.name == "integrations"
    )
    if canonical:
        origin = "the canonical provider"
    else:
        # LOCAL PATCH (P13): a copy is an acceptable install where symlinks are
        # not available, as long as it is the copy this repo made and has not
        # been edited since. Without this, `install.sh --copy` produced an
        # install that doctor refused.
        ok, detail = _verified_copy(resolved)
        if not ok:
            return False, detail
        origin = detail

    init_py = resolved / "__init__.py"
    cli_py = resolved / "cli.py"
    if not init_py.is_file():
        return False, f"provider {resolved} has no __init__.py"
    init_text = init_py.read_text(encoding="utf-8", errors="replace")
    if "def register_memory_provider" not in init_text or "def register(" not in init_text:
        return False, f"provider {resolved} is missing register()/register_memory_provider()"
    cli_text = cli_py.read_text(encoding="utf-8", errors="replace") if cli_py.is_file() else ""
    for fn in ("register_cli", "mnemosyne_command"):
        if f"def {fn}" not in cli_text:
            return False, f"provider cli.py is missing {fn} (Hermes CLI handler contract)"
    return True, f"plugin target {resolved} is {origin}"


def _provider_registration_count():
    """Count providers a single register call yields; (count, detail)."""
    try:
        try:
            from . import register_memory_provider
        except ImportError:
            from hermes_memory_provider import register_memory_provider
    except Exception as e:
        return None, f"could not import provider registration: {e}"
    box = []

    class _Ctx:
        def register_memory_provider(self, p):
            box.append(p)

    try:
        register_memory_provider(_Ctx())
    except Exception as e:
        return None, f"register_memory_provider raised: {e}"
    return len(box), ""


def _writable(path):
    """os.access(W_OK) as a plain bool; an unusable path is 'not writable'.

    os.access raises rather than returning False for a path it cannot even
    encode (an embedded NUL, for instance). doctor must report a bad DB path as
    a failed check, not die with a traceback.
    """
    try:
        return os.access(path, os.W_OK)
    except (OSError, ValueError):
        return False


def _db_writable(db_path):
    # LOCAL PATCH (P10 amendment): a fresh install has no data directory yet —
    # the installer deliberately creates nothing — so an absent parent is not a
    # failure. Walk up to the nearest existing ancestor and check that one.
    if not db_path:
        return False, "DB path could not be resolved"
    p = Path(str(db_path)).expanduser()
    if p.exists():
        return _writable(p), f"{p} (file)"
    ancestor = p.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.exists():
        return False, f"{p} (no existing ancestor directory)"
    if ancestor == p.parent:
        return _writable(ancestor), f"{p} (parent {ancestor}, not created yet)"
    return (
        _writable(ancestor),
        f"{p} (will be created; nearest existing ancestor {ancestor})",
    )


def _db_integrity(db_path):
    if not db_path:
        return False, "DB path could not be resolved"
    p = Path(str(db_path)).expanduser()
    if not p.exists():
        return True, "not created yet"
    try:
        import sqlite3
        con = sqlite3.connect(str(p))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            val = row[0] if row else "no result"
            return bool(val == "ok"), str(val)
        finally:
            con.close()
    except Exception as e:
        return False, str(e)


def register_cli(subparser):
    """Register CLI subcommands for ``hermes mnemosyne``."""
    mn_cmds = subparser.add_subparsers(dest="mnemosyne_cmd")

    stats_cmd = mn_cmds.add_parser("stats", help="Show memory statistics")
    stats_cmd.add_argument("--global", "-g", action="store_true", help="Show global stats across all sessions")

    sleep_cmd = mn_cmds.add_parser("sleep", help="Run consolidation cycle")
    sleep_cmd.add_argument("--all-sessions", action="store_true", help="Consolidate eligible old working memories across all sessions")
    sleep_cmd.add_argument("--dry-run", action="store_true", help="Report what would be consolidated without writing changes")
    mn_cmds.add_parser("version", help="Show Mnemosyne version")

    inspect_cmd = mn_cmds.add_parser("inspect", help="Search memories")
    inspect_cmd.add_argument("query", nargs="?", default="", help="Search query")
    inspect_cmd.add_argument("--limit", type=int, default=10, help="Max results")
    # LOCAL PATCH: P27 by-ID inspection is a bounded read-only snapshot. Keep
    # the existing positional keyword-search form intact.
    inspect_cmd.add_argument("--id", dest="memory_id", help="Inspect one memory by ID")
    inspect_cmd.add_argument("--session-id", default="hermes_default", help="Session whose memories are visible")
    inspect_cmd.add_argument("--all-sessions", action="store_true", help="Explicit local-operator access across sessions")
    inspect_cmd.add_argument("--format", choices=("json",), help="By-ID inspection format (JSON)")

    history_cmd = mn_cmds.add_parser("history", help="Show partial audit history for one memory ID")
    history_cmd.add_argument("--id", dest="memory_id", required=True, help="Memory ID")
    history_cmd.add_argument("--session-id", default="hermes_default", help="Session whose events are visible")
    history_cmd.add_argument("--all-sessions", action="store_true", help="Explicit local-operator access across sessions")
    history_cmd.add_argument("--profile", help="Narrow audit events to a stored profile label")
    history_cmd.add_argument("--limit", type=int, default=50, help="Maximum events (1-100)")
    history_cmd.add_argument("--format", choices=("json", "text"), default="json", help="Output format")

    mn_cmds.add_parser("clear", help="Clear scratchpad")

    doctor_cmd = mn_cmds.add_parser("doctor", help="Run diagnostics and auto-fix missing dependencies")
    doctor_cmd.add_argument("--dry-run", action="store_true", help="Show what would be fixed without installing")
    doctor_cmd.add_argument("--no-fix", action="store_true", help="Diagnose only, do not fix")

    # LOCAL PATCH: P27 add a bounded visibility-filtered Markdown projection;
    # JSON remains the existing engine export format by default.
    export_cmd = mn_cmds.add_parser("export", help="Export memories to JSON or Markdown")
    export_cmd.add_argument("--output", "-o", type=str, required=True, help="Output file path")
    export_cmd.add_argument("--format", choices=("json", "markdown"), default="json", help="Export format")
    export_cmd.add_argument("--session-id", default="hermes_default", help="Session whose memories are visible")
    export_cmd.add_argument("--all-sessions", action="store_true", help="Explicit local-operator export across sessions")
    export_cmd.add_argument("--limit", type=int, default=200, help="Maximum Markdown records (1-200)")

    import_cmd = mn_cmds.add_parser("import", help="Import memories from a JSON file or another provider")
    import_cmd.add_argument("--input", "-i", type=str, help="Input JSON file path (for file imports)")
    import_cmd.add_argument("--file", type=str, help="Provider file input, e.g. Hindsight JSON export")
    import_cmd.add_argument("--force", action="store_true", help="Overwrite existing records (file import)")
    import_cmd.add_argument("--from", dest="from_provider", type=str, help="Provider to import from (e.g., 'mem0')")
    import_cmd.add_argument("--api-key", type=str, help="Provider API key (or set env var)")
    import_cmd.add_argument("--user-id", type=str, help="Filter by user ID (provider-specific)")
    import_cmd.add_argument("--agent-id", type=str, help="Filter by agent ID (provider-specific)")
    import_cmd.add_argument("--base-url", type=str, help="Provider base URL (for self-hosted)")
    import_cmd.add_argument("--bank", type=str, help="Provider memory bank, e.g. Hindsight bank")
    import_cmd.add_argument("--dry-run", action="store_true", help="Validate but don't import")
    import_cmd.add_argument("--session-id", type=str, help="Override session for imported memories")
    import_cmd.add_argument("--channel-id", type=str, help="Channel for imported memories")
    import_cmd.add_argument("--list-providers", action="store_true", help="List supported import providers")
    import_cmd.add_argument("--generate-script", action="store_true", help="Generate a migration script for the provider")
    import_cmd.add_argument("--agentic", action="store_true", help="Generate agent migration instructions (prompt to give your AI agent)")
    import_cmd.add_argument("--output-script", type=str, help="Save generated script to file")
    import_cmd.add_argument("--db-path", type=str, help="Holographic memory store path (default: ~/.hermes/memory_store.db)")
    import_cmd.add_argument("--min-trust", type=float, default=0.0, help="Minimum trust score threshold 0.0-1.0 (holographic only)")

    subparser.set_defaults(func=mnemosyne_command)


def _inspection_db_path() -> Path:
    """Resolve an existing provider DB without opening it for writes."""
    resolved = resolve_effective_db_path()
    if not resolved:
        raise RuntimeError("could not resolve the Mnemosyne database path")
    path = Path(resolved).expanduser()
    if not path.is_file():
        raise RuntimeError("Mnemosyne database does not exist; inspection will not create it")
    return path


def _bounded_limit(args: Any, default: int) -> int:
    limit = getattr(args, "limit", default)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("--limit must be between 1 and 100")
    return limit


def _readonly_inspect(args: Any) -> int:
    """Print one visible current memory via a validated read-only snapshot."""
    try:
        if getattr(args, "query", ""):
            raise ValueError("inspect accepts either a query or --id, not both")
        path = _inspection_db_path()
        from hermes_memory_provider.evidence import read_snapshot, source_reference

        rows = read_snapshot(
            path,
            session_id=getattr(args, "session_id", "hermes_default"),
            all_sessions=bool(getattr(args, "all_sessions", False)),
            memory_id=str(args.memory_id),
            limit=1,
        )
        row = rows[0] if rows else None
        if row is not None:
            row = dict(row)
            content = str(row.get("content") or "")
            row["content_truncated"] = bool(row.get("content_truncated")) or len(content) > 4000
            row["content"] = content if len(content) <= 4000 else content[:3999] + "…"
            row["source"] = str(row.get("source") or "")[:128]
            lineage = row.get("summary_of") or []
            row["lineage_truncated"] = bool(row.get("lineage_truncated")) or len(lineage) > 20
            row["summary_of"] = lineage[:20]
            row.pop("metadata", None)
            row["source_reference"] = source_reference(rows[0].get("metadata"))
        print(json.dumps({
            "status": "found" if row else "not_found",
            "coverage": "current record only; audit history may be partial",
            "memory": row,
        }, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"Inspection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _readonly_history(args: Any) -> int:
    """Print bounded, ownership-filtered audit events for a memory ID."""
    try:
        path = _inspection_db_path()
        limit = _bounded_limit(args, 50)
        from hermes_memory_provider.audit import read_audit_history

        events = read_audit_history(
            path,
            memory_id=str(args.memory_id),
            session_id=getattr(args, "session_id", "hermes_default"),
            all_sessions=bool(getattr(args, "all_sessions", False)),
            profile=getattr(args, "profile", None),
            limit=limit,
        )
        output_format = getattr(args, "format", "json")
        if output_format == "text":
            print(f"Partial audit history for {args.memory_id} ({len(events)} event(s))")
            for event in events:
                metadata = event.get("metadata_json")
                try:
                    metadata = json.loads(metadata) if metadata else {}
                except (TypeError, ValueError):
                    metadata = {"status": "invalid metadata"}
                fields = (
                    event.get("timestamp"), event.get("action"), event.get("source_tool"),
                    event.get("scope"), event.get("session_id"), event.get("profile"),
                )
                print("  " + " | ".join("" if value is None else str(value)[:128] for value in fields))
                if metadata:
                    print("    metadata: " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))[:512])
        else:
            print(json.dumps({
                "memory_id": str(args.memory_id),
                "coverage": "partial audit history; not a complete or undoable timeline",
                "count": len(events),
                "events": events,
            }, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"History failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def mnemosyne_command(args):
    """Dispatch ``hermes mnemosyne <subcommand>``."""
    cmd = getattr(args, "mnemosyne_cmd", None)
    if not cmd:
        print("Usage: hermes mnemosyne {stats|sleep|version|inspect|history|clear|export|import}")
        return 1

    # LOCAL PATCH: P27 inspection must never construct BeamMemory because its
    # initializer may create or migrate tables in a mistyped/empty database.
    if cmd == "inspect" and getattr(args, "memory_id", None):
        return _readonly_inspect(args)
    if cmd == "inspect" and getattr(args, "all_sessions", False):
        print("Error: --all-sessions is only available with inspect --id", file=sys.stderr)
        raise SystemExit(1)
    if cmd == "history":
        return _readonly_history(args)

    # Register Hermes host LLM backend so sleep uses Hermes' provider.
    # Use a try/except fallback chain: the relative import works when loaded
    # as part of the hermes_memory_provider package; the absolute import is
    # needed when this module is loaded standalone (e.g. Hermes user-plugin
    # discovery via importlib.util.spec_from_file_location, which does not
    # set up the parent package — breaking relative imports silently).
    try:
        try:
            from .hermes_llm_adapter import register_hermes_host_llm
        except ImportError:
            from hermes_memory_provider.hermes_llm_adapter import register_hermes_host_llm
        register_hermes_host_llm()
    except Exception:
        pass

    # T4: doctor reports on its own and must not fail early when the engine is
    # missing -- the Hermes CLI ignores a handler's return value, so an early
    # `return 1` would exit 0. version/list-providers also need no beam.
    needs_beam = cmd in ("stats", "sleep", "inspect", "clear")
    # LOCAL PATCH (P12): annotate instead of inferring. `beam` is None on the
    # commands that need no engine, and `_beam_kwargs` mixes a str session id
    # with the resolved db path, so an inferred `dict[str, str]` made the
    # BeamMemory call look like a type error and every later `beam.<method>`
    # look like an attribute access on None. No runtime change.
    beam: Any = None
    if needs_beam:
        try:
            from mnemosyne.core.beam import BeamMemory
            _resolved_db_path = resolve_effective_db_path()
            _beam_kwargs: dict[str, Any] = {"session_id": "hermes_default"}
            if _resolved_db_path:
                _beam_kwargs["db_path"] = _resolved_db_path
            beam = BeamMemory(**_beam_kwargs)
        except Exception as e:
            print(f"Error: Mnemosyne not available: {e}")
            # The CLI ignores return values; raise so the process fails loud.
            raise SystemExit(1)

    if cmd == "stats":
        if getattr(args, "global", False):
            working = beam.get_global_working_stats()
        else:
            working = beam.get_working_stats()
        episodic = beam.get_episodic_stats()
        memoria = beam.get_memoria_stats()
        print(json.dumps({"working": working, "episodic": episodic, "memoria": memoria}, indent=2))

    elif cmd == "version":
        from mnemosyne import __version__
        print(f"Mnemosyne v{__version__}")

    elif cmd == "sleep":
        dry_run = bool(getattr(args, "dry_run", False))
        if getattr(args, "all_sessions", False):
            result = beam.sleep_all_sessions(dry_run=dry_run)
        else:
            result = beam.sleep(dry_run=dry_run)
        print(json.dumps(result, indent=2))

    elif cmd == "inspect":
        query = getattr(args, "query", "") or ""
        limit = getattr(args, "limit", 10)
        if not query:
            query = input("Search query: ")
        results = beam.recall(query, top_k=limit)
        print(f"Results for '{query}': {len(results)}")
        for i, r in enumerate(results, 1):
            content = r.get("content", "")[:120]
            imp = r.get("importance", 0.0)
            print(f"  {i}. [{imp:.2f}] {content}")

    elif cmd == "clear":
        confirm = input("Clear scratchpad? This cannot be undone. [y/N]: ")
        if confirm.lower() in ("y", "yes"):
            beam.scratchpad_clear()
            print("Scratchpad cleared.")
        else:
            print("Cancelled.")

    elif cmd == "doctor":
        dry_run = bool(getattr(args, "dry_run", False))
        no_fix = bool(getattr(args, "no_fix", False))
        hermes_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        db_path = resolve_effective_db_path(hermes_home)

        # T4: explicit critical checks gate the exit code. The engine's own
        # diagnostics stay informational so a partial report can never be read
        # as a pass ("Checks passed: 16/43" used to exit 0).
        critical = []  # (label, ok, detail)
        try:
            import mnemosyne.core.beam  # noqa: F401
            critical.append(("engine importable", True, f"v{engine_version()}"))
        except Exception as e:
            critical.append(("engine importable", False, str(e)))
        count, detail = _provider_registration_count()
        if count is None:
            critical.append(("provider registered exactly once", False, detail))
        else:
            critical.append((
                "provider registered exactly once", count == 1,
                f"{count} provider(s) registered",
            ))
        ok_db, db_detail = _db_writable(db_path)
        critical.append(("DB resolved + writable", ok_db, db_detail))
        ok_int, int_detail = _db_integrity(db_path)
        critical.append(("DB integrity", ok_int, int_detail))
        plugin_link = Path(hermes_home) / "plugins" / "mnemosyne"
        if plugin_link.exists():
            ok_prov, prov_msg = check_provider_provenance(plugin_link)
            critical.append(("canonical provider deployed", ok_prov, prov_msg))
        else:
            critical.append((
                "canonical provider deployed", False,
                f"{plugin_link} does not exist; run ./install.sh",
            ))
        critical_ok = all(ok for _, ok, _ in critical)

        print("\nMnemosyne Diagnostics")
        print("=" * 40)
        for line in describe_memory_location(db_path, hermes_home):
            print(f"  {line}")
        _hv = detect_hermes_version()
        _ok_hv, _hv_msg = check_hermes_version(_hv)
        print(f"  Hermes version: {_hv or 'unknown'} [{'ok' if _ok_hv else 'WARN'}]")
        if not _ok_hv:
            print(f"    {_hv_msg}")
        print("  Critical checks:")
        for label, ok, item_detail in critical:
            mark = "PASS" if ok else "FAIL"
            suffix = f" — {item_detail}" if item_detail else ""
            print(f"    [{mark}] {label}{suffix}")

        if not critical_ok:
            # T4: the Hermes CLI ignores a handler's return value; only a raised
            # SystemExit makes `hermes mnemosyne doctor` exit non-zero.
            raise SystemExit(1)

        try:
            from mnemosyne.diagnose import run_diagnostics, auto_fix
            result = run_diagnostics()
            print(f"\n  Engine checks passed: {result.get('checks_passed', 0)}/{result.get('checks_total', 0)}")
            if result.get("key_findings"):
                print("\n  Key findings:")
                for finding in result["key_findings"]:
                    print(f"    - {finding}")
            else:
                print("\n  No issues detected.")

            if not no_fix:
                print("\n--- Auto-fix ---")
                fix_result = auto_fix(result.get("entries", []), dry_run=dry_run)
                if fix_result["fixed"]:
                    for item in fix_result["fixed"]:
                        print(f"  ✅ {item}")
                if fix_result["failed"]:
                    for item in fix_result["failed"]:
                        print(f"  ❌ {item['label']}: {item['error']}")
                if not fix_result["fixed"] and not fix_result["failed"]:
                    print("  Nothing to fix - all dependencies are healthy.")
            print(f"\nFull log: {result.get('log_path', 'unknown')}")
        except Exception as e:
            # LOCAL PATCH: say what to install. Upstream printed only the raw
            # exception, leaving a public user with "No module named 'mnemosyne'"
            # and no next step.
            print(f"\nDiagnostic failed: {e}")
            print(
                "The mnemosyne-memory engine is required by this provider "
                "(pinned >=3.15.1,<3.16). Install it into the Hermes venv, e.g.\n"
                "  uv pip install 'mnemosyne-memory[embeddings]>=3.15.1,<3.16'"
            )
            raise SystemExit(1)

        return 0

    elif cmd == "export":
        output_path = getattr(args, "output", None)
        if not output_path:
            print("Usage: hermes mnemosyne export --output <path>")
            return 1
        if getattr(args, "format", "json") == "markdown":
            try:
                path = _inspection_db_path()
                limit = getattr(args, "limit", 200)
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
                    raise ValueError("--limit must be between 1 and 200")
                from hermes_memory_provider.evidence import export_markdown
                result = export_markdown(
                    path,
                    output_path,
                    session_id=getattr(args, "session_id", "hermes_default"),
                    all_sessions=bool(getattr(args, "all_sessions", False)),
                    limit=limit,
                )
                print(f"Exported {result.get('shown', 0)} visible memories to {output_path} (Markdown)")
                if result.get("truncated"):
                    print(f"Output capped at {limit} records")
                return 0
            except Exception as e:
                print(f"Export failed: {e}")
                raise SystemExit(1) from e
        try:
            from mnemosyne.core.memory import Mnemosyne
            _db = resolve_effective_db_path()
            mem = Mnemosyne(session_id="hermes_default", **({"db_path": _db} if _db else {}))
            result = mem.export_to_file(output_path)
            print(f"Exported {result['working_memory_count']} working, {result['episodic_memory_count']} episodic, {result['legacy_memories_count']} legacy, {result['triples_count']} triples to {output_path}")
        except Exception as e:
            print(f"Export failed: {e}")
            return 1

    elif cmd == "import":
        # --list-providers
        if getattr(args, "list_providers", False):
            from mnemosyne.core.importers import PROVIDERS
            print("Supported import providers:")
            for name, info in PROVIDERS.items():
                print(f"  {name}: {info['description']}")
                print(f"         docs: {info['docs']}")
                print(f"         env key: {info['env_key']}")
                print(f"         pip: {info['pypi_package']}")
            return 0

        # --agentic: generate instructions for user's AI agent
        generate_script_flag = getattr(args, "generate_script", False)
        agentic_flag = getattr(args, "agentic", False)
        from_provider = getattr(args, "from_provider", None)
        output_script = getattr(args, "output_script", None)

        if agentic_flag and from_provider:
            from mnemosyne.core.importers.agentic import generate_agent_instructions
            instructions = generate_agent_instructions(from_provider)
            if output_script:
                Path(output_script).write_text(instructions)
                print(f"Agent instructions saved to {output_script}")
            else:
                print(instructions)
            return 0

        if generate_script_flag and from_provider:
            from mnemosyne.core.importers.agentic import generate_migration_script
            api_key = getattr(args, "api_key", None)
            user_id = getattr(args, "user_id", None)
            script = generate_migration_script(
                from_provider,
                api_key=api_key or "",
                user_id=user_id or "",
            )
            if output_script:
                Path(output_script).write_text(script)
                print(f"Migration script saved to {output_script}")
            else:
                print(script)
            return 0

        cross_provider = from_provider
        input_path = getattr(args, "input", None)
        dry_run = getattr(args, "dry_run", False)
        session_id = getattr(args, "session_id", None)
        channel_id = getattr(args, "channel_id", None)

        try:
            from mnemosyne.core.memory import Mnemosyne
            _db = resolve_effective_db_path()
            mem = Mnemosyne(session_id=session_id or "import_session",
                            channel_id=channel_id,
                            **({"db_path": _db} if _db else {}))
        except Exception as e:
            print(f"Error: Mnemosyne not available: {e}")
            return 1

        # Cross-provider import
        if cross_provider:
            api_key = getattr(args, "api_key", None)
            user_id = getattr(args, "user_id", None)
            agent_id = getattr(args, "agent_id", None)
            base_url = getattr(args, "base_url", None)

            def _print_import_result(result):
                print(f"\nImport complete:")
                print(f"  Total found: {result.total}")
                print(f"  Imported:    {result.imported}")
                print(f"  Skipped:     {result.skipped}")
                print(f"  Failed:      {result.failed}")
                if result.errors:
                    print(f"  Errors:")
                    for err in result.errors[:10]:
                        print(f"    - {err}")
                    if len(result.errors) > 10:
                        print(f"    ... and {len(result.errors) - 10} more")

            if cross_provider == "hindsight":
                file_path = getattr(args, "file", None) or input_path
                bank = getattr(args, "bank", None) or "hermes"
                if not file_path and not base_url:
                    print("Error: Hindsight import requires --file/--input or --base-url.")
                    return 1

                print("Importing from hindsight...")
                if dry_run:
                    print("  (dry-run mode: no memories will be written)")

                try:
                    from mnemosyne.core.importers import import_from_provider
                    import_kwargs = {
                        "file_path": file_path,
                        "base_url": base_url,
                        "bank": bank,
                        "dry_run": dry_run,
                        "session_id": session_id,
                        "channel_id": channel_id,
                    }
                    result = import_from_provider(
                        "hindsight", mem,
                        **import_kwargs,
                    )
                    _print_import_result(result)
                    return 0 if result.failed == 0 else 1
                except ValueError as e:
                    print(f"Error: {e}")
                    return 1
                except Exception as e:
                    print(f"Import failed: {e}")
                    return 1

            if cross_provider == "holographic":
                db_path = getattr(args, "db_path", None)
                min_trust = getattr(args, "min_trust", 0.0)

                print("Importing from holographic memory...")
                if dry_run:
                    print("  (dry-run mode: no memories will be written)")

                try:
                    from mnemosyne.core.importers import import_from_provider
                    import_kwargs = {
                        "db_path": db_path,
                        "min_trust": min_trust,
                        "dry_run": dry_run,
                        "session_id": session_id,
                        "channel_id": channel_id,
                    }
                    result = import_from_provider(
                        "holographic", mem,
                        **import_kwargs,
                    )
                    _print_import_result(result)
                    return 0 if result.failed == 0 else 1
                except ValueError as e:
                    print(f"Error: {e}")
                    return 1
                except Exception as e:
                    print(f"Import failed: {e}")
                    return 1

            # Try env var fallback
            # (module-level `import os` covers this; a local re-import here
            # made `os` function-local and broke earlier os.environ reads.)
            if not api_key:
                info = __import__("mnemosyne.core.importers", fromlist=["PROVIDERS"]).PROVIDERS
                pk = info.get(cross_provider, {}).get("env_key", "")
                if pk:
                    api_key = os.environ.get(pk)
            if not api_key:
                print(f"Error: --api-key required for {cross_provider} import. "
                      f"Or set the {cross_provider.upper()}_API_KEY env var.")
                return 1

            print(f"Importing from {cross_provider}...")
            if dry_run:
                print("  (dry-run mode: no memories will be written)")

            try:
                from mnemosyne.core.importers import import_from_provider
                result = import_from_provider(
                    cross_provider, mem,
                    api_key=api_key,
                    user_id=user_id,
                    agent_id=agent_id,
                    base_url=base_url,
                    dry_run=dry_run,
                    session_id=session_id,
                    channel_id=channel_id,
                )
                _print_import_result(result)
                return 0 if result.failed == 0 else 1
            except ValueError as e:
                print(f"Error: {e}")
                return 1
            except Exception as e:
                print(f"Import failed: {e}")
                return 1

        # File import
        force = getattr(args, "force", False)
        if not input_path:
            print("Usage: hermes mnemosyne import --input <path> [--force]")
            print("       hermes mnemosyne import --from <provider> --api-key <key> [--dry-run]")
            print("       hermes mnemosyne import --list-providers")
            return 1
        try:
            stats = mem.import_from_file(input_path, force=force)
            beam_stats = stats.get("beam", {})
            legacy_stats = stats.get("legacy", {})
            triples_stats = stats.get("triples", {})
            print(f"Import complete:")
            print(f"  Working: +{beam_stats.get('working_memory', {}).get('inserted', 0)}")
            print(f"  Episodic: +{beam_stats.get('episodic_memory', {}).get('inserted', 0)}")
            print(f"  Legacy: +{legacy_stats.get('inserted', 0)}")
            print(f"  Triples: +{triples_stats.get('inserted', 0)}")
            if force:
                print(f"  (force mode: overwrites applied)")
        except Exception as e:
            print(f"Import failed: {e}")
            return 1

    return 0
