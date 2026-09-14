"""Hermes Agent MemoryProvider backed by the Mnemosyne Rust binary.

Entry point: hermes_agent.memory_providers -> mnemosyne_rust_hermes:register
(see pyproject.toml); users select it with 'memory.provider: mnemosyne-rust'.

Design rules enforced here:

* is_available() never spawns a process and never touches the network.
* prefetch returns UNFENCED plain text. Hermes applies the <memory-context>
  wrapper and its streaming scrubber, so this module must never emit those
  tags, and it degrades to "" instead of raising.
* sync_turn hands captures to BackgroundWorker so a turn is never blocked by an
  MCP round-trip.
* Cron / flush / subagent / background / skill_loop contexts are skipped locally
  (the MCP call is not even attempted) *and* the context is forwarded to the
  Rust side for defence in depth. Assistant-authored turns are never captured.
* on_pre_compress is fail-closed (pre_compress_checkpoint_api_version = 2): it
  writes a durable, content-addressed checkpoint and raises on failure rather
  than reporting partial success.

Importing agent.memory_provider is defensive: when Hermes is absent the provider
still imports and is usable/testable with the same method surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional

from .config import PROVIDER_ID_DEFAULT, ProviderConfig, default_config
from .contexts import is_skip_context, normalize_context
from .mcp_client import StdioJsonRpcClient
from .worker import BackgroundWorker

try:  # pragma: no cover - depends on whether Hermes is installed
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    MemoryProvider = object  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

PROVIDER_ID = PROVIDER_ID_DEFAULT  # "mnemosyne-rust"
CHECKPOINT_API_VERSION = 2

PREFETCH_TOOL = "mnemosyne_prefetch"
SYNC_TURN_TOOL = "mnemosyne_sync_turn"
PREFETCH_ALIASES = ("mnemosyne.prefetch", "mnemosyne_prefetch")
SYNC_TURN_ALIASES = ("mnemosyne.sync_turn", "mnemosyne_sync_turn")

CONFIG_FILE_NAME = "provider.json"
CHECKPOINT_DIR_NAME = "checkpoints"

SYSTEM_PROMPT_BLOCK = (
    "Long-term memory is provided by the local Mnemosyne store through the "
    "'mnemosyne-rust' Hermes memory provider. Relevant memories are injected "
    "automatically before a turn; use the mnemosyne_memory_search tool only when "
    "you need to look something up that was not injected."
)


class CheckpointError(RuntimeError):
    """A durable pre-compression checkpoint could not be written (fail-closed)."""


class MnemosyneRustProvider(MemoryProvider):  # type: ignore[misc]
    """Concrete MemoryProvider for Mnemosyne's persistent MCP stdio server."""

    #: Fail-closed pre-compression archiving (see on_pre_compress).
    pre_compress_checkpoint_api_version = CHECKPOINT_API_VERSION

    def __init__(
        self,
        config: Optional[ProviderConfig] = None,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[ProviderConfig], Any]] = None,
    ) -> None:
        self.config: ProviderConfig = config or default_config()
        self._injected_client = client
        self._client_factory = client_factory
        self._client: Any = None
        self._client_lock = threading.RLock()
        self._worker = BackgroundWorker()
        self._state_lock = threading.RLock()
        self._session_id: str = ""
        self._hermes_home: Optional[str] = None
        self._execution_context: str = normalize_context(self.config.execution_context)
        self._initialized = False
        self._closed = False
        self._shutdown_done = False
        self._prefetch_cache: Dict[str, str] = {}
        self.last_memory_write: Optional[Dict[str, Any]] = None
        self.checkpoints_written = 0

    # -- identity / availability -------------------------------------------

    @property
    def name(self) -> str:
        """Provider id selected by 'memory.provider'."""
        return PROVIDER_ID

    def is_available(self) -> bool:
        """True when the configured binary looks executable. No I/O side effects.

        Deliberately no subprocess spawn and no network probe: this is called on
        hot paths (provider election, config UI).
        """
        binary = (self.config.binary or "").strip()
        if not binary:
            return False
        if os.path.dirname(binary):
            if not os.path.isfile(binary):
                return False
            if os.name == "nt":
                # Windows os.access(X_OK) is true for any existing file, so
                # require a PATHEXT executable extension instead of spawning it.
                pathext = (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").lower()
                return os.path.splitext(binary)[1].lower() in pathext.split(os.pathsep)
            return os.access(binary, os.X_OK)
        return shutil.which(binary) is not None

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, session_id: str = "", **kwargs: Any) -> bool:
        """Bind a session. kwargs always carries hermes_home."""
        self._session_id = session_id or kwargs.get("session_id") or ""
        hermes_home = kwargs.get("hermes_home")
        if hermes_home:
            self._hermes_home = str(hermes_home)
            if not self.config.hermes_home:
                self.config.hermes_home = self._hermes_home
        context = kwargs.get("agent_context") or kwargs.get("execution_context")
        if context:
            self._execution_context = normalize_context(context)
        self._closed = False
        self._shutdown_done = False
        self._worker.start()
        self._initialized = True
        if self.config.eager_connect:
            try:
                self._ensure_client()
            except Exception as exc:  # pragma: no cover - degraded path
                logger.warning("mnemosyne-rust: eager connect failed: %s", exc)
        return True

    def shutdown(self) -> None:
        """Flush pending captures, stop the worker, close the transport.

        Idempotent: repeated calls are no-ops after the first one.
        """
        with self._state_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._closed = True
        try:
            if not self._worker.drain(self.config.shutdown_timeout):
                logger.warning("mnemosyne-rust: shutdown drain timed out; captures may be lost")
            self._worker.stop(self.config.shutdown_timeout)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("mnemosyne-rust: worker shutdown failed: %s", exc)
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("mnemosyne-rust: transport close failed: %s", exc)

    # -- transport ---------------------------------------------------------

    def _ensure_client(self) -> Any:
        with self._client_lock:
            if self._client is not None:
                return self._client
            client = self._injected_client
            if client is None:
                factory = self._client_factory or _default_client_factory
                client = factory(self.config)
            initialize = getattr(client, "initialize", None)
            if callable(initialize):
                initialize()
            self._client = client
            return client

    def _call(self, tool: str, arguments: Dict[str, Any], *, timeout: float) -> Any:
        client = self._ensure_client()
        aliases = PREFETCH_ALIASES if tool == PREFETCH_TOOL else SYNC_TURN_ALIASES
        return client.call_tool(tool, arguments, timeout=timeout, direct_methods=aliases)

    # -- context gating ----------------------------------------------------

    def _context_for(self, **kwargs: Any) -> str:
        explicit = kwargs.get("agent_context") or kwargs.get("execution_context")
        if explicit:
            return normalize_context(explicit)
        return self._execution_context

    def _skipped(self, **kwargs: Any) -> bool:
        return is_skip_context(self._context_for(**kwargs))

    # -- optional hooks ----------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self.is_available():
            return ""
        return SYSTEM_PROMPT_BLOCK

    def prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> str:
        """Return unfenced memory text for injection, or "" (never raises)."""
        if not isinstance(query, str) or not query.strip():
            return ""
        if self._skipped(**kwargs):
            return ""
        try:
            result = self._call(
                PREFETCH_TOOL,
                self._prefetch_args(query, session_id, **kwargs),
                timeout=self.config.prefetch_timeout,
            )
        except Exception as exc:
            logger.warning("mnemosyne-rust: prefetch degraded to empty context: %s", exc)
            return ""
        return _result_text(result)

    def queue_prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> bool:
        """Warm the cache asynchronously; read it back with take_prefetched()."""
        if not isinstance(query, str) or not query.strip() or self._skipped(**kwargs):
            return False
        key = session_id or self._session_id

        def job() -> None:
            text = self.prefetch(query, session_id=session_id, **kwargs)
            with self._state_lock:
                self._prefetch_cache[key] = text

        self._worker.start()
        return self._worker.submit(job, kind="prefetch")

    def take_prefetched(self, session_id: str = "") -> str:
        """Pop the most recently queued prefetch text (helper; may be empty)."""
        with self._state_lock:
            return self._prefetch_cache.pop(session_id or self._session_id, "")

    def sync_turn(
        self,
        user: str,
        assistant: str = "",
        *,
        session_id: str = "",
        messages: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> bool:
        """Queue a non-blocking capture. Returns True when it was queued."""
        if not isinstance(user, str) or not user.strip():
            return False  # never capture assistant-authored / empty turns
        if normalize_context(kwargs.get("speaker")) in ("assistant", "agent"):
            return False
        if not self.config.is_capture_owner:
            return False
        if self._skipped(**kwargs):
            return False
        arguments = self._sync_turn_args(user, assistant, session_id, **kwargs)
        self._worker.start()
        return self._worker.submit(lambda: self._capture(arguments), kind="sync_turn")

    def _capture(self, arguments: Dict[str, Any]) -> None:
        """Worker-side capture; failure is logged, never raised into Hermes."""
        try:
            self._call(SYNC_TURN_TOOL, arguments, timeout=self.config.request_timeout)
        except Exception as exc:
            logger.warning("mnemosyne-rust: background capture failed: %s", exc)

    def on_session_end(self, messages: Optional[List[Any]] = None) -> bool:
        self._worker.start()
        drained = self._worker.drain(self.config.shutdown_timeout)
        self._session_id = ""
        return drained

    def on_pre_compress(
        self,
        messages: Optional[List[Any]] = None,
        *,
        require_checkpoint: bool = False,
        **kwargs: Any,
    ) -> bool:
        """Durable, idempotent checkpoint. Fail-closed: raises on write failure."""
        if self._skipped(**kwargs):
            return False
        payload = {
            "api_version": CHECKPOINT_API_VERSION,
            "provider": PROVIDER_ID,
            "session_id": kwargs.get("session_id") or self._session_id,
            "messages": list(messages or []),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=repr)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        try:
            path = os.path.join(self._checkpoint_dir(), digest + ".json")
            if os.path.isfile(path):
                return True  # idempotent: identical content is already durable
            _atomic_write(path, encoded)
        except OSError as exc:
            raise CheckpointError(
                "mnemosyne-rust: pre-compression checkpoint failed ("
                + digest[:12]
                + "): "
                + str(exc)
            ) from exc
        if require_checkpoint and not os.path.isfile(path):  # pragma: no cover - defensive
            raise CheckpointError("mnemosyne-rust: checkpoint missing after write: " + path)
        self.checkpoints_written += 1
        return True

    def on_memory_write(self, action: str, target: str, content: str) -> bool:
        """Record Hermes' own memory writes; deliberately captures nothing.

        Hermes persists the write itself, so re-capturing it here would duplicate
        the memory. Returning False means "not handled by this provider".
        """
        self.last_memory_write = {
            "action": str(action),
            "target": str(target),
            "content": str(content),
        }
        return False

    # -- config ------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "binary",
                "description": "Path or name of the mnemosyne executable.",
                "env_var": "MNEMOSYNE_BIN",
                "default": "mnemosyne",
                "required": False,
                "secret": False,
            },
            {
                "key": "mcp_args",
                "description": "Arguments appended to the binary (JSON array or shell string).",
                "env_var": "MNEMOSYNE_MCP_ARGS",
                "default": "mcp",
                "required": False,
                "secret": False,
            },
            {
                "key": "db_path",
                "description": "Database file; defaults to <hermes_home>/mnemosyne/mnemosyne.db.",
                "env_var": "MNEMOSYNE_DB_PATH",
                "default": "",
                "required": False,
                "secret": False,
            },
            {
                "key": "namespace",
                "description": "Memory namespace written and searched by this provider.",
                "env_var": "MNEMOSYNE_NAMESPACE",
                "default": "agent:hermes",
                "required": False,
                "secret": False,
            },
            {
                "key": "policy_owner",
                "description": "Owner of automatic capture; any other value disables capture here.",
                "env_var": "MNEMOSYNE_POLICY_OWNER",
                "default": PROVIDER_ID,
                "required": False,
                "secret": False,
            },
            {
                "key": "execution_context",
                "description": "Default execution context; capture/injection skip cron, flush, subagent, background, skill_loop.",
                "env_var": "MNEMOSYNE_EXECUTION_CONTEXT",
                "default": "",
                "required": False,
                "secret": False,
                "choices": ["", "primary", "cron", "flush", "subagent", "background", "skill_loop"],
            },
            {
                "key": "prefetch_timeout",
                "description": "Bounded timeout in seconds for a prefetch round-trip.",
                "env_var": "MNEMOSYNE_PREFETCH_TIMEOUT",
                "default": 3.0,
                "required": False,
                "secret": False,
            },
            {
                "key": "request_timeout",
                "description": "Per-JSON-RPC request timeout in seconds.",
                "env_var": "MNEMOSYNE_REQUEST_TIMEOUT",
                "default": 10.0,
                "required": False,
                "secret": False,
            },
            {
                "key": "shutdown_timeout",
                "description": "Seconds to drain pending captures on shutdown.",
                "env_var": "MNEMOSYNE_SHUTDOWN_TIMEOUT",
                "default": 5.0,
                "required": False,
                "secret": False,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str = "") -> str:
        """Persist operator-supplied values next to the Mnemosyne storage.

        Returns the absolute path of the written file (truthy on success).
        """
        home = hermes_home or self._hermes_home or ""
        if home:
            self._hermes_home = str(home)
            self.config.hermes_home = str(home)
        applied = {}
        for key, value in (values or {}).items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
                applied[key] = value
        self._execution_context = normalize_context(self.config.execution_context)
        payload = {"provider": PROVIDER_ID, "values": applied}
        directory = self.config.storage_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, CONFIG_FILE_NAME)
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return path

    # -- tool surface ------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "mnemosyne_memory_search",
                "description": "Search local long-term memory for relevant notes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for."},
                        "limit": {"type": "integer", "description": "Maximum results (default 5)."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mnemosyne_memory_remember",
                "description": "Store a durable fact or preference in local long-term memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Text to remember."},
                        "session_id": {"type": "string", "description": "Originating session."},
                    },
                    "required": ["content"],
                },
            },
        ]

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs: Any
    ) -> Dict[str, Any]:
        """Dispatch a Hermes tool call; returns a JSON-serialisable mapping."""
        args = args or {}
        if self._skipped(**kwargs):
            return {"ok": False, "error": "memory tools are disabled in this execution context"}
        try:
            if tool_name == "mnemosyne_memory_search":
                query = str(args.get("query") or "")
                limit = args.get("limit")
                extra = {"limit": int(limit)} if isinstance(limit, (int, float)) else {}
                text = self.prefetch(query, session_id=kwargs.get("session_id", ""), **extra)
                return {"ok": True, "text": text}
            if tool_name == "mnemosyne_memory_remember":
                content = str(args.get("content") or "")
                sid = str(args.get("session_id") or kwargs.get("session_id", ""))
                queued = self.sync_turn(content, "", session_id=sid)
                return {"ok": queued, "queued": queued}
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("mnemosyne-rust: tool %s failed: %s", tool_name, exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "unknown tool: " + str(tool_name)}

    # -- internals ---------------------------------------------------------

    def _prefetch_args(self, query: str, session_id: str, **kwargs: Any) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"query": query.strip(), "namespace": self.config.namespace}
        for key in ("budget_tokens", "limit"):
            if isinstance(kwargs.get(key), (int, float)):
                arguments[key] = int(kwargs[key])
        sid = session_id or self._session_id
        if sid:
            arguments["session_id"] = sid
        context = self._context_for(**kwargs)
        if context:
            arguments["execution_context"] = context
        return arguments

    def _sync_turn_args(
        self, user: str, assistant: str, session_id: str, **kwargs: Any
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "user_text": user,
            "assistant_text": assistant or "",
            "namespace": self.config.namespace,
            "policy_owner": self.config.effective_policy_owner,
            "speaker": "user",
        }
        context = self._context_for(**kwargs)
        if context:
            arguments["execution_context"] = context
        sid = session_id or self._session_id
        if sid:
            arguments["session_id"] = sid
        turn_id = kwargs.get("turn_id")
        if turn_id:
            arguments["turn_id"] = str(turn_id)
        return arguments

    def _checkpoint_dir(self) -> str:
        directory = os.path.join(self.config.storage_dir(), CHECKPOINT_DIR_NAME)
        os.makedirs(directory, exist_ok=True)
        return directory

    # -- diagnostics -------------------------------------------------------

    @property
    def worker(self) -> BackgroundWorker:
        return self._worker


