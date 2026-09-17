"""Pure Python Hermes memory provider. Local storage, no external service."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from lib.storage import PythonMemoryStorage
from mnemosyne.tools import SKIP_CONTEXTS, call_tool, tool_schemas
from .config import ProviderConfig, default_config

PROVIDER_ID = "mnemosyne"


class CheckpointError(Exception):
    pass


class MnemosyneMemoryProvider:
    name = PROVIDER_ID
    pre_compress_checkpoint_api_version = 2

    def __init__(self, config=None):
        self.config = ProviderConfig(**config) if isinstance(config, dict) else config or default_config()
        self._storage = None
        self.agent_context = "primary"
        self.checkpoints_written = 0
        self.prefetched_texts = {}
        self.last_memory_write = {}

    def _get_storage(self):
        if self._storage is None:
            self._storage = PythonMemoryStorage(self.config.resolved_db_path())
        return self._storage

    def _allowed(self, agent_context=None):
        return (agent_context or self.agent_context) not in SKIP_CONTEXTS

    def is_available(self):
        return True  # stdlib backend; no initialization or filesystem side effects

    def initialize(self, session_id, hermes_home=None, agent_context="primary", **kwargs):
        self.shutdown()
        if hermes_home is not None:
            self.config.hermes_home = hermes_home
        self.agent_context = agent_context or "primary"
        return True

    def system_prompt_block(self):
        return ""

    def get_tool_schemas(self):
        return tool_schemas()

    def handle_tool_call(self, name, arguments, **kwargs):
        if not self._allowed(kwargs.get("agent_context")):
            return {"ok": False, "error": "Tool disabled for this execution context"}
        try:
            if name in ("mnemosyne_sync_turn", "mnemosyne.sync_turn"):
                if self.config.policy_owner not in ("", self.name):
                    return {"ok": True, "synced": False, "status": "skipped"}
            return call_tool(self._get_storage(), name, arguments, self.config.namespace)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def prefetch(self, query, session_id=None, agent_context=None):
        if not self._allowed(agent_context) or not isinstance(query, str) or not query.strip():
            return ""
        result = self.handle_tool_call("mnemosyne_prefetch", {"query": query}, agent_context=agent_context)
        return result.get("text", "")

    def queue_prefetch(self, query, session_id=None, agent_context=None):
        if not self._allowed(agent_context) or not isinstance(query, str) or not query.strip():
            return False
        # ponytail: synchronous local recall; add a worker only if measured
        # SQLite latency makes turn completion noticeably slow.
        self.prefetched_texts[session_id] = self.prefetch(query, session_id, agent_context)
        return True

    def take_prefetched(self, session_id):
        return self.prefetched_texts.pop(session_id, "") if self._allowed() else ""

    def sync_turn(self, user_text, assistant_text="", session_id="default", agent_context=None,
                  speaker="user", **kwargs):
        if not self._allowed(agent_context) or speaker != "user":
            return False
        result = self.handle_tool_call("mnemosyne_sync_turn", {
            "user_text": user_text, "assistant_text": assistant_text,
            "session_id": session_id, "speaker": speaker,
        }, agent_context=agent_context)
        return result.get("synced", False)

    def on_session_end(self, messages):
        if self._storage is not None:
            self._storage.close()  # writes are synchronous; flush access counters
        self.prefetched_texts.clear()
        return True

    def on_pre_compress(self, messages, require_checkpoint=False):
        if not require_checkpoint:
            return ""
        temporary = None
        try:
            content = json.dumps({"messages": messages}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            directory = Path(self.config.resolved_storage_dir()) / "checkpoints"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / f"checkpoint-{digest}.json"
            if target.exists() and target.read_bytes() == content:
                return True
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                temporary = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            if os.name != "nt":
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.checkpoints_written += 1
            return True
        except Exception as exc:
            raise CheckpointError(str(exc)) from exc
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def get_config_schema(self):
        return [{"key": key, "description": description, "env_var": env}
                for key, description, env in (
                    ("namespace", "Memory namespace", "MNEMOSYNE_NAMESPACE"),
                    ("db_path", "SQLite database path", "MNEMOSYNE_DB_PATH"),
                    ("hermes_home", "Hermes profile directory", "HERMES_HOME"))]

    def save_config(self, values, hermes_home=None):
        if hermes_home is not None:
            self.config.hermes_home = hermes_home
        allowed = {field["key"] for field in self.get_config_schema()}
        if not isinstance(values, dict) or any(k not in allowed or not isinstance(v, str) for k, v in values.items()):
            raise ValueError("Invalid provider configuration")
        self.shutdown()
        for key, value in values.items():
            setattr(self.config, key, value)
        directory = Path(self.config.resolved_storage_dir())
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "provider_config.json"
        path.write_text(json.dumps({"values": values}), encoding="utf-8")
        return str(path)

    def on_memory_write(self, operation, memory_type, content):
        self.last_memory_write = {"operation": operation, "memory_type": memory_type, "content": content}
        return False

    def shutdown(self):
        if self._storage is not None:
            self._storage.close()
            self._storage = None
        self.prefetched_texts.clear()
        return True
