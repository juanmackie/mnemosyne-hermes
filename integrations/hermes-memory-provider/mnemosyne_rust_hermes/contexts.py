"""Execution-context gating.

Automatic capture and automatic injection are *user-originated learning only*.
Any execution context that is not a primary interactive turn must be skipped
both locally (never issue the MCP call) and by passing execution_context
through to the Rust side (defence in depth).

The pinned MemoryProvider.initialize contract documents agent_context values
"primary" | "subagent" | "cron" | "flush". "background" and "skill_loop" are
additional non-interactive contexts handled here as a best-effort superset
(see README "Research note").
"""

from __future__ import annotations

from typing import Optional

#: Execution contexts that must never trigger automatic capture or injection.
SKIP_CONTEXTS = frozenset(
    {
        "cron",
        "flush",
        "subagent",
        "background",
        "skill_loop",
    }
)


def normalize_context(value: Optional[str]) -> str:
    """Lower-case / strip an execution-context label ("" for unknown)."""
    if not value:
        return ""
    return str(value).strip().lower()


def is_skip_context(value: Optional[str]) -> bool:
    """True when automatic capture AND injection must be suppressed."""
    return normalize_context(value) in SKIP_CONTEXTS


def is_primary_context(value: Optional[str]) -> bool:
    """True for an interactive primary turn (empty context means primary)."""
    normalized = normalize_context(value)
    return normalized in ("", "primary", "interactive", "user")