def _default_client_factory(config: ProviderConfig) -> Any:
    return StdioJsonRpcClient(
        config.command,
        env=config.child_env(),
        request_timeout=config.request_timeout,
        initialize_timeout=config.initialize_timeout,
    )


def _result_text(result: Any) -> str:
    """Extract unfenced text from a prefetch result (no <memory-context> tags)."""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            return text.strip()
    return ""


def _atomic_write(path: str, text: str) -> None:
    """Durable write: temp file in the same directory, fsync, atomic replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# --- Planning deliverable item 5 design note (audit tracking) ---
# Added per planning deliverable item 5 (compression durability / audit repair).
# This is a DESIGN NOTE only. Actual audit event insertion requires DB schema
# verification (`audit_events` table existence) and is NOT executed in this
# planning implementation (Repo-only authorization; no deployed DB writes).
#
# Proposed audit event insertion (not executed against deployed DB):
#   For checkpoint writes (`on_pre_compress`), insert:
#     event_type='checkpoint', digest=<sha256>, provider='mnemosyne-rust',
#     path='checkpoints/<digest>.json', timestamp=<utc>
#   For memory mutations (`on_memory_write`), insert audit row (currently
#     returns False; proposed change: return True after audit insertion):
#     event_type='mutation', action=<action>, target=<target>, provider='mnemosyne-rust'
#   For maintenance (`OrphanRepair` / maintenance runs), insert:
#     event_type='maintenance', projection_counts=<exact counts>, timestamp=<utc>
#
# Note: The `BackgroundWorker` does not currently expose a public `drain()`
# for queued captures before checkpoint. A future design change could add:
#   if self._worker.has_queued():
#       self._worker.drain(timeout=self.config.shutdown_timeout)
# Before `on_pre_compress()` writes a checkpoint, queued captures must be
# drained to prevent data loss during compression.
# This design note is part of the item 5 planning deliverable.
