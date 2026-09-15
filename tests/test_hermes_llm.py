import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestration.hermes_llm import get_llm, resolve_hermes_model


class _Handler(BaseHTTPRequestHandler):
    payload = None
    auth = None

    def do_POST(self):
        _Handler.payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.auth = self.headers.get("Authorization")
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class HermesLLMTests(unittest.TestCase):
    def test_active_model_uses_proxy_without_anthropic_key(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "config.yaml").write_text(
                "model:\n  provider: nous\n  default: Hermes-4-70B\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}/v1"
                with patch.dict(os.environ, {
                    "HERMES_HOME": home,
                    "HERMES_PROXY_BASE_URL": base,
                    "ANTHROPIC_API_KEY": "must-not-be-used",
                }, clear=False):
                    config = resolve_hermes_model()
                    client = get_llm()
                    result = client.chat([{"role": "user", "content": "hello"}])

                self.assertEqual(config.model, "Hermes-4-70B")
                self.assertEqual(client.model, "Hermes-4-70B")
                self.assertIsNone(client.api_key)
                self.assertEqual(result["choices"][0]["message"]["content"], "ok")
                self.assertEqual(_Handler.payload["model"], "Hermes-4-70B")
                self.assertEqual(_Handler.auth, "Bearer hermes-proxy")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_missing_hermes_home_keeps_legacy_fallback_available(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(
            os.environ,
            {"HERMES_HOME": home, "ANTHROPIC_API_KEY": "legacy"},
            clear=False,
        ):
            client = get_llm()
            self.assertIsNone(client.hermes)
            self.assertEqual(client.api_key, "legacy")


if __name__ == "__main__":
    unittest.main()
