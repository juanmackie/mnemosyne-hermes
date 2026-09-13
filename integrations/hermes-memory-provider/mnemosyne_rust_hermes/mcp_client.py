"""Persistent newline-delimited JSON-RPC 2.0 client over stdio.

Speaks the frozen Mnemosyne MCP surface:

* server command: MNEMOSYNE_BIN + MNEMOSYNE_MCP_ARGS (default "mnemosyne mcp")
* initialize handshake, then tools/call with the underscore tool names
  (mnemosyne_prefetch / mnemosyne_sync_turn); if a server does not expose MCP
  tools it falls back to the direct JSON-RPC methods (mnemosyne.prefetch and
  its mnemosyne_prefetch alias).
* request-id correlation, per-request timeouts, stderr capture, clean shutdown.

Pure standard library; Python 3.11+.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

JSON = Dict[str, Any]

METHOD_NOT_FOUND = -32601
PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    """JSON-RPC transport / server error."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        data: Any = None,
        method: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.method = method


class McpTimeout(McpError):
    """No response within the configured timeout."""


class McpDisconnected(McpError):
    """The child process closed or could not be started."""


class McpProtocolError(McpError):
    """Malformed frame from the server."""


def _maybe_json(text: str) -> Any:
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _content_text(result: JSON) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def extract_tool_result(result: Any) -> Any:
    """Normalise an MCP tools/call result into the provider's result object.

    Prefers structuredContent; otherwise decodes the text content as JSON (the
    frozen interface returns objects such as text/count/diagnostics). Falls back
    to a plain text mapping.
    """
    if isinstance(result, dict):
        if result.get("isError"):
            raise McpError(
                "server reported tool error: " + (_content_text(result)[:500]),
                method="tools/call",
            )
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content")
        if isinstance(content, list):
            text = _content_text(result)
            parsed = _maybe_json(text)
            if parsed is not None:
                return parsed
            return {"text": text}
    return result


