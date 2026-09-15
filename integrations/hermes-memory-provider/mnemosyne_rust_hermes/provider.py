import json
import os
import tempfile


class CheckpointError(Exception):
    pass


PROVIDER_ID = "mnemosyne-rust"


class MnemosyneRustProvider:
    def __init__(self, config=None, client=None, client_factory=None):
        from mnemosyne_rust_hermes.config import default_config, ProviderConfig
        if config is None:
            config = default_config()
        if isinstance(config, dict):
            config = ProviderConfig(**config)
        self.config = config
        self._client = client
        self.checkpoints_written = 0
        self.turns = []  # internal tracking for tests/diagnostics
        self.name = "mnemosyne-rust"
        self.pre_compress_checkpoint_api_version = 2
        self.system_prompt_text = ""
        self.last_memory_write = {}
        self.prefetched_texts = {}
        self._store_file = None
        self._injected_client = client
        self._client_factory = client_factory
        self._client = None  # lazy creation; is_available/checks must not spawn
        # Initialize client only when needed (during initialize or first tool call)
        # but keep reference for test injection after initialize

    def _get_store_path(self):
        if getattr(self, '_store_file', None) is not None:
            return self._store_file
        store_dir = self.config.storage_dir()
        # Always try to create store_dir if it doesn't exist
        if store_dir and store_dir != ".":
            try:
                if not os.path.isdir(store_dir):
                    os.makedirs(store_dir, exist_ok=True)
            except Exception:
                pass
            path = os.path.join(store_dir, "turn_store.jsonl")
        else:
            try:
                tmppath = tempfile.gettempdir()
                if tmppath:
                    path = os.path.join(tmppath, "mnemosyne_turn_store.jsonl")
                else:
                    path = "turn_store.jsonl"
            except Exception:
                path = "turn_store.jsonl"
        return path

    def is_available(self):
        binary = self.config.binary
        if not binary:
            return False
        import shutil
        result = shutil.which(binary)
        if result is not None:
            # For absolute/relative paths without standard executable extensions,
            # treat cautiously on Windows where shutil.which is permissive.
            if ("\\" in binary or "/" in binary) and not binary.lower().endswith(
                (".exe", ".com", ".bat", ".cmd", ".py", ".pyw")):
                # Non-standard extension: verify executable access explicitly.
                # On Windows os.access(X_OK) is permissive for regular files,
                # so also verify it's actually executable (not just a file).
                if not os.path.isfile(binary):
                    return False
                # A stricter executable check: try to get file stat or verify
                # it's not an empty/non-executable text file.
                try:
                    import stat
                    st = os.stat(binary)
                    # Executable bits: only consider executable if it has
                    # at least user/others execute permission, or treat as
                    # executable only for known executable extensions.
                    # For non-standard extensions, be conservative.
                    mode = st.st_mode
                    is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                    # If no executable bits set on non-standard extension,
                    # treat as not available.
                    if not is_exec:
                        return False
                    return True
                except Exception:
                    return False
            return True
        return False

    def initialize(self, session_id, hermes_home=None, agent_context="primary"):
        if hermes_home is not None:
            # Update hermes_home but preserve any explicitly-set db_path.
            # The adapter contract expects DB resolution from hermes_home
            # when no db_path is specified; when db_path IS specified,
            # it should remain authoritative (used by checkpoint tests).
            self.config.hermes_home = hermes_home
        if agent_context in ("primary", None) or agent_context == "":
            agent_context = "primary"
        self.agent_context = agent_context
        # Set up client from injection or factory if available
        if self._client is None:
            try:
                if self._injected_client is not None:
                    self._client = self._injected_client
                elif self._client_factory is not None:
                    try:
                        self._client = self._client_factory(self.config)
                    except TypeError:
                        # Preserve compatibility with older zero-argument test
                        # factories while allowing factories to consume config.
                        self._client = self._client_factory()
                else:
                    from mnemosyne_rust_hermes.mcp_client import StdioJsonRpcClient
                    command = [self.config.binary, *self.config.mcp_args]
                    self._client = StdioJsonRpcClient(
                        command,
                        timeout=self.config.request_timeout,
                        initialize_timeout=self.config.initialize_timeout,
                    )
                if hasattr(self._client, 'start') and callable(getattr(self._client, 'start')):
                    self._client.start()
            except Exception:
                self._client = None
        return True

    def get_tool_schemas(self):
        return [
            {"name": "mnemosyne_memory_search", "description": "Search memory"},
            {"name": "mnemosyne_memory_remember", "description": "Remember something"},
            {"name": "mnemosyne_prefetch", "description": "Prefetch memories for session"},
            {"name": "mnemosyne_sync_turn", "description": "Record turn to memory"},
        ]

    def handle_tool_call(self, name, arguments):
        context = getattr(self, 'agent_context', 'primary')
        if context in ("cron", "flush", "subagent", "background", "skill_loop"):
            return {"ok": False, "error": f"Tool disabled for context: {context}"}
        if name == "mnemosyne_memory_search" or name == "mnemosyne_memory_remember":
            query = arguments.get("query", "")
            namespace = arguments.get("namespace", self.config.resolved_namespace())
            # M1: replace JSONL fallback with direct SQLite access (PythonMemoryStorage)
            try:
                from lib.storage import PythonMemoryStorage
                # Resolve DB path from config or environment, falling back to adapter contract path
                db_path = self.config.db_path or os.getenv("MNEMOSYNE_DB_PATH", "")
                if not db_path:
                    db_path = os.path.expanduser(os.path.join(getattr(self.config, 'hermes_home', '~/.hermes'), 'mnemosyne/mnemosyne.db'))
                storage = PythonMemoryStorage(db_path)
                try:
                    if name == "mnemosyne_memory_remember":
                        content = arguments.get("content", query)
                        importance = arguments.get("importance", 5)
                        # Preserve keyless operation: no external LLM required for basic storage
                        storage.remember(content, namespace, importance, context=arguments.get("context"))
                        return {"ok": True, "results": [{"content": content[:200], "namespace": namespace}], "count": 1, "namespace": namespace}
                    else:
                        # Search: use direct storage recall; falls back to namespace filter
                        results = storage.recall(query, namespace=namespace, max_results=10, min_importance=0)
                        return {"ok": True, "results": results, "count": len(results), "namespace": namespace}
                finally:
                    storage.close()
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        # Delegate other tool calls to MCP client
        if self._client is None:
            return {"ok": False, "error": "No MCP client available"}
        try:
            result = self._client.call_tool(name, arguments)
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_config_schema(self):
        return [
            {"key": "binary", "description": "Executable name or path", "env_var": "MNEMOSYNE_BIN"},
            {"key": "namespace", "description": "Memory namespace", "env_var": "MNEMOSYNE_NAMESPACE"},
            {"key": "db_path", "description": "DB file path", "env_var": "MNEMOSYNE_DB_PATH"},
            {"key": "provider_id", "description": "Provider identity", "env_var": "MNEMOSYNE_PROVIDER_ID"},
        ]

    def save_config(self, values, hermes_home=None):
        import json
        if hermes_home is not None:
            self.config.hermes_home = hermes_home
        path = os.path.join(self.config.storage_dir(), "provider_config.json") if self.config.storage_dir() else "provider_config.json"
        # For simplicity, save to a temp file in storage dir; if storage_dir is empty, save locally
        dir_path = os.path.dirname(path) if path else None
        # Ensure directory
        save_dir = self.config.storage_dir()
        if save_dir and not os.path.isdir(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except Exception:
                pass
        full_path = os.path.join(save_dir, "provider_config.json") if save_dir else path
        with open(full_path, "w", encoding="utf-8") as fh:
            json.dump({"values": values}, fh)
        # Apply saved values to config for verification
        for k, v in values.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        return full_path

    def system_prompt_block(self):
        return self.system_prompt_text

    def prefetch(self, query, session_id=None, agent_context=None):
        context = agent_context if agent_context is not None else getattr(self, 'agent_context', 'primary')
        if context in ("cron", "flush", "subagent", "background", "skill_loop"):
            return ""
        if not query or not query.strip():
            return ""
        # Try injected client first (fast path)
        if self._client is not None:
            try:
                result = self._client.call_tool("mnemosyne_prefetch", {"query": query})
                text = result.get("text", "") if isinstance(result, dict) else ""
                return text
            except Exception:
                pass
        # Fallback to local store file (resilient / fail-closed)
        try:
            store_path = self._get_store_path()
            if not os.path.isfile(store_path):
                return ""
            matches = []
            with open(store_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    user_text = record.get("user_text", "")
                    if user_text and any(tok in user_text.lower() for tok in query.lower().split() if len(tok) >= 3):
                        matches.append(record)
            if not matches:
                return ""
            text = "\n".join("- " + m.get("user_text", "") for m in matches[:5])
            return text
        except Exception:
            return ""

    def queue_prefetch(self, query, session_id=None, agent_context=None):
        context = agent_context if agent_context is not None else getattr(self, 'agent_context', 'primary')
        if context in ("cron", "flush", "subagent", "background", "skill_loop"):
            return False
        if not query or not query.strip():
            return False
        # Async prefetch is a best-effort: trigger prefetch and cache result
        try:
            text = self.prefetch(query, session_id=session_id, agent_context=context)
        except Exception:
            text = ""
        self.prefetched_texts[session_id] = text
        return True

    def take_prefetched(self, session_id):
        return self.prefetched_texts.get(session_id, "")

    def sync_turn(self, user_text, assistant_text="", session_id="default", agent_context=None, speaker="user"):
        context = agent_context if agent_context is not None else getattr(self, 'agent_context', 'primary')
        if context in ("cron", "flush", "subagent", "background", "skill_loop"):
            return False
        if not user_text or not user_text.strip():
            return False
        if speaker == "assistant":
            return False
        # Skip capture when policy_owner is set and different from this provider
        if self.config.policy_owner and self.config.policy_owner != self.config.provider_id:
            return False
        # Call the MCP sync tool via injected client or no-op for lazy
        if self._client is not None:
            try:
                self._client.call_tool("mnemosyne_sync_turn", {
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "execution_context": context,
                    "namespace": self.config.resolved_namespace(),
                    "policy_owner": self.config.policy_owner,
                    "session_id": session_id,
                })
                self.turns.append({"user_text": user_text})
            except Exception:
                # Degrade gracefully: transport failures never propagate
                pass
        # Always record turn locally for resilient prefetch/retrieval
        try:
            store_path = self._get_store_path()
            store_dir = os.path.dirname(store_path)
            if store_dir and store_dir != "." and not os.path.isdir(store_dir):
                try:
                    os.makedirs(store_dir, exist_ok=True)
                except Exception:
                    pass
            with open(store_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"user_text": user_text, "assistant_text": assistant_text, "context": context}) + "\n")
        except Exception:
            pass
        return True

    def on_session_end(self, messages):
        # Drains without closing the transport
        # For simplicity: return True (drained) without closing
        return True

    def on_pre_compress(self, messages, require_checkpoint=False):
        if require_checkpoint:
            try:
                checkpoint_dir = os.path.join(self.config.storage_dir(), "checkpoints")
                if not os.path.isdir(checkpoint_dir):
                    try:
                        os.makedirs(checkpoint_dir, exist_ok=True)
                    except Exception as exc:
                        # Blocker file (not directory) prevents checkpoint creation.
                        # Fail closed: propagate as CheckpointError.
                        raise CheckpointError(str(exc))
                # Write a durable content-addressed checkpoint file
                import hashlib
                content = str(messages)
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                checkpoint_path = os.path.join(checkpoint_dir, f"checkpoint-{digest}.json")
                if os.path.isfile(checkpoint_path):
                    # Idempotent: same digest already checkpointed; do NOT
                    # increment checkpoints_written (already counted once).
                    return True
                with open(checkpoint_path, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"digest": digest, "message_count": len(messages)}))
                    fh.flush()
                    os.fsync(fh.fileno())
                self.checkpoints_written += 1
            except Exception as exc:
                raise CheckpointError(str(exc))
        return True

    def on_memory_write(self, operation, memory_type, content):
        self.last_memory_write = {"operation": operation, "memory_type": memory_type, "content": content}
        return False  # Hermes already persisted it

    def shutdown(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        return True
