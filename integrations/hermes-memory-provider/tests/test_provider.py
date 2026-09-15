"""Contract tests for the Mnemosyne Rust Hermes MemoryProvider.

Runnable with either runner:

    python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v
    pytest integrations/hermes-memory-provider/tests/test_provider.py

Standard library only. A subprocess-free fake MCP server records sync_turn and
serves prefetch from the recorded turns, so the preference-recall path is
exercised end to end. A second, real-stdio test drives the same protocol through
the production StdioJsonRpcClient against a small Python script.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

import mnemosyne_rust_hermes as pkg  # noqa: E402
from mnemosyne_rust_hermes.config import ProviderConfig  # noqa: E402
from mnemosyne_rust_hermes.contexts import SKIP_CONTEXTS  # noqa: E402
from mnemosyne_rust_hermes.mcp_client import McpDisconnected, StdioJsonRpcClient  # noqa: E402
from mnemosyne_rust_hermes.provider import (  # noqa: E402
    CheckpointError,
    MnemosyneRustProvider,
    PROVIDER_ID,
)

PREFETCH_CALLS = ("mnemosyne_prefetch", "mnemosyne.prefetch")
SYNC_CALLS = ("mnemosyne_sync_turn", "mnemosyne.sync_turn")


def _tokens(query):
    return [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) >= 4]


class FakeMcpServer:
    """In-process stand-in for the mnemosyne MCP stdio server."""

    def __init__(self):
        self.turns = []
        self.calls = []
        self.close_count = 0
        self.fail_with = None

    # transport surface used by the provider
    def initialize(self):
        return {"serverInfo": {"name": "fake-mnemosyne", "version": "test"}}

    def call_tool(self, name, arguments, *, timeout=None, direct_methods=()):
        self.calls.append((name, dict(arguments)))
        if self.fail_with is not None:
            raise self.fail_with
        if name in PREFETCH_CALLS:
            return self._prefetch(arguments)
        if name in SYNC_CALLS:
            return self._sync(arguments)
        raise AssertionError("unexpected tool: " + str(name))

    def close(self):
        self.close_count += 1

    # server behaviour
    def _prefetch(self, arguments):
        query = arguments["query"]
        matches = [t for t in self.turns if any(tok in t["user_text"].lower() for tok in _tokens(query))]
        if not matches and self.turns:
            matches = self.turns[-1:]
        text = "\n".join("- " + t["user_text"] for t in matches[: arguments.get("limit", 5)])
        return {"text": text, "count": len(matches), "diagnostics": {"source": "fake"}}

    def _sync(self, arguments):
        if arguments.get("execution_context") in SKIP_CONTEXTS:
            return {"status": "skipped", "reason": "execution_context"}
        self.turns.append(
            {
                "user_text": arguments["user_text"],
                "assistant_text": arguments.get("assistant_text", ""),
            }
        )
        return {
            "status": "captured",
            "source_memory_id": "mem-" + str(len(self.turns)),
            "derived_ids": [],
            "policy_proposal_ids": [],
        }


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.server = FakeMcpServer()

    def tearDown(self):
        self._tmp.cleanup()

    def make_provider(self, server=None, **config_overrides):
        values = {
            "binary": "mnemosyne",  # never spawned: the fake client is injected
            "db_path": os.path.join(self.home, "mnemosyne", "mnemosyne.db"),
            "hermes_home": self.home,
        }
        values.update(config_overrides)
        config = ProviderConfig(**values)
        return MnemosyneRustProvider(config, client=server if server is not None else self.server)


class TestProviderSurface(ProviderTestCase):
    def test_import_surface_and_register(self):
        self.assertEqual(PROVIDER_ID, "mnemosyne-rust")
        self.assertEqual(pkg.PROVIDER_ID, PROVIDER_ID)

        class Ctx:
            def __init__(self):
                self.registered = []

            def register_memory_provider(self, provider):
                self.registered.append(provider)

        ctx = Ctx()
        provider = pkg.register(ctx)
        self.assertEqual([p.name for p in ctx.registered], ["mnemosyne-rust"])
        self.assertIs(ctx.registered[0], provider)
        self.assertEqual(provider.pre_compress_checkpoint_api_version, 2)

    def test_required_members_exist(self):
        provider = self.make_provider()
        for member in (
            "is_available",
            "initialize",
            "get_tool_schemas",
            "handle_tool_call",
            "get_config_schema",
            "save_config",
            "system_prompt_block",
            "prefetch",
            "queue_prefetch",
            "sync_turn",
            "on_session_end",
            "on_pre_compress",
            "on_memory_write",
            "shutdown",
        ):
            self.assertTrue(callable(getattr(provider, member)), member)
        self.assertEqual(provider.name, "mnemosyne-rust")

    def test_is_available_has_no_side_effects(self):
        provider = self.make_provider(binary="mnemosyne-does-not-exist-xyz")
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.system_prompt_block(), "")
        self.assertIsNone(provider._client)  # no client built, no subprocess spawned
        self.assertEqual(self.server.calls, [])
        os_path = os.path.join(self.home, "not-executable")
        with open(os_path, "w", encoding="utf-8") as handle:
            handle.write("")
        provider.config.binary = os_path
        self.assertFalse(provider.is_available())

    def test_config_schema_and_save_config(self):
        provider = self.make_provider()
        fields = provider.get_config_schema()
        self.assertTrue(fields)
        for field in fields:
            self.assertIn("key", field)
            self.assertIn("description", field)
            self.assertIn("env_var", field)
        keys = {f["key"] for f in fields}
        self.assertIn("binary", keys)
        self.assertIn("namespace", keys)
        path = provider.save_config({"namespace": "agent:test"}, hermes_home=self.home)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(provider.config.namespace, "agent:test")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["values"]["namespace"], "agent:test")


class TestCaptureAndRecall(ProviderTestCase):
    def test_preference_is_recalled_in_a_fresh_session(self):
        first = self.make_provider()
        self.assertTrue(first.initialize("session-1", hermes_home=self.home))
        self.assertTrue(
            first.sync_turn(
                "I prefer dark mode in all editors", "Noted.", session_id="session-1"
            )
        )
        first.shutdown()  # flush queued capture
        self.assertEqual(len(self.server.turns), 1)

        fresh = self.make_provider()  # fresh session, no memory-tool request
        fresh.initialize("session-2", hermes_home=self.home)
        text = fresh.prefetch("what theme do I prefer?")
        self.assertIn("dark mode", text)  # (a)
        self.assertNotIn("<memory-context>", text)  # (c)
        search = fresh.handle_tool_call("mnemosyne_memory_search", {"query": "theme"})
        self.assertTrue(search["ok"])
        fresh.shutdown()

    def test_prefetch_text_has_no_memory_context_tags(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        provider.sync_turn("My editor theme is heliotrope", "", session_id="s")
        provider.shutdown()
        text = provider.prefetch("heliotrope theme")
        self.assertTrue(text)
        self.assertNotIn("<memory-context>", text)
        self.assertNotIn("memory-context", text)
        self.assertFalse(text.startswith("<"))

    def test_assistant_authored_and_empty_turns_are_never_captured(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        self.assertFalse(provider.sync_turn("", "assistant only answer"))
        self.assertFalse(provider.sync_turn("   ", "assistant only answer"))
        self.assertFalse(provider.sync_turn("user idea", "answer", speaker="assistant"))
        provider.shutdown()
        self.assertEqual(self.server.turns, [])

    def test_non_owner_policy_disables_capture(self):
        provider = self.make_provider(policy_owner="other-provider")
        provider.initialize("s", hermes_home=self.home)
        self.assertFalse(provider.sync_turn("I prefer vim", "ok"))
        provider.shutdown()
        self.assertEqual(self.server.turns, [])

    def test_queue_prefetch_is_async_and_cached(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        provider.sync_turn("My deployment region is eu-west", "", session_id="s")
        self.assertTrue(provider.queue_prefetch("which deployment region?", session_id="s"))
        provider.shutdown()
        self.assertIn("eu-west", provider.take_prefetched("s"))


class TestExecutionContextGating(ProviderTestCase):
    def test_skip_contexts_yield_empty_prefetch_and_no_capture(self):
        for context in sorted(SKIP_CONTEXTS):
            with self.subTest(context=context):
                server = FakeMcpServer()
                provider = self.make_provider(server)
                provider.initialize("s", hermes_home=self.home, agent_context=context)
                self.assertEqual(provider.prefetch("what do I prefer?"), "")
                self.assertFalse(provider.sync_turn("I prefer dark mode", "ok"))
                self.assertFalse(provider.queue_prefetch("what do I prefer?"))
                provider.shutdown()
                self.assertEqual(server.turns, [], context)
                self.assertEqual(server.calls, [], context)
                self.assertEqual(
                    provider.handle_tool_call("mnemosyne_memory_search", {"query": "x"})["ok"],
                    False,
                    context,
                )

    def test_per_call_context_override_is_gated(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)  # primary
        provider.sync_turn("I prefer dark mode", "ok", agent_context="cron")
        provider.shutdown()
        self.assertEqual(self.server.turns, [])
        self.assertEqual(self.server.calls, [])

    def test_primary_context_captures_and_forwards_context(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home, agent_context="primary")
        provider.sync_turn("I prefer dark mode", "ok")
        provider.shutdown()
        name, arguments = self.server.calls[0]
        self.assertEqual(name, "mnemosyne_sync_turn")
        self.assertEqual(arguments["user_text"], "I prefer dark mode")
        self.assertEqual(arguments["namespace"], "agent:hermes")
        self.assertEqual(arguments["policy_owner"], "mnemosyne-rust")
        self.assertEqual(arguments["execution_context"], "primary")


class TestDegradation(ProviderTestCase):
    def test_transport_failure_never_propagates(self):
        server = FakeMcpServer()
        server.fail_with = McpDisconnected("fake transport died")
        provider = self.make_provider(server)
        provider.initialize("s", hermes_home=self.home)
        self.assertEqual(provider.prefetch("what do I prefer?"), "")
        self.assertTrue(provider.sync_turn("I prefer dark mode", "ok"))
        provider.shutdown()  # must not raise even though captures fail

    def test_unexpected_transport_exception_never_propagates(self):
        server = FakeMcpServer()
        server.fail_with = RuntimeError("boom")
        provider = self.make_provider(server)
        provider.initialize("s", hermes_home=self.home)
        self.assertEqual(provider.prefetch("anything at all"), "")
        provider.shutdown()

    def test_empty_query_is_not_sent(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        self.assertEqual(provider.prefetch(""), "")
        self.assertEqual(provider.prefetch("   "), "")
        provider.shutdown()
        self.assertEqual(self.server.calls, [])


class TestCheckpoint(ProviderTestCase):
    def test_pre_compress_checkpoint_is_idempotent(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        messages = [{"role": "user", "content": "hi"}]
        self.assertTrue(provider.on_pre_compress(messages, require_checkpoint=True))
        self.assertTrue(provider.on_pre_compress(list(messages), require_checkpoint=True))
        checkpoints = os.path.join(provider.config.storage_dir(), "checkpoints")
        self.assertEqual(len(os.listdir(checkpoints)), 1)
        self.assertEqual(provider.checkpoints_written, 1)
        provider.shutdown()

    def test_pre_compress_is_fail_closed(self):
        blocker = os.path.join(self.home, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        provider = self.make_provider(db_path=os.path.join(blocker, "nested", "mnemosyne.db"))
        provider.initialize("s", hermes_home=self.home)
        with self.assertRaises(CheckpointError):
            provider.on_pre_compress([{"role": "user", "content": "hi"}], require_checkpoint=True)
        provider.shutdown()

    def test_memory_write_is_recorded_but_not_recaptured(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        self.assertFalse(provider.on_memory_write("add", "memory", "user likes tea"))
        provider.shutdown()
        self.assertEqual(provider.last_memory_write["content"], "user likes tea")
        self.assertEqual(self.server.turns, [])


class TestShutdown(ProviderTestCase):
    def test_shutdown_is_idempotent_and_flushes(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        self.assertTrue(provider.sync_turn("I prefer dark mode", "ok", session_id="s"))
        provider.shutdown()  # (e) flush happens here
        self.assertEqual(len(self.server.turns), 1)
        self.assertEqual(self.server.close_count, 1)
        calls = len(self.server.calls)
        provider.shutdown()
        provider.shutdown()
        self.assertEqual(self.server.close_count, 1)
        self.assertEqual(len(self.server.calls), calls)

    def test_on_session_end_drains_without_closing(self):
        provider = self.make_provider()
        provider.initialize("s", hermes_home=self.home)
        provider.sync_turn("I prefer dark mode", "ok")
        self.assertTrue(provider.on_session_end([{"role": "user", "content": "x"}]))
        self.assertEqual(len(self.server.turns), 1)
        self.assertEqual(self.server.close_count, 0)
        provider.shutdown()


# ---------------------------------------------------------------------------
# Real stdio: proves the newline-delimited JSON-RPC wire path via the
# production client, against a small scripted server.
# ---------------------------------------------------------------------------

STDIO_SERVER_SCRIPT = r'''
import json, os, sys

store = sys.argv[1]
SKIP = {"cron", "flush", "subagent", "background", "skill_loop"}

def load():
    if not os.path.exists(store):
        return []
    with open(store, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:
            continue
        params = msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "script-mnemosyne"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "mnemosyne_sync_turn"}, {"name": "mnemosyne_prefetch"}]}
        elif method == "tools/call" and params.get("name") == "mnemosyne_sync_turn":
            args = params.get("arguments") or {}
            if args.get("execution_context") in SKIP:
                result = {"structuredContent": {"status": "skipped"}, "content": []}
            else:
                with open(store, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"user_text": args["user_text"]}) + "\n")
                result = {"structuredContent": {"status": "captured"}, "content": []}
        elif method == "tools/call" and params.get("name") == "mnemosyne_prefetch":
            args = params.get("arguments") or {}
            text = "\n".join("- " + t["user_text"] for t in load())
            result = {"structuredContent": {"text": text, "count": len(load())}, "content": []}
        else:
            result = None
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "error": {"code": -32601, "message": "no method"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        sys.stdout.flush()

main()
'''


class TestRealStdioTransport(ProviderTestCase):
    def test_script_server_end_to_end(self):
        script = os.path.join(self.home, "fake_mnemosyne_mcp.py")
        store = os.path.join(self.home, "turns.jsonl")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(STDIO_SERVER_SCRIPT)
        config = ProviderConfig(
            binary=sys.executable,
            db_path=os.path.join(self.home, "mnemosyne", "mnemosyne.db"),
            hermes_home=self.home,
        )
        factory = lambda cfg: StdioJsonRpcClient(  # noqa: E731
            [sys.executable, "-u", script, store], request_timeout=20.0, initialize_timeout=20.0
        )
        provider = MnemosyneRustProvider(config, client_factory=factory)
        provider.initialize("session-1", hermes_home=self.home)
        self.assertTrue(provider.sync_turn("I prefer dark mode everywhere", "ok"))
        provider.shutdown()
        with open(store, encoding="utf-8") as handle:
            persisted = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(persisted, [{"user_text": "I prefer dark mode everywhere"}])

        fresh = MnemosyneRustProvider(config, client_factory=factory)
        fresh.initialize("session-2", hermes_home=self.home)
        text = fresh.prefetch("what do I prefer?")
        fresh.shutdown()
        self.assertIn("dark mode", text)
        self.assertNotIn("<memory-context>", text)


if __name__ == "__main__":
    unittest.main()
