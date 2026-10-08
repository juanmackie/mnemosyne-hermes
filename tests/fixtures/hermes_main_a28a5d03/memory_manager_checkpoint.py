"""Extracted checkpoint contract from Hermes main at a28a5d03a9fa60418db5f44f3436fa2aa029c8f2.

Source: https://github.com/NousResearch/hermes-agent/blob/a28a5d03a9fa60418db5f44f3436fa2aa029c8f2/agent/memory_manager.py
Full source SHA256: 1bf9538949edc03f24b1475268e83e0c3fa7d0eaf03701bc296b35d454dc6819
Only the exact checkpoint-version helpers and MemoryManager methods are retained,
so these compatibility tests need no Hermes install or network access.
"""

# ruff: noqa  # Keep the extracted AST faithful to the pinned host source.

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)
PRE_COMPRESS_CHECKPOINT_API_VERSION = 2
_LEGACY_PRE_COMPRESS_API_VERSION = 1


def _signature_params(fn: Callable[..., Any]):
    """Copied from Hermes MemoryManager: return signature parameters when readable."""
    try:
        return inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None


def _has_var_kwargs(params) -> bool:
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _accepts_require_checkpoint(fn: Callable[..., Any]) -> bool:
    """True if ``fn`` can receive the ``require_checkpoint`` keyword (unreadable signatures -> False).

    Bare-shape v2 providers (``on_pre_compress(self, messages)``) would raise TypeError on the
    keyword, which the host would re-raise as a checkpoint failure despite a successful write.
    """
    params = _signature_params(fn)
    if params is None:
        return False
    kind = getattr(params.get("require_checkpoint"), "kind", None)
    return _has_var_kwargs(params) or kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def _redact_for_provider(*values: Any) -> Any:
    """No secrets occur in the fixture; preserve the pinned method call shape."""
    return values[0] if len(values) == 1 else tuple(values)


class MemoryProvider:
    """Only a type placeholder for the upstream method annotation."""


class PinnedMemoryManager:
    """Checkpoint-only extraction of Hermes MemoryManager for contract tests."""

    def __init__(self, providers: List[Any]) -> None:
        self._providers = providers

    @staticmethod
    def _checkpoint_api_version(provider: MemoryProvider) -> Optional[int]:
        """Provider's advertised pre-compress checkpoint API version; None if unparseable."""
        try:
            return int(
                getattr(
                    provider,
                    "pre_compress_checkpoint_api_version",
                    _LEGACY_PRE_COMPRESS_API_VERSION,
                )
            )
        except (TypeError, ValueError):
            return None

    def supports_pre_compress_checkpoint(
        self, api_version: int = PRE_COMPRESS_CHECKPOINT_API_VERSION
    ) -> bool:
        """Return whether an active provider guarantees checkpoint API support."""
        versions = (self._checkpoint_api_version(p) for p in self._providers)
        return any(v is not None and v >= api_version for v in versions)

    def on_pre_compress(
        self,
        messages: List[Dict[str, Any]],
        *,
        evidence_messages: Optional[List[Dict[str, Any]]] = None,
        require_checkpoint: bool = False,
        checkpoint_api_version: int = PRE_COMPRESS_CHECKPOINT_API_VERSION,
    ) -> str:
        """Notify providers before compression; return their combined summary-prompt text.

        ``messages`` is the raw v1 transcript; ``evidence_messages`` is the host-normalized list handed
        only to checkpoint (v2+) providers. With ``require_checkpoint`` at least one checkpoint provider
        must succeed — its exception propagates so the caller keeps the uncompressed transcript.
        """
        parts = []
        checkpoint_succeeded = False
        messages, evidence_messages = _redact_for_provider(messages or [], evidence_messages)
        for provider in self._providers:
            version = self._checkpoint_api_version(provider)
            if version is None:
                version = _LEGACY_PRE_COMPRESS_API_VERSION
            is_checkpoint_provider = version >= checkpoint_api_version
            use_evidence = is_checkpoint_provider and evidence_messages is not None
            provider_messages = evidence_messages if use_evidence else messages
            kwargs: Dict[str, Any] = {}
            if is_checkpoint_provider and _accepts_require_checkpoint(provider.on_pre_compress):
                kwargs["require_checkpoint"] = require_checkpoint
            try:
                result = provider.on_pre_compress(provider_messages, **kwargs)
                if result and result.strip():
                    parts.append(result)
                checkpoint_succeeded = checkpoint_succeeded or is_checkpoint_provider
            except Exception as e:
                logger.debug("Memory provider '%s' on_pre_compress failed: %s", provider.name, e)
                if require_checkpoint and is_checkpoint_provider:
                    raise
        if require_checkpoint and not checkpoint_succeeded:
            raise RuntimeError(
                f"No active memory provider completed pre-compress checkpoint API v{checkpoint_api_version}"
            )
        return "\n\n".join(parts)
