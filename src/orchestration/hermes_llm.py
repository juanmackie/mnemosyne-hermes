"""LLM configuration inherited from the active Hermes instance.

Hermes exposes its logged-in provider through the local OpenAI-compatible
subscription proxy.  This module keeps every Mnemosyne LLM caller on that
same model and credential path instead of collecting a second API key.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PROXY_URL = "http://127.0.0.1:8645/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"


@dataclass(frozen=True)
class HermesModelConfig:
    """Resolved model settings without exposing Hermes credentials."""

    model: str
    provider: str
    base_url: str
    hermes_home: Path
    inherited: bool
    api_key: Optional[str] = field(default=None, repr=False)


class LLMUnavailable(RuntimeError):
    """Raised when no usable Hermes or legacy LLM configuration exists."""


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().strip('"').strip("'")
    return value or None


def _load_dotenv(path: Path) -> Dict[str, str]:
    """Load Hermes' provider secret without logging or persisting it."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return values


def _load_model_yaml(path: Path) -> Dict[str, Any]:
    """Read the small model section we need, with an optional YAML parser."""
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    if yaml is not None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            model = data.get("model", {})
            if isinstance(model, str):
                return {"default": model}
            return model if isinstance(model, dict) else {}
        except Exception as exc:
            raise LLMUnavailable(f"Could not read Hermes config {path}: {exc}") from exc

    # Hermes normally installs PyYAML.  Keep the dependency optional for the
    # memory-only runtime and parse the documented flat model mapping when it
    # is absent.
    values: Dict[str, Any] = {}
    in_model = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^model\s*:", line):
            remainder = line.split(":", 1)[1].strip()
            in_model = not remainder
            if remainder:
                values["default"] = remainder
            continue
        if in_model and line[:1].isspace() and ":" in line:
            key, value = line.strip().split(":", 1)
            values[key.strip()] = value.strip()
        elif in_model and not line[:1].isspace():
            in_model = False
    return values


