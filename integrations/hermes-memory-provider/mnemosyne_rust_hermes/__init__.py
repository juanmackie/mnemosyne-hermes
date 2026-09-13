"""Hermes Agent MemoryProvider adapter for the Mnemosyne Rust binary.

This package is intentionally INDEPENDENT of Mnemosyne's own PyO3/orchestration
Python package: it is a pure-stdlib client that talks to the ``mnemosyne``
binary over a persistent, newline-delimited JSON-RPC 2.0 MCP stdio session.

Registration entry point (see ``pyproject.toml``):

    [project.entry-points."hermes_agent.memory_providers"]
    mnemosyne-rust = "mnemosyne_rust_hermes:register"

The entry-point name is the provider id users select with
``memory.provider: mnemosyne-rust``. It is deliberately *not* "mnemosyne" so it
can never shadow an installed Python "mnemosyne" provider.
"""

from __future__ import annotations

from typing import Any

from .config import (
    PROVIDER_ID_DEFAULT,
    ProviderConfig,
    default_config,
)
from .contexts import SKIP_CONTEXTS
from .provider import PROVIDER_ID, MnemosyneRustProvider

__version__ = "0.1.0"

__all__ = [
    "MnemosyneRustProvider",
    "ProviderConfig",
    "PROVIDER_ID",
    "PROVIDER_ID_DEFAULT",
    "SKIP_CONTEXTS",
    "default_config",
    "register",
    "__version__",
]


def register(ctx: Any) -> MnemosyneRustProvider:
    """Hermes plugin entry point: register exactly one memory provider."""
    provider = MnemosyneRustProvider()
    ctx.register_memory_provider(provider)
    return provider
