"""
mnemosyne_hermes — Pure Python Hermes memory provider.

Uses PythonMemoryStorage directly (no binary, no MCP stdio).
Provider ID: mnemosyne (not mnemosyne-rust).
Local-first, keyless by design.
"""

__version__ = "0.1.0"
provider_id = "mnemosyne"

from .provider import MnemosyneMemoryProvider
from .config import default_config, ProviderConfig

__all__ = ["MnemosyneMemoryProvider", "default_config", "ProviderConfig", "__version__", "provider_id"]