def resolve_hermes_model(home: Optional[str] = None) -> Optional[HermesModelConfig]:
    """Resolve the active Hermes model and inherited provider credential."

    ``HERMES_PROXY_BASE_URL`` is an explicit override for a non-default local
    proxy.  A configured Hermes home is enough to opt into inheritance; the
    proxy itself remains responsible for OAuth/API credentials.
    """
    hermes_home = Path(home or os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    config_path = hermes_home / "config.yaml"
    proxy_url = os.environ.get("HERMES_PROXY_BASE_URL", DEFAULT_PROXY_URL).rstrip("/")
    model_override = _clean(os.environ.get("HERMES_MODEL"))

    if not config_path.is_file() and not model_override and "HERMES_PROXY_BASE_URL" not in os.environ:
        return None

    values: Dict[str, Any] = {}
    if config_path.is_file():
        values = _load_model_yaml(config_path)

    provider = (_clean(values.get("provider")) or "nous").lower()
    model = model_override or _clean(values.get("default")) or _clean(values.get("model"))
    secrets = _load_dotenv(hermes_home / ".env")
    explicit_proxy = "HERMES_PROXY_BASE_URL" in os.environ
    proxy_provider = provider in {"nous", "nous-portal", "xai"}
    if explicit_proxy or proxy_provider:
        base_url = proxy_url
        api_key = None
    else:
        base_url = _clean(values.get("base_url"))
        if not base_url and provider == "openrouter":
            base_url = "https://openrouter.ai/api/v1"
        if not base_url:
            base_url = proxy_url
        key_name = _clean(values.get("key_env")) or _clean(values.get("api_key_env"))
        if key_name:
            api_key = secrets.get(key_name) or os.environ.get(key_name)
        elif provider == "openrouter":
            api_key = secrets.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        elif provider == "anthropic":
            api_key = (
                secrets.get("ANTHROPIC_API_KEY")
                or secrets.get("ANTHROPIC_TOKEN")
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_TOKEN")
            )
        else:
            api_key = secrets.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not model:
        model = "default"
    return HermesModelConfig(
        model=model,
        provider=provider,
        base_url=base_url.rstrip("/"),
        hermes_home=hermes_home,
        inherited=True,
        api_key=api_key,
    )


def _legacy_key(explicit_key: Optional[str]) -> Optional[str]:
    """Legacy fallback only when no Hermes instance is configured."""
    return explicit_key or os.environ.get("ANTHROPIC_API_KEY")


class LLMClient:
    """Small OpenAI-compatible client with an Anthropic compatibility fallback."""

    def __init__(self, hermes: Optional[HermesModelConfig], api_key: Optional[str] = None):
        self.hermes = hermes
        self.api_key = None if hermes else _legacy_key(api_key)
        self.model = hermes.model if hermes else os.environ.get("MNEMOSYNE_MODEL", DEFAULT_ANTHROPIC_MODEL)

    @property
    def configured(self) -> bool:
        return self.hermes is not None or bool(self.api_key)

    @property
    def endpoint(self) -> Optional[str]:
        return self.hermes.base_url if self.hermes else None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        if self.hermes and self.hermes.provider != "anthropic":
            payload_messages = ([{"role": "system", "content": system}] if system else []) + messages
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": payload_messages,
                "max_tokens": max_tokens,
            }
            if tools:
                payload["tools"] = tools
            auth = self.hermes.api_key or "hermes-proxy"
            request = urllib.request.Request(
                f"{self.hermes.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {auth}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                hint = "run `hermes proxy start`" if not self.hermes.api_key else "check Hermes provider credentials"
                raise LLMUnavailable(
                    f"Hermes LLM endpoint unavailable at {self.hermes.base_url}; {hint} ({exc})"
                ) from exc

        anthropic_key = self.hermes.api_key if self.hermes else self.api_key
        if not anthropic_key:
            raise LLMUnavailable(
                "No Hermes provider credential or legacy ANTHROPIC_API_KEY is configured. "
                "Run `hermes setup --portal` and `hermes proxy start`."
            )

        import anthropic

        anthropic_messages = _to_anthropic_messages(messages)
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system or "",
            "messages": anthropic_messages,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t["function"]["name"], "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters", {})}
                for t in tools
            ]
        response = anthropic.Anthropic(api_key=anthropic_key).messages.create(**kwargs)
        return _from_anthropic_response(response)


def _to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for message in messages:
        if message.get("role") == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": message.get("content", ""),
                }],
            })
        else:
            result.append(message)
    return result


def _from_anthropic_response(response: Any) -> Dict[str, Any]:
    content = []
    tool_calls = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            content.append(block.text)
        elif getattr(block, "type", None) == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {"name": block.name, "arguments": json.dumps(block.input)},
            })
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": getattr(response.usage, "input_tokens", 0),
            "completion_tokens": getattr(response.usage, "output_tokens", 0),
        },
    }


def get_llm(api_key: Optional[str] = None) -> LLMClient:
    """Return the shared active-model client for an agent or service."""
    return LLMClient(resolve_hermes_model(), api_key=api_key)


def configure_dspy(dspy_module: Any) -> Optional[Any]:
    """Configure DSPy to use Hermes' active model and local proxy."""
    llm = get_llm()
    if not llm.configured:
        return None
    if llm.hermes and llm.hermes.provider == "anthropic":
        lm = dspy_module.Claude(
            model=llm.model,
            api_key=llm.hermes.api_key,
            max_tokens=4096,
        )
    elif llm.hermes:
        lm = dspy_module.LM(
            f"openai/{llm.model}",
            api_key=llm.hermes.api_key or "hermes-proxy",
            api_base=llm.endpoint,
            max_tokens=4096,
        )
    else:
        lm = dspy_module.Claude(
            model=llm.model,
            api_key=llm.api_key,
            max_tokens=4096,
        )
    dspy_module.settings.configure(lm=lm)
    return lm