class StdioJsonRpcClient:
    """A long-lived MCP stdio client.

    One reader thread dispatches responses by request id; one stderr thread
    keeps a bounded diagnostic tail. Restartable: start() respawns after the
    child died.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        request_timeout: float = 10.0,
        initialize_timeout: float = 15.0,
        client_name: str = "mnemosyne-rust-hermes",
        client_version: str = "0.1.0",
        stderr_max_lines: int = 200,
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty sequence")
        self._command = list(command)
        self._env = dict(env) if env else None
        self._cwd = cwd
        self._request_timeout = float(request_timeout)
        self._initialize_timeout = float(initialize_timeout)
        self._client_name = client_name
        self._client_version = client_version

        self._proc: Optional[subprocess.Popen] = None
        self._write_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._pending: Dict[int, JSON] = {}
        self._next_id = 1
        self._closing = False
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        self._stderr_lines: Deque[str] = deque(maxlen=stderr_max_lines)
        self._protocol_errors: Deque[str] = deque(maxlen=20)
        self._notifications: List[JSON] = []
        self._unsolicited: List[JSON] = []
        self._initialized = threading.Event()
        self.server_info: Optional[JSON] = None
        self.tools: Optional[List[JSON]] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "StdioJsonRpcClient":
        with self._lifecycle_lock:
            if self._proc is not None and self._proc.poll() is None:
                return self
            self._closing = False
            self._initialized.clear()
            self._spawn()
        return self

    def _spawn(self) -> None:
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        kwargs: Dict[str, Any] = {}
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=env,
                creationflags=creationflags,
                **kwargs,
            )
        except OSError as exc:
            raise McpDisconnected(
                "failed to start " + " ".join(self._command) + ": " + str(exc)
            ) from exc
        self._proc = proc
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(proc,),
            name="mnemosyne-mcp-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(proc,),
            name="mnemosyne-mcp-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def initialize(self, timeout: Optional[float] = None) -> Optional[JSON]:
        """Perform the MCP initialize handshake (tolerates method-not-found)."""
        self.start()
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self._client_name, "version": self._client_version},
        }
        try:
            result = self._request(
                "initialize", params, timeout=timeout or self._initialize_timeout
            )
        except McpError as exc:
            if exc.code == METHOD_NOT_FOUND:
                logger.info("mnemosyne-rust: server has no initialize method; continuing")
                self._initialized.set()
                return None
            raise
        self.server_info = result if isinstance(result, dict) else None
        try:
            self._notify("notifications/initialized", {})
        except McpError:
            pass
        self._initialized.set()
        return self.server_info

    def list_tools(self, timeout: Optional[float] = None) -> List[JSON]:
        """Best-effort tools/list; returns [] when unsupported."""
        try:
            result = self._request(
                "tools/list", {}, timeout=timeout or self._request_timeout
            )
        except McpError:
            return []
        tools = result.get("tools") if isinstance(result, dict) else result
        if isinstance(tools, list):
            self.tools = tools
            return tools
        return []

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._fail_pending(McpDisconnected("client closed"))

    @property
    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        proc = self._proc
        return proc.pid if proc is not None else None

    # -- calls -------------------------------------------------------------

    def call_tool(
        self,
        name: str,
        arguments: JSON,
        *,
        timeout: Optional[float] = None,
        direct_methods: Sequence[str] = (),
    ) -> Any:
        """Invoke name via MCP tools/call.

        On -32601 (method not found) it falls back to the direct JSON-RPC alias
        methods (e.g. mnemosyne.prefetch). Returns the normalised result.
        """
        last_error: Optional[McpError] = None
        try:
            raw = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout=timeout,
            )
            return extract_tool_result(raw)
        except McpError as exc:
            if exc.code != METHOD_NOT_FOUND:
                raise
            last_error = exc

        methods: List[str] = []
        for method in (name, *direct_methods):
            if method and method not in methods:
                methods.append(method)
        for method in methods:
            try:
                return self._request(method, arguments, timeout=timeout)
            except McpError as exc:
                if exc.code == METHOD_NOT_FOUND:
                    last_error = exc
                    continue
                raise
        raise McpError(
            "server exposes neither tools/call nor a direct method for " + name,
            code=METHOD_NOT_FOUND,
            method=name,
        ) from last_error

    def _notify(self, method: str, params: Optional[JSON] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(
        self, method: str, params: Optional[JSON], timeout: Optional[float] = None
    ) -> Any:
        if self._closing:
            raise McpDisconnected("client is closing")
        self.start()
        request_id = self._alloc_id()
        slot: JSON = {"event": threading.Event(), "response": None, "error": None}
        with self._state_lock:
            self._pending[request_id] = slot
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params if params is not None else {},
                }
            )
        except McpError:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise
        wait_seconds = float(timeout if timeout is not None else self._request_timeout)
        if not slot["event"].wait(wait_seconds):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise McpTimeout(
                "timed out after " + str(wait_seconds) + "s waiting for " + method,
                method=method,
            )
        if slot["error"] is not None:
            raise slot["error"]
        response = slot["response"] or {}
        if "error" in response and response["error"] is not None:
            error = response["error"] if isinstance(response["error"], dict) else {}
            raise McpError(
                str(error.get("message") or "JSON-RPC error"),
                code=error.get("code"),
                data=error.get("data"),
                method=method,
            )
        return response.get("result")

    def _send(self, message: JSON) -> None:
        data = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise McpDisconnected("MCP server process is not running")
            try:
                proc.stdin.write(data + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise McpDisconnected("failed writing to MCP server: " + str(exc)) from exc

    def _alloc_id(self) -> int:
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    # -- reader threads ----------------------------------------------------

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, ""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self._protocol_errors.append(line[:500])
                    logger.debug("mnemosyne-rust: non-JSON stdout line: %s", line[:200])
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id is not None and ("result" in message or "error" in message):
                    with self._state_lock:
                        slot = self._pending.pop(message_id, None)
                    if slot is not None:
                        slot["response"] = message
                        slot["event"].set()
                    else:
                        self._unsolicited.append(message)
                else:
                    self._notifications.append(message)
        finally:
            self._fail_pending(McpDisconnected("MCP server closed stdout"))

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip()
                if line:
                    self._stderr_lines.append(line)
        except Exception:
            return

    def _fail_pending(self, exc: McpError) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = exc
            slot["event"].set()

    # -- diagnostics -------------------------------------------------------

    def stderr_tail(self, limit: int = 20) -> List[str]:
        items = list(self._stderr_lines)
        return items[-limit:] if limit > 0 else items

    def stderr_text(self, limit: int = 20) -> str:
        return "\n".join(self.stderr_tail(limit))

    def protocol_errors(self) -> List[str]:
        return list(self._protocol_errors)
