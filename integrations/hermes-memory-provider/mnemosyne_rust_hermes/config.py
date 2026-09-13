"""Environment-driven configuration with sane defaults.

Every setting is overridable by environment variable so the adapter never has
to read or write Mnemosyne's own config files:

===============================  ============================================
Variable                         Meaning / default
===============================  ============================================
MNEMOSYNE_BIN                    Executable; absolute paths allowed.
                                 Default "mnemosyne".
MNEMOSYNE_MCP_ARGS               Arguments appended to MNEMOSYNE_BIN.
                                 Default "mcp". Accepts a JSON array or a
                                 shell-style string (shlex).
MNEMOSYNE_DB_PATH                Database file. Default
                                 <hermes_home>/mnemosyne/mnemosyne.db
MNEMOSYNE_HERMES_HOME            Override the active Hermes home used to
                                 resolve storage when MNEMOSYNE_DB_PATH is
                                 unset. Default: the hermes_home kwarg from
                                 MemoryProvider.initialize.
MNEMOSYNE_NAMESPACE              Memory namespace. Default "agent:hermes".
MNEMOSYNE_POLICY_OWNER           Who owns automatic capture. Default
                                 MNEMOSYNE_PROVIDER_ID.
MNEMOSYNE_PROVIDER_ID            Reported provider / policy identity.
                                 Default "mnemosyne-rust".
MNEMOSYNE_EXECUTION_CONTEXT      Optional default execution context.
MNEMOSYNE_REQUEST_TIMEOUT        Per JSON-RPC request timeout (s). Default 10.
MNEMOSYNE_INITIALIZE_TIMEOUT     MCP initialize handshake timeout (s). 15.
MNEMOSYNE_PREFETCH_TIMEOUT       Bounded prefetch timeout (s). Default 3.
MNEMOSYNE_SHUTDOWN_TIMEOUT       Drain/flush timeout (s). Default 5.
MNEMOSYNE_EAGER_CONNECT          "1" to connect during initialize()
                                 instead of lazily. Default off.
===============================  ============================================
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PROVIDER_ID_DEFAULT = "mnemosyne-rust"
NAMESPACE_DEFAULT = "agent:hermes"
DEFAULT_MCP_ARGS = ("mcp",)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_mcp_args(raw: Optional[str]) -> List[str]:
    """Parse MNEMOSYNE_MCP_ARGS (JSON array or shlex string)."""
    if raw is None or raw.strip() == "":
        return list(DEFAULT_MCP_ARGS)
    text = raw.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            return list(DEFAULT_MCP_ARGS)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return list(parsed)
        return list(DEFAULT_MCP_ARGS)
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


@dataclass
class ProviderConfig:
    provider_id: str = PROVIDER_ID_DEFAULT
    binary: str = "mnemosyne"
    mcp_args: List[str] = field(default_factory=lambda: list(DEFAULT_MCP_ARGS))
    db_path: Optional[str] = None
    namespace: str = NAMESPACE_DEFAULT
    hermes_home: Optional[str] = None
    policy_owner: Optional[str] = None
    execution_context: Optional[str] = None
    request_timeout: float = 10.0
    initialize_timeout: float = 15.0
    prefetch_timeout: float = 3.0
    shutdown_timeout: float = 5.0
    eager_connect: bool = False
    extra_env: Dict[str, str] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------

    @property
    def command(self) -> List[str]:
        return [self.binary, *self.mcp_args]

    @property
    def effective_policy_owner(self) -> str:
        return self.policy_owner or self.provider_id

    @property
    def is_capture_owner(self) -> bool:
        """Exactly one automatic capture owner.

        Defaults to True because policy_owner defaults to provider_id. When an
        operator names a different owner, this provider performs NO automatic
        capture at all (there is deliberately no legacy fallback that could
        reactivate capture in a skipped context).
        """
        return self.effective_policy_owner.strip().lower() == self.provider_id.strip().lower()

    def resolved_db_path(self) -> str:
        if self.db_path:
            return os.path.abspath(os.path.expanduser(self.db_path))
        home = self.hermes_home or os.path.join(os.path.expanduser("~"), ".hermes")
        home = os.path.abspath(os.path.expanduser(home))
        return os.path.join(home, "mnemosyne", "mnemosyne.db")

    def storage_dir(self) -> str:
        return os.path.dirname(self.resolved_db_path())

    def child_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update(self.extra_env)
        env["MNEMOSYNE_DB_PATH"] = self.resolved_db_path()
        env["MNEMOSYNE_NAMESPACE"] = self.namespace
        env["MNEMOSYNE_POLICY_OWNER"] = self.effective_policy_owner
        env["MNEMOSYNE_PROVIDER_ID"] = self.provider_id
        if self.hermes_home:
            env["MNEMOSYNE_HERMES_HOME"] = self.hermes_home
        return env


def from_env(hermes_home: Optional[str] = None, **overrides: object) -> ProviderConfig:
    """Build a ProviderConfig from the environment.

    Resolution order for hermes_home: explicit argument > MNEMOSYNE_HERMES_HOME
    > the hermes_home kwarg supplied by MemoryProvider.initialize.
    """
    provider_id = os.environ.get("MNEMOSYNE_PROVIDER_ID", PROVIDER_ID_DEFAULT) or PROVIDER_ID_DEFAULT
    resolved_home = os.environ.get("MNEMOSYNE_HERMES_HOME") or hermes_home
    cfg = ProviderConfig(
        provider_id=provider_id,
        binary=os.environ.get("MNEMOSYNE_BIN", "mnemosyne") or "mnemosyne",
        mcp_args=parse_mcp_args(os.environ.get("MNEMOSYNE_MCP_ARGS")),
        db_path=os.environ.get("MNEMOSYNE_DB_PATH") or None,
        namespace=os.environ.get("MNEMOSYNE_NAMESPACE", NAMESPACE_DEFAULT) or NAMESPACE_DEFAULT,
        hermes_home=resolved_home,
        policy_owner=os.environ.get("MNEMOSYNE_POLICY_OWNER") or None,
        execution_context=os.environ.get("MNEMOSYNE_EXECUTION_CONTEXT") or None,
        request_timeout=_env_float("MNEMOSYNE_REQUEST_TIMEOUT", 10.0),
        initialize_timeout=_env_float("MNEMOSYNE_INITIALIZE_TIMEOUT", 15.0),
        prefetch_timeout=_env_float("MNEMOSYNE_PREFETCH_TIMEOUT", 3.0),
        shutdown_timeout=_env_float("MNEMOSYNE_SHUTDOWN_TIMEOUT", 5.0),
        eager_connect=_env_bool("MNEMOSYNE_EAGER_CONNECT", False),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def default_config() -> ProviderConfig:
    return from_env()
