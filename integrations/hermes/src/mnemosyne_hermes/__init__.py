"""
mnemosyne_hermes — Pure Python Hermes memory provider.

Uses PythonMemoryStorage directly (no binary, no MCP stdio).
Provider ID: mnemosyne (not mnemosyne-rust).
Local-first, keyless by design.
"""

__version__ = "0.1.0"
provider_id = "mnemosyne"

from .provider import MnemosyneMemoryProvider, CheckpointError, PROVIDER_ID
from .config import default_config, ProviderConfig

__all__ = ["MnemosyneMemoryProvider", "CheckpointError", "PROVIDER_ID", "default_config", "ProviderConfig", "__version__", "provider_id", "register"]


def register(ctx):
    provider = MnemosyneMemoryProvider()
    ctx.register_memory_provider(provider)
    return provider