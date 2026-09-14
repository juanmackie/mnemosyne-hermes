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
        # Simplified response read (production would read line and parse)
        return {"ok": True, "result": {}}

    def close(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None
