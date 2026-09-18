"""Lite MCP surface checks (src/mnemosyne_lite/mcp.py + tools.py).

The standalone MCP server is a public surface, and it opens its store once,
before the request loop. These checks pin the two things that fail in practice:
a request round trip that must reach the store, and a misconfigured path that
must fail loudly instead of being fabricated or mutated.

    python tests/test_lite_mcp.py
    pytest tests/test_lite_mcp.py
"""
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from lib.storage import PythonMemoryStorage, StorageError, StorageSchemaError  # noqa: E402
from mnemosyne_lite.mcp import serve  # noqa: E402


def _serve(db_path, requests):
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    serve(db_path, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def _new_store(d):
    db = os.path.join(d, "mcp.db")
    PythonMemoryStorage(db).close()
    return db


def test_round_trip_over_the_mcp_protocol():
    with tempfile.TemporaryDirectory() as d:
        db = _new_store(d)
        responses = _serve(db, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "mnemosyne_memory_remember",
                        "arguments": {"content": "the user prefers local storage",
                                      "namespace": "ns"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "mnemosyne_memory_search",
                        "arguments": {"query": "local storage", "namespace": "ns"}}},
        ])
        assert [r["id"] for r in responses] == [1, 2, 3, 4, 5], responses
        assert responses[0]["result"]["serverInfo"]["name"] == "mnemosyne"
        names = [t["name"] for t in responses[2]["result"]["tools"]]
        assert "mnemosyne_memory_search" in names and "mnemosyne_memory_remember" in names
        assert responses[3]["result"]["isError"] is False
        found = responses[4]["result"]["structuredContent"]
        assert found["count"] == 1, found
        assert found["results"][0]["content"] == "the user prefers local storage"


def test_notifications_and_unknown_methods():
    with tempfile.TemporaryDirectory() as d:
        db = _new_store(d)
        responses = _serve(db, [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},   # no id -> no reply
            {"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "not_a_tool", "arguments": {}}},
        ])
        assert len(responses) == 2, responses          # the notification was silent
        assert responses[0]["error"]["code"] == -32601
        assert responses[1]["result"]["isError"] is True
        assert "Unknown tool" in responses[1]["result"]["content"][0]["text"]


def test_missing_store_is_refused_not_fabricated():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "nested", "missing.db")
        try:
            serve(missing, stdin=io.StringIO(""), stdout=io.StringIO())
        except StorageError as e:
            assert "nothing was created" in str(e)
        else:
            raise AssertionError("the server fabricated a store at a missing path")
        assert not os.path.exists(missing)


def test_foreign_store_is_refused_byte_identically():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "foreign.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, created_at REAL)")
        conn.execute("INSERT INTO memories VALUES ('a', 'not ours', 1.0)")
        conn.commit()
        conn.close()
        before = hashlib.sha256(open(db, "rb").read()).hexdigest()
        try:
            serve(db, stdin=io.StringIO(""), stdout=io.StringIO())
        except StorageSchemaError:
            pass
        else:
            raise AssertionError("the server accepted a foreign database")
        assert hashlib.sha256(open(db, "rb").read()).hexdigest() == before, "file was modified"


if __name__ == "__main__":
    tests = [test_round_trip_over_the_mcp_protocol,
             test_notifications_and_unknown_methods,
             test_missing_store_is_refused_not_fabricated,
             test_foreign_store_is_refused_byte_identically]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
