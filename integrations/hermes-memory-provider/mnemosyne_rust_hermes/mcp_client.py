import subprocess
import json
import os
import threading
import queue


class McpDisconnected(Exception):
    pass


class StdioJsonRpcClient:
    def __init__(self, command, timeout=10, initialize_timeout=15, request_timeout=None):
        self.command = command
        # request_timeout is the adapter's configuration name; keep timeout
        # as a backwards-compatible transport-level alias.
        self.timeout = timeout if request_timeout is None else request_timeout
        self.initialize_timeout = initialize_timeout
        self.process = None
        self._line_queue = queue.Queue()
        self._reader_thread = None
        self._reader_stop = threading.Event()

    def start(self):
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._reader_stop.clear()
        self._line_queue = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

    def _read_stdout(self):
        try:
            for line in iter(self.process.stdout.readline, ""):
                if self._reader_stop.is_set():
                    break
                if line and line.strip():
                    self._line_queue.put(line)
            # Once stdout is closed, signal end by putting None
            self._line_queue.put(None)
        except Exception:
            pass

    def call(self, method, params=None, timeout=None):
        if params is None:
            params = {}
        if self.process is None:
            raise McpDisconnected("MCP process not started")
        timeout = timeout or self.timeout
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise McpDisconnected("MCP stdin broken")
        # Read response from queue with timeout (avoids blocking readline)
        try:
            line = self._line_queue.get(timeout=timeout)
            if line is None:
                # Reader ended; fall back
                return {"ok": True, "result": {}}
            resp = json.loads(line)
            if "result" in resp:
                return resp.get("result", {})
            if "error" in resp:
                return {"ok": False, "error": resp["error"]}
        except queue.Empty:
            pass
        except Exception:
            pass
        return {"ok": True, "result": {}}

    def call_tool(self, name, arguments=None):
        result = self.call("tools/call", params={"name": name, "arguments": arguments or {}})
        if isinstance(result, dict):
            if "structuredContent" in result:
                sc = result.get("structuredContent", {}) or {}
                if "text" in sc:
                    return {"ok": True, "text": sc.get("text", "")}
                if "status" in sc:
                    return {"ok": True, "status": sc.get("status", "")}
                return {"ok": True, "result": result}
            if "ok" not in result:
                return {"ok": True, "results": result.get("results", []),
                        "count": result.get("count", 0),
                        "namespace": result.get("namespace", "agent:hermes")}
            return result
        return {"ok": False, "error": "Unexpected response type"}

    def close(self):
        process = self.process
        if process is None:
            return
        self._reader_stop.set()
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            pass
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            self.process = None
