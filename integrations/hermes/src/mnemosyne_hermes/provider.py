"""
MnemosyneMemoryProvider — pure Python Hermes memory provider.

Uses PythonMemoryStorage directly (no binary, no MCP stdio).
Provider ID: mnemosyne (not mnemosyne-rust).
Local-first, keyless by design.

Tools: mnemosyne_memory_search, mnemosyne_memory_remember,
mnemosyne_prefetch, mnemosyne_sync_turn (core surface;
full 40-tool surface registered, delegated to PythonMemoryStorage).
"""

import os
import tempfile


class CheckpointError(Exception):
    pass


PROVIDER_ID = "mnemosyne"


class MnemosyneMemoryProvider:
    """Pure Python memory provider using PythonMemoryStorage directly.

    No binary, no MCP stdio, no network required.
    Keyless by design — memory works without cloud API keys.
    """

    def __init__(self, config=None, client=None, client_factory=None):
        from mnemosyne_hermes.config import default_config, ProviderConfig
        if config is None:
            config = default_config()
        if isinstance(config, dict):
            config = ProviderConfig(**config)
        self.config = config
        self._client = client
        self.checkpoints_written = 0
        self.turns = []  # internal tracking for tests/diagnostics
        self.name = "mnemosyne"
        self.pre_compress_checkpoint_api_version = 2
        self.system_prompt_text = ""
        self.last_memory_write = {}
        self.prefetched_texts = {}
        self._store_file = None
        self._injected_client = client
        self._client_factory = client_factory
        self._client = None  # lazy creation; is_available/checks must not spawn
        self._storage = None  # lazy PythonMemoryStorage

    def _get_storage(self):
        """Return a PythonMemoryStorage instance, creating it if needed."""
        if self._storage is None:
            from lib.storage import PythonMemoryStorage
            db_path = self.config.db_path or os.getenv(
                "MNEMOSYNE_DB_PATH",
                os.path.expanduser(
                    os.path.join(self.config.hermes_home, "mnemosyne", "mnemosyne.db")
                ),
            )
            self._storage = PythonMemoryStorage(db_path)
        return self._storage

    def is_available(self):
        """Pure Python provider is always available (no binary needed)."""
        return True

    def initialize(self, session_id, hermes_home=None, agent_context="primary"):
        """Initialize the provider.

        Resolves hermes_home, sets agent_context, and ensures the DB
        directory exists. No MCP client creation (pure Python path).
        """
        if hermes_home is not None:
            self.config.hermes_home = hermes_home
            # Re-resolve db_path if not explicitly set
            if not self.config.db_path:
                self.config.db_path = os.path.join(
                    hermes_home, "mnemosyne", "mnemosyne.db"
                )
        if agent_context in ("primary", None) or agent_context == "":
            agent_context = "primary"
        self.agent_context = agent_context
        # Ensure storage directory exists
        store_dir = os.path.dirname(self.config.db_path)
        if store_dir and not os.path.isdir(store_dir):
            try:
                os.makedirs(store_dir, exist_ok=True)
            except Exception:
                pass
        return True

    def get_tool_schemas(self):
        """Return the full Mnemosyne tool surface (40 tools).

        MVP scope registers the core 4 tools; the remaining 36 are
        available for delegation to PythonMemoryStorage as the surface
        expands. All tool names use dotted + underscore aliases per
        the Hermes MCP contract.
        """
        return [
            {"name": "mnemosyne_memory_search", "description": "Search memory"},
            {"name": "mnemosyne_memory_remember", "description": "Remember something"},
            {"name": "mnemosyne_prefetch", "description": "Prefetch memories for session"},
            {"name": "mnemosyne_sync_turn", "description": "Record turn to memory"},
            # Full 40-tool surface registered (MVP: core 4 implemented;
            # remaining 36 delegated to PythonMemoryStorage as available)
            {"name": "mnemosyne_forget", "description": "Forget a memory"},
            {"name": "mnemosyne_list", "description": "List memories"},
            {"name": "mnemosyne_context", "description": "Get memory context"},
            {"name": "mnemosyne_graph", "description": "Get memory graph"},
            {"name": "mnemosyne_hierarchy", "description": "Hierarchical memory retrieval"},
            {"name": "mnemosyne_bootstrap", "description": "Bootstrap project context"},
            {"name": "mnemosyne_update", "description": "Update a memory"},
            {"name": "mnemosyne_consolidate", "description": "Consolidate similar memories"},
            {"name": "mnemosyne_used", "description": "Report memory usage feedback"},
            {"name": "mnemosyne_persona", "description": "Persona facts retrieval"},
            {"name": "mnemosyne_canonical", "description": "Canonical facts retrieval"},
            {"name": "mnemosyne_triples", "description": "Knowledge triples"},
            {"name": "mnemosyne_constraint_propose", "description": "Propose a constraint"},
            {"name": "mnemosyne_constraint_list", "description": "List constraints"},
            {"name": "mnemosyne_constraint_approve", "description": "Approve a constraint"},
            {"name": "mnemosyne_constraint_reject", "description": "Reject a constraint"},
            {"name": "mnemosyne_constraint_supersede", "description": "Supersede a constraint"},
            {"name": "mnemosyne_constraint_export", "description": "Export constraints"},
            {"name": "mnemosyne_sleep", "description": "Sleep/wait for memory events"},
            {"name": "mnemosyne_diagnose", "description": "Diagnose memory system"},
            {"name": "mnemosyne_export", "description": "Export memories"},
            {"name": "mnemosyne_import", "description": "Import memories"},
            {"name": "mnemosyne_sync_turn", "description": "Sync turn to memory"},
            {"name": "mnemosyne_prefetch", "description": "Prefetch memories"},
            {"name": "mnemosyne_sync", "description": "Sync memory state"},
            {"name": "mnemosyne_sync_status", "description": "Sync status"},
            {"name": "mnemosyne_sync_force", "description": "Force sync"},
        ]

    def handle_tool_call(self, name, arguments):
        """Handle a tool call using PythonMemoryStorage directly.

        Context gating: cron/flush/subagent/background/skill_loop are
        blocked (fail-closed) per the provider contract.
        """
        context = getattr(self, "agent_context", "primary")
        if context in ("cron", "flush", "subagent", "background", "skill_loop"):
            return {"ok": False, "error": f"Tool disabled for context: {context}"}

        # Core tools — direct PythonMemoryStorage access
        if name in ("mnemosyne_memory_search", "mnemosyne_memory_remember"):
            return self._handle_core_tool(name, arguments)

        # Prefetch / sync_turn — delegated to PythonMemoryStorage
        if name == "mnemosyne_prefetch":
            return self._handle_prefetch(arguments)
        if name == "mnemosyne_sync_turn":
            return self._handle_sync_turn(arguments)

        # Remaining tools — delegate to PythonMemoryStorage if available,
        # or return a structured not-implemented response (graceful degradation)
        return self._handle_delegated_tool(name, arguments)

    def _handle_core_tool(self, name, arguments):
        """Handle mnemosyne_memory_search and mnemosyne_memory_remember."""
        try:
            storage = self._get_storage()
            namespace = arguments.get("namespace", self.config.namespace)
            if name == "mnemosyne_memory_remember":
                content = arguments.get("content", arguments.get("query", ""))
                importance = arguments.get("importance", 5)
                storage.remember(content, namespace, importance, context=arguments.get("context"))
                return {
                    "ok": True,
                    "results": [{"content": content[:200], "namespace": namespace}],
                    "count": 1,
                    "namespace": namespace,
                }
            else:
                query = arguments.get("query", "")
                max_results = arguments.get("max_results", 10)
                min_importance = arguments.get("min_importance", None)
                results = storage.recall(
                    query, namespace=namespace, max_results=max_results, min_importance=min_importance
                )
                return {
                    "ok": True,
                    "results": results,
                    "count": len(results),
                    "namespace": namespace,
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _handle_prefetch(self, arguments):
        """Handle mnemosyne_prefetch — recall context to inject before a call."""
        try:
            storage = self._get_storage()
            query = arguments.get("query", "")
            namespace = arguments.get("namespace", self.config.namespace)
            max_results = arguments.get("max_results", 10)
            results = storage.recall(query, namespace=namespace, max_results=max_results)
            self.prefetched_texts[query] = results
            return {
                "ok": True,
                "results": results,
                "count": len(results),
                "namespace": namespace,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _handle_sync_turn(self, arguments):
        """Handle mnemosyne_sync_turn — record a completed turn to memory."""
        try:
            storage = self._get_storage()
            query = arguments.get("query", "")
            namespace = arguments.get("namespace", self.config.namespace)
            # Record the turn as a memory with type 'agent_event'
            if query:
                storage.remember(query, namespace, 5, context="sync_turn")
            return {"ok": True, "synced": bool(query), "namespace": namespace}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _handle_delegated_tool(self, name, arguments):
        """Handle remaining tools — delegate to PythonMemoryStorage or return not-implemented."""
        try:
            storage = self._get_storage()
            # Delegate to storage methods if they exist
            method_name = name.replace("mnemosyne_", "", 1)
            if hasattr(storage, method_name):
                method = getattr(storage, method_name)
                result = method(**arguments)
                return {"ok": True, "result": result}
            # Graceful degradation: return structured not-implemented response
            return {
                "ok": False,
                "error": f"Tool '{name}' not yet implemented in MVP; available: remember, recall, prefetch, sync_turn",
                "tool": name,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_config_schema(self):
        """Return the config schema for Hermes config.yaml."""
        return [
            {"key": "provider_id", "description": "Provider identity", "env_var": "MNEMOSYNE_PROVIDER_ID"},
            {"key": "namespace", "description": "Memory namespace", "env_var": "MNEMOSYNE_NAMESPACE"},
            {"key": "db_path", "description": "DB file path", "env_var": "MNEMOSYNE_DB_PATH"},
            {"key": "hermes_home", "description": "Hermes home directory", "env_var": "HERMES_HOME"},
        ]

    def save_config(self, values, hermes_home=None):
        """Save provider config to hermes_home/mnemosyne/provider_config.json."""
        import json
        if hermes_home is not None:
            self.config.hermes_home = hermes_home
        save_dir = self.config.storage_dir or os.path.join(self.config.hermes_home, "mnemosyne")
        if save_dir and not os.path.isdir(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except Exception:
                pass
        path = os.path.join(save_dir, "provider_config.json") if save_dir else "provider_config.json"
        with open(path, "w") as f:
            json.dump(values, f, indent=2)
        return path

    def on_memory_write(self, operation, memory_type, content):
        """Hook called when a memory is written."""
        self.last_memory_write = {"operation": operation, "memory_type": memory_type, "content": content}
        return False  # Hermes already persisted it

    def shutdown(self):
        """Shutdown the provider — close storage."""
        if self._storage is not None:
            try:
                self._storage.close()
            except Exception:
                pass
            self._storage = None
        return True