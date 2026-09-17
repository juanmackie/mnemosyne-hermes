"""Native provider contract checks; runnable with stdlib unittest."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "integrations/hermes/src")]
from mnemosyne_hermes import MnemosyneMemoryProvider, ProviderConfig
from mnemosyne_hermes.provider import CheckpointError


class NativeProviderTests(unittest.TestCase):
    def test_checkpoint_preserves_messages_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            provider = MnemosyneMemoryProvider(ProviderConfig(hermes_home=home))
            messages = [{"role": "user", "content": "Remember my preference"}]
            self.assertTrue(provider.on_pre_compress(messages, require_checkpoint=True))
            self.assertTrue(provider.on_pre_compress(messages, require_checkpoint=True))
            files = list(Path(provider.config.resolved_storage_dir()).glob("checkpoints/*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["messages"], messages)
            self.assertEqual(provider.checkpoints_written, 1)

    def test_checkpoint_failure_blocks_compression(self):
        with tempfile.TemporaryDirectory() as home:
            blocker = Path(home) / "blocker"
            blocker.write_text("not a directory")
            provider = MnemosyneMemoryProvider(ProviderConfig(storage_dir=str(blocker)))
            with self.assertRaises(CheckpointError):
                provider.on_pre_compress([], require_checkpoint=True)


if __name__ == "__main__":
    unittest.main()
