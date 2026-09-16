"""
mnemosyne_hermes config — ProviderConfig + default_config.

Config key: memory.provider = mnemosyne (not mnemosyne-rust).
Hermes config.yaml compatibility verified.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


class ProviderConfig:
    """Configuration for the mnemosyne pure-Python provider.

    Keys mirror the Rust adapter's ProviderConfig so Hermes config.yaml
    compatibility is preserved when switching providers.
    """

    provider_id: str = "mnemosyne"
    namespace: str = "agent:hermes"
    db_path: str = ""  # resolved from MNEMOSYNE_DB_PATH or hermes_home
    hermes_home: str = os.path.expanduser("~/.hermes")
    storage_dir: str = ""  # defaults to hermes_home/mnemosyne
    binary: str = ""  # unused for pure Python; kept for config compat
    mcp_args: list = field(default_factory=list)  # unused for pure Python
    request_timeout: int = 30
    initialize_timeout: int = 60

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        # Resolve db_path from env or hermes_home
        if not self.db_path:
            self.db_path = os.getenv(
                "MNEMOSYNE_DB_PATH",
                os.path.join(self.hermes_home, "mnemosyne", "mnemosyne.db"),
            )
        if not self.storage_dir:
            self.storage_dir = os.path.join(self.hermes_home, "mnemosyne")


def default_config() -> ProviderConfig:
    """Return the default ProviderConfig for the mnemosyne pure-Python provider."""
    return ProviderConfig()