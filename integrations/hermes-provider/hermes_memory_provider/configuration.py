"""Read-only profile configuration and store resolution for every surface."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# LOCAL PATCH: P31 never invoke the engine's seeding/config singleton or bank
# manager to discover settings and paths. Invalid profile config fails loudly.


class ProfileConfigError(ValueError):
    """The selected profile cannot be resolved safely."""


def active_home(hermes_home: str | Path | None = None) -> Path:
    if hermes_home:
        return Path(hermes_home).expanduser()
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    return Path(get_hermes_home()).expanduser()


def _mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path.is_symlink():
            raise ProfileConfigError(f"Profile config link is broken: {path}")
        return {}
    except (OSError, UnicodeError) as exc:
        raise ProfileConfigError(f"Cannot read profile config: {path}") from exc
    try:
        import yaml
        value = yaml.safe_load(text)
    except Exception as exc:
        raise ProfileConfigError(f"Cannot parse profile config: {path}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileConfigError(f"Profile config must be a mapping: {path}")
    return value


def read_hermes_config_key(hermes_home: str | Path | None, key: str) -> Any:
    home = active_home(hermes_home)
    config = _mapping(home / "config.yaml")
    memory = config.get("memory", {})
    if memory is None:
        memory = {}
    if not isinstance(memory, dict):
        raise ProfileConfigError(f"memory must be a mapping: {home / 'config.yaml'}")
    provider = memory.get("mnemosyne", {})
    if provider is None:
        provider = {}
    if not isinstance(provider, dict):
        raise ProfileConfigError(f"memory.mnemosyne must be a mapping: {home / 'config.yaml'}")
    return provider.get(key)


def read_profile_key(hermes_home: str | Path | None, key: str) -> Any:
    home = active_home(hermes_home)
    value = read_hermes_config_key(home, key)
    if value is not None:
        return value
    return read_engine_profile_key(home, key)


def read_engine_profile_key(hermes_home: str | Path | None, key: str) -> Any:
    return _mapping(active_home(hermes_home) / "mnemosyne/config.yaml").get(key)


def profile_bank(hermes_home: str | Path | None, agent_identity: str | None = None) -> str:
    home = active_home(hermes_home)
    name = home.name
    if name in (".hermes", "hermes", "default"):
        name = "default"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ProfileConfigError("Profile isolation requires an unambiguous lowercase profile name")
    identity = agent_identity or ""
    if identity not in ("", "primary", "default", "none", name):
        raise ProfileConfigError("Agent identity and profile directory select different memory banks; set db_path explicitly")
    return name


def configured_db_path(hermes_home: str | Path | None = None, *,
                       overrides: dict[str, Any] | None = None) -> str | None:
    """Return the explicit provider/environment path, excluding engine defaults."""
    values = overrides or {}
    explicit = values.get("db_path")
    if not explicit:
        explicit = read_hermes_config_key(hermes_home, "db_path")
    if not explicit:
        explicit = os.environ.get("MNEMOSYNE_DB_PATH")
    if not explicit:
        return None
    if str(explicit) == ":memory:":
        raise ProfileConfigError("A persistent database path is required")
    return str(Path(str(explicit)).expanduser())


def resolve_db_path(hermes_home: str | Path | None = None, *,
                    overrides: dict[str, Any] | None = None,
                    agent_identity: str | None = None) -> str:
    home = active_home(hermes_home)
    values = overrides or {}
    explicit = configured_db_path(home, overrides=values)
    if explicit:
        return explicit
    data_dir = os.environ.get("MNEMOSYNE_DATA_DIR")
    data = Path(data_dir).expanduser() if data_dir else home / "mnemosyne/data"
    isolated = values.get("profile_isolation")
    if isolated is None:
        isolated = read_profile_key(home, "profile_isolation")
    if isinstance(isolated, str):
        isolated = isolated.lower() in ("true", "1", "yes", "on")
    if isolated:
        bank = profile_bank(home, agent_identity)
        if bank != "default":
            return str(data / "banks" / bank / "mnemosyne.db")
    return str(data / "mnemosyne.db")
