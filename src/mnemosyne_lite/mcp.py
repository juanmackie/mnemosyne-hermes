"""Newline-delimited MCP stdio server for the implemented memory tools."""
import json
import os
import sys

from lib.storage import PythonMemoryStorage, StorageError
from .tools import call_tool, tool_schemas


def serve(db_path, stdin=None, stdout=None):
    """Serve MCP over stdin/stdout against an EXISTING store.

    The store is opened once, before the request loop, so a misconfigured path
    fails loudly at startup instead of answering every request with an error.
    A missing or foreign database is refused (never fabricated and never
    mutated); run `mnemosyne-lite init` to create a new store deliberately.
    """
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not os.path.exists(db_path):
        raise StorageError(
            f"no Mnemosyne database at {db_path} (nothing was created). "
            "Run 'mnemosyne-lite init' first, or point --db-path at an existing store."
        )
    storage = PythonMemoryStorage(db_path)
    try:
        for line in stdin:
            request = None
            try:
                request = json.loads(line)
                if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                        or not isinstance(request.get("method"), str)):
                    raise ValueError("Invalid JSON-RPC request")
                if "id" not in request:
                    continue  # notifications never receive responses
                params = request.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                method = request["method"]
                if method == "initialize":
                    requested = params.get("protocolVersion")
                    result = {"protocolVersion": requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18") else "2025-06-18",
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "mnemosyne", "version": "2.4.0"}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": [{"name": t["name"], "description": t["description"],
                                         "inputSchema": t["parameters"]} for t in tool_schemas()]}
                elif method == "tools/call":
                    try:
                        data = call_tool(storage, params.get("name", ""), params.get("arguments", {}))
                        result = {"content": [{"type": "text", "text": json.dumps(data)}],
                                  "structuredContent": data, "isError": False}
                    except Exception as exc:
                        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                else:
                    response = {"jsonrpc": "2.0", "id": request["id"],
                                "error": {"code": -32601, "message": "Method not found"}}
                    stdout.write(json.dumps(response) + "\n")
                    stdout.flush()
                    continue
                response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            except (ValueError, TypeError) as exc:
                response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                            "error": {"code": -32700 if isinstance(exc, json.JSONDecodeError) else -32600,
                                      "message": str(exc)}}
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    finally:
        storage.close()
    return 0
