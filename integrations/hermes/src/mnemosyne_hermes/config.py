"""Configuration for the local, keyless Hermes provider."""
import os
from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    provider_id: str = "mnemosyne"
    namespace: str = field(default_factory=lambda: os.getenv("MNEMOSYNE_NAMESPACE", "agent:hermes"))
    db_path: str = field(default_factory=lambda: os.getenv("MNEMOSYNE_DB_PATH", ""))
    hermes_home: str = field(default_factory=lambda: os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes")))
    storage_dir: str = ""
    policy_owner: str = "mnemosyne"

    def resolved_db_path(self):
        return os.path.expanduser(self.db_path or os.path.join(self.hermes_home, "mnemosyne", "mnemosyne.db"))

    def resolved_storage_dir(self):
        return os.path.expanduser(self.storage_dir or os.path.dirname(self.resolved_db_path()) or ".")


def default_config():
    return ProviderConfig()
