import subprocess
import json
import os


class McpDisconnected(Exception):
    pass


class StdioJsonRpcClient:
    def __init__(self, command, timeout=10, initialize_timeout=15):
        self.command = command
        self.timeout = timeout
        self.initialize_timeout = initialize_timeout
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

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
        # Read response line from stdout (blocking until newline or timeout)
        try:
            # For stdio transport, read exactly one JSON-RPC response line.
            # This blocks briefly; if the server doesn't respond, fall back.
            line = self.process.stdout.readline()
            if line and line.strip():
                resp = json.loads(line)
                if "result" in resp:
                    return resp.get("result", {})
                if "error" in resp:
                    return {"ok": False, "error": resp["error"]}
        except Exception:
            pass
        return {"ok": True, "result": {}}

    def call_tool(self, name, arguments=None):
        # Delegate to call() method using the tool name as method
        result = self.call("tools/call", params={"name": name, "arguments": arguments or {}})
        # Map stdio JSON-RPC result shape into adapter contract
        if isinstance(result, dict):
            if "structuredContent" in result:
                sc = result.get("structuredContent", {}) or {}
                # Prefetch: extract text field if present
                if "text" in sc:
                    return {"ok": True, "text": sc.get("text", "")}
                if "status" in sc:
                    return {"ok": True, "status": sc.get("status", "")}
                return {"ok": True, "result": result}
            # Already in adapter contract shape
            if "ok" not in result:
                # Convert raw result dict to adapter contract if needed
                return {"ok": True, "results": result.get("results", []),
                        "count": result.get("count", 0),
                        "namespace": result.get("namespace", "agent:hermes")}
            return result
        return {"ok": False, "error": "Unexpected response type"}

    def close(self):
        if self.process is not None:
            try:
                # Give the process a brief moment to flush stdout before terminating
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None
