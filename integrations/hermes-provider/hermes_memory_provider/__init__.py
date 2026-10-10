"""Mnemosyne Memory Provider for Hermes.

Deploy to Hermes via:
    ln -s /path/to/mnemosyne/hermes_memory_provider ~/.hermes/plugins/mnemosyne

Then set in ~/.hermes/config.yaml:
    memory:
      provider: mnemosyne

This gives Mnemosyne first-class MemoryProvider integration (system prompt
injection, pre-turn prefetch, post-turn sync, tool dispatch) while remaining
a standalone plugin deployed through the plugin system.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta


# LOCAL PATCH: P22 registration/cleanup ownership spans provider instances.
_host_llm_registration_lock = threading.Lock()

# LOCAL PATCH (P15): supported Hermes versions do not ship this helper.
def spawn_context_thread(target: Callable[[], Any], *, name: Optional[str] = None,
                         daemon: bool = True) -> threading.Thread:
    """Start a stdlib thread with the caller's contextvars copied.

    Hermes 0.18.2 and 0.19.0 do not expose a ``spawn_context_thread`` helper;
    this local compatibility fallback provides that behavior without assuming
    a newer Hermes API. Each call gets a fresh Context because a Context cannot
    be entered concurrently or recursively.
    """
    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run,
        args=(target,),
        name=name,
        daemon=daemon,
    )
    thread.start()
    return thread


class CheckpointError(RuntimeError):
    """A required pre-compression checkpoint could not be created."""


# Ensure mnemosyne core is importable from this directory
# MUST be before any `from mnemosyne.*` imports

import uuid

# Write-approval gate: when memory.write_approval is enabled, writes are
# staged to pending/memory/<id>.json instead of committed directly.
# apply_pending() replays approved records through the BEAM write path.
def _write_approval_enabled() -> bool:
    """Check if memory.write_approval is enabled in Hermes config."""
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        raw = cfg_get(cfg, "memory", "write_approval", default=False)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"on", "true", "yes", "1", "approve", "enabled"}
        return False
    except Exception:
        return False


def _stage_pending_write(payload: Dict[str, Any]) -> str:
    """Stage a write to the pending store and return the record ID."""
    from hermes_constants import get_hermes_home
    pid = uuid.uuid4().hex[:8]
    pending_dir = get_hermes_home() / "pending" / "memory"
    pending_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": pid,
        "subsystem": "memory",
        "provider": "mnemosyne",
        "tool": payload.get("tool", "mnemosyne_remember"),
        "payload": payload,
        "summary": payload.get("content", "")[:200],
        "created_at": time.time(),
    }
    (pending_dir / f"{pid}.json").write_text(json.dumps(record, indent=2))
    return pid
# LOCAL PATCH (T7): only expose the repo sibling directory on sys.path when it
# actually holds the sibling package. When this provider is COPIED (not
# symlinked) into $HERMES_HOME/plugins/mnemosyne, `.parent.parent` is the
# plugins directory, which contains a `mnemosyne/` entry -- inserting it would
# shadow the engine's `mnemosyne` package and make every engine import fail
# ("No module named 'mnemosyne.core'"). The guard keeps repo checkouts working
# and removes the shadowing trap for copied installs.
_mnemosyne_root = Path(__file__).resolve().parent.parent
if (_mnemosyne_root / "hermes_memory_provider").is_dir() and str(_mnemosyne_root) not in sys.path:
    sys.path.insert(0, str(_mnemosyne_root))

# LOCAL PATCH: the engine is imported TOLERANTLY. Upstream imported it
# unconditionally, so in a venv without mnemosyne-memory this module raised
# ImportError, the Hermes loader returned None, and the provider could not even
# report "unavailable" — it vanished. Now the module always imports and
# is_available() answers with a reason (unavailable_reason()). Only these two
# names are used at import time (the class bases); everything else is behind
# initialize()/is_available(), so the placeholders below are never reached on an
# unavailable provider.
_ENGINE_IMPORT_ERROR: Optional[BaseException] = None
try:
    from mnemosyne.core.episodic_graph import GraphEdge
    from mnemosyne.core.beam import WORKING_MEMORY_TTL_HOURS
    from mnemosyne.batch_tool import (
        BatchValidationError,
        apply_beam_batch,
        batch_validation_error_payload,
        dry_run_batch,
        validate_batch_operations,
    )
    from mnemosyne.integrations.hermes_persona_prompt import HermesPersonaPromptMixin
except ImportError as _engine_import_error:
    _ENGINE_IMPORT_ERROR = _engine_import_error
    GraphEdge = None
    WORKING_MEMORY_TTL_HOURS = 24
    BatchValidationError = None
    apply_beam_batch = None
    batch_validation_error_payload = None
    dry_run_batch = None
    validate_batch_operations = None

    class HermesPersonaPromptMixin:  # fallback: keeps the class body valid
        pass

# LOCAL PATCH: P31 configuration discovery never seeds engine-global config.
from .configuration import (ProfileConfigError, active_home, configured_db_path,
                            read_hermes_config_key, read_engine_profile_key,
                            resolve_db_path, profile_bank)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# C13: provider-active flag for memory-context double-injection prevention.
# ---------------------------------------------------------------------------
# When Hermes loads BOTH the MemoryProvider (canonical surface) AND the
# legacy hermes_plugin (composed by the provider's register() at line ~828,
# or independently discovered when a plugin.yaml is found), TWO pre-turn
# memory-injection paths fire on every LLM call:
#   1. MnemosyneMemoryProvider.prefetch() renders a `## Mnemosyne Context`
#      block.
#   2. hermes_plugin._on_pre_llm_call() renders a `MNEMOSYNE CONTEXT /
#      MNEMOSYNE RECALL` block.
# Both run their own beam.recall() and write to the system prompt, doubling
# the per-turn token cost and confusing the agent with duplicated context.
#
# Fix: when at least one MemoryProvider instance is the active surface (its
# initialize() ran successfully in a non-skip context), the plugin's
# _on_pre_llm_call() defers via the ``_provider_active`` flag below. The
# flag is the boolean view of an instance refcount so:
#   - Multiple provider instances coexisting in one process all keep the
#     flag True until ALL of them shut down (codex review #3 -- a single
#     bool can't represent multi-instance lifecycle).
#   - Skip-context re-init of an already-active instance DEACTIVATES it
#     (codex review #2 -- otherwise a primary->subagent re-init silences
#     the plugin for the subagent session, breaking legacy plugin behavior
#     for skip contexts).
#   - Init FAILURE keeps the flag at whatever it was -- if init fails,
#     this instance never activated, so the plugin path remains available
#     as the legacy fallback (codex review #1 -- without C27 merged here,
#     the provider's system_prompt_block returns "" on init failure;
#     suppressing the plugin too would leave a failed install completely
#     invisible).
_provider_active: bool = False
_active_provider_count: int = 0

# ---------------------------------------------------------------------------
# Lazy imports — fail gracefully if mnemosyne core is missing
# ---------------------------------------------------------------------------

def _get_beam_class():
    from mnemosyne.core.beam import BeamMemory
    return BeamMemory


def _get_triple_module():
    from mnemosyne.core.triples import add_triple, query_triples
    return add_triple, query_triples


def _prefetch_content_char_limit() -> int:
    """Return the per-memory prefetch content limit.

    ``0`` means no truncation. This is the default because the old hardcoded
    200-character cap often removed the actual fact from LLM-authored memories.
    Operators that need tighter prompt budgets can set
    ``MNEMOSYNE_PREFETCH_CONTENT_CHARS`` to a positive integer.
    """
    raw = os.environ.get("MNEMOSYNE_PREFETCH_CONTENT_CHARS", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid MNEMOSYNE_PREFETCH_CONTENT_CHARS=%r; disabling prefetch truncation",
            raw,
        )
        return 0



def _sync_turn_user_limit() -> int:
    """Return the per-turn user content truncation limit.

    ``0`` means no truncation. Defaults to 500 characters for backward
    compatibility. Set ``MNEMOSYNE_SYNC_TURN_USER_LIMIT`` to override.
    """
    raw = os.environ.get("MNEMOSYNE_SYNC_TURN_USER_LIMIT", "500").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid MNEMOSYNE_SYNC_TURN_USER_LIMIT=%r; using default 500",
            raw,
        )
        return 500


def _sync_turn_assistant_limit() -> int:
    """Return the per-turn assistant content truncation limit.

    ``0`` means no truncation. Defaults to 800 characters for backward
    compatibility. Set ``MNEMOSYNE_SYNC_TURN_ASSISTANT_LIMIT`` to override.
    """
    raw = os.environ.get("MNEMOSYNE_SYNC_TURN_ASSISTANT_LIMIT", "800").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid MNEMOSYNE_SYNC_TURN_ASSISTANT_LIMIT=%r; using default 800",
            raw,
        )
        return 800

def _format_prefetch_content(content: str, limit: int) -> str:
    """Format recalled memory content for prompt injection.

    When a positive limit is configured, truncate on a word boundary instead of
    splitting mid-token. Without a positive limit, return the complete content.
    """
    if limit <= 0 or len(content) <= limit:
        return content

    cut = content[:limit].rstrip()
    # Prefer a word boundary when one exists reasonably close to the limit.
    boundary = cut.rfind(" ")
    if boundary >= max(1, limit // 2):
        cut = cut[:boundary].rstrip()
    return f"{cut}..."


# Low-quality fragment filter for prefetch.
#
# The regex fact extractor can emit bare single-token "facts" (a stray adverb, a
# particle, or a truncated word). Such tokens FTS-match common query words and can
# outrank real memories in the per-turn prefetch window. A real, injectable memory
# is a phrase, not a lone token, so drop lone short/stopword tokens. Exact- and
# length-based only, so genuine short multi-word facts are never affected.
_PREFETCH_FRAGMENT_STOPWORDS = frozenset({
    "still", "what", "most", "almost", "back", "now", "too", "right",
    "being", "going", "here", "there", "then", "just", "also", "only",
    "even", "very", "really", "again", "away", "off", "out", "up",
    "down", "over", "that", "this", "it", "so",
})
_PREFETCH_MIN_FRAGMENT_CHARS = 8   # lone tokens shorter than this are dropped
_PREFETCH_OVERFETCH = 16           # recall more, then filter junk and cap
_PREFETCH_TOP_K = 5                # final injected count: compact, relevance-first

# Prompt-usefulness filter for automatic memory-context injection. Manual recall
# tools can stay broad; prefetch is silently injected into every model call, so
# it should be conservative and favor distilled memories over raw transcript.
# LOCAL PATCH: P23 classifies opt-in tool/delegation captures as raw evidence.
_PREFETCH_RAW_PREFIXES = ("[USER]", "[ASSISTANT]", "[IDENTITY]", "[TOOL]", "[DELEGATION]")
_PREFETCH_EXCLUDED_PREFIXES = ("[ASSISTANT]",)
_PREFETCH_RAW_SOURCES = {"conversation", "conversation_tool", "conversation_delegation"}
_PREFETCH_DISTILLED_SOURCES = {
    "preference", "correction", "fact", "identity", "insight", "sleep_consolidation",
}
_PREFETCH_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./:-]*", re.IGNORECASE)
_PREFETCH_DEDUP_STOPWORDS = _PREFETCH_FRAGMENT_STOPWORDS | frozenset({
    "about", "after", "before", "because", "could", "from", "have", "into",
    "like", "more", "need", "needs", "than", "them", "they", "want", "wants",
    "when", "where", "which", "while", "would", "yourself",
})
_PREFETCH_MODEL_SLOT_STOPWORDS = _PREFETCH_DEDUP_STOPWORDS | frozenset({
    "and", "are", "for", "how", "should", "the", "with", "what", "why",
})


# LOCAL PATCH: P23 puts a hard ceiling on the complete block returned to
# Hermes. It includes identity and model context, which used to bypass the
# per-memory limit entirely.
def _prefetch_total_char_budget() -> int:
    raw = os.environ.get("MNEMOSYNE_PREFETCH_TOTAL_CHARS", "8000").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid MNEMOSYNE_PREFETCH_TOTAL_CHARS=%r; using 8000", raw)
        return 8000
    # Keep enough room for useful context and prevent accidental unbounded
    # injection through configuration typos.
    return min(max(value, 256), 65536)


def _budget_prefetch_blocks(blocks: List[str], budget: int) -> Tuple[str, bool]:
    """Keep the highest-priority complete lines within a visible char budget.

    Callers order blocks by priority (identity, model, then relevance-ranked
    sources). A single oversized line is shortened at a word boundary and
    marked; later lower-priority lines are omitted with an explicit marker.
    """
    output: List[str] = []
    used = 0
    truncated = False
    omitted = False
    per_line_cap = min(2000, max(128, budget // 4))
    for block in blocks:
        if not block:
            continue
        for line_index, line in enumerate(block.splitlines()):
            separator = "\n\n" if line_index == 0 and output else ("\n" if output else "")
            candidate = separator + line
            remaining = budget - used
            if len(candidate) <= remaining:
                output.append(candidate)
                used += len(candidate)
                continue
            suffix = " … [truncated]"
            if len(line) > per_line_cap and remaining >= len(separator) + per_line_cap + len(suffix):
                # Keep a long identity or recall item from consuming the entire
                # aggregate budget; the next high-priority rows still get a
                # chance to contribute.
                room = per_line_cap - len(suffix)
                cut = line[:room].rstrip()
                boundary = cut.rfind(" ")
                if boundary >= max(8, room // 2):
                    cut = cut[:boundary].rstrip()
                piece = separator + cut + suffix
                output.append(piece)
                used += len(piece)
                truncated = True
                continue
            truncated = True
            room = remaining - len(separator) - len(suffix)
            if room > 16 and line.strip():
                cut = line[:room].rstrip()
                boundary = cut.rfind(" ")
                if boundary >= max(8, room // 2):
                    cut = cut[:boundary].rstrip()
                output.append(separator + cut + suffix)
                used += len(separator) + len(cut) + len(suffix)
            omitted = True
            break
        if omitted:
            break
    rendered = "".join(output)
    if omitted and "[additional context omitted]" not in rendered:
        # If the remaining space cannot hold a shortened row, make omission
        # visible by reserving the tail of the last complete line for a marker.
        marker = " … [additional context omitted]"
        room = max(0, budget - len(marker))
        cut = rendered[:room].rstrip()
        boundary = cut.rfind(" ")
        if boundary >= max(8, room // 2):
            cut = cut[:boundary].rstrip()
        rendered = cut + marker
    if len(rendered) > budget:  # Defensive guard for separator accounting.
        rendered = rendered[:budget]
        truncated = True
    return rendered, truncated


def _is_low_quality_prefetch(content: str) -> bool:
    """True if recalled content is a bare single-token fragment with no value as
    injected context. Multi-word phrases always pass."""
    c = (content or "").strip()
    if not c:
        return True
    if len(c.split()) <= 1 and (len(c) <= _PREFETCH_MIN_FRAGMENT_CHARS
                                or c.lower() in _PREFETCH_FRAGMENT_STOPWORDS):
        return True
    return False


def _strip_prefetch_prefix(content: str) -> str:
    c = (content or "").strip()
    upper = c.upper()
    for prefix in _PREFETCH_RAW_PREFIXES:
        if upper.startswith(prefix):
            return c[len(prefix):].strip()
    return c


def _prefetch_tokens(content: str) -> Set[str]:
    c = _strip_prefetch_prefix(content).lower()
    tokens: Set[str] = set()
    for token in _PREFETCH_TOKEN_RE.findall(c):
        if len(token) <= 2 or token in _PREFETCH_DEDUP_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _prefetch_model_slot_tokens(content: str) -> Set[str]:
    """Content-word tokens for selected canonical model-slot injection.

    Model-slot injection is more dangerous than dedup tokenization because a
    single overlap can silently inject a durable user/workflow model into the
    prompt. Ignore common function words so unrelated slots do not match on
    tokens like "and" or "the". Expand structured slot labels such as
    ``communication_style`` into useful lexical pieces so normal queries like
    "communication style" can match them.
    """

    tokens: Set[str] = set()
    for token in _prefetch_tokens(content):
        if token in _PREFETCH_MODEL_SLOT_STOPWORDS:
            continue
        tokens.add(token)
        for part in re.split(r"[_:/.-]+", token):
            if len(part) > 2 and part not in _PREFETCH_MODEL_SLOT_STOPWORDS:
                tokens.add(part)
    return tokens


def _prefetch_topic_signal(row: Dict[str, Any]) -> float:
    """Best available non-importance relevance signal for a recall row."""
    signal = max(
        float(row.get("keyword_score") or 0.0),
        float(row.get("fts_score") or 0.0),
        float(row.get("dense_score") or 0.0),
    )
    # Fact/entity matches are explicit relevance signals even when recall() did
    # not fill keyword/FTS scores for that path.
    if row.get("fact_match") or row.get("entity_match"):
        signal = max(signal, 0.20)
    return signal


def _prefetch_source_quality(row: Dict[str, Any]) -> float:
    """Relative usefulness multiplier for injected memory.

    Distilled memories are better prompt context than raw transcript snippets;
    assistant transcript snippets should not be injected at all by default.
    """
    content = (row.get("content") or "").strip()
    upper = content.upper()
    source = str(row.get("source") or "").lower()

    if upper.startswith(_PREFETCH_EXCLUDED_PREFIXES):
        return 0.0

    # Tool output and child-agent transcripts are useful searchable evidence,
    # but are noisy and may contain secrets. Keep them out of silent injection
    # unless an operator explicitly opts in; manual recall remains unchanged.
    # LOCAL PATCH: P23 keep operational transcript captures out of silent
    # prefetch by default; operators can opt in without changing manual recall.
    if source in {"conversation_tool", "conversation_delegation"}:
        if not _parse_env_bool("MNEMOSYNE_PREFETCH_INCLUDE_RAW_TOOL_CAPTURE", False):
            return 0.0

    quality = 1.0
    if source in _PREFETCH_DISTILLED_SOURCES:
        quality *= 1.12
    if source in _PREFETCH_RAW_SOURCES:
        quality *= 0.72
    if upper.startswith("[USER]"):
        quality *= 0.68
    elif upper.startswith("[IDENTITY]"):
        quality *= 0.80
    elif source.startswith("memoria_source"):
        quality *= 0.90
    return quality


# LOCAL PATCH: P23 recognize tool/delegation records and explicit transcript tags.
def _prefetch_is_raw(row: Dict[str, Any]) -> bool:
    content = (row.get("content") or "").strip().upper()
    source = str(row.get("source") or "").lower()
    return (
        source in _PREFETCH_RAW_SOURCES
        or source.startswith("conversation_")
        or content.startswith(("[USER]", "[IDENTITY]", "[TOOL]", "[DELEGATION]"))
    )


def _prefetch_adjusted_score(row: Dict[str, Any]) -> float:
    score = float(row.get("score") or 0.0)
    signal = _prefetch_topic_signal(row)
    importance = min(max(float(row.get("importance") or 0.0), 0.0), 1.0)
    return (score * 0.65 + signal * 0.35 + importance * 0.05) * _prefetch_source_quality(row)


def _semantic_dedup_prefetch(rows: List[Dict[str, Any]], threshold: float = 0.72) -> List[Dict[str, Any]]:
    """Collapse near-duplicate memory rows, keeping the best-ranked variant."""
    kept: List[Dict[str, Any]] = []
    kept_tokens: List[Set[str]] = []
    for row in rows:
        tokens = _prefetch_tokens(row.get("content", ""))
        if not tokens:
            continue
        duplicate = False
        for existing in kept_tokens:
            overlap = len(tokens & existing)
            if not overlap:
                continue
            jaccard = overlap / max(len(tokens | existing), 1)
            containment = overlap / max(min(len(tokens), len(existing)), 1)
            if jaccard >= threshold or containment >= 0.86:
                duplicate = True
                break
        if duplicate:
            continue
        kept.append(row)
        kept_tokens.append(tokens)
    return kept


# ---------------------------------------------------------------------------
# Prefetch profiles
#
# A profile is a named bundle of the prefetch knobs (recall breadth, weights,
# temporal decay, relevance thresholds, source quality filtering + dedup toggles,
# and which registered sources to merge). Operators select a profile via
# MNEMOSYNE_PREFETCH_PROFILE; libraries can register their own with
# register_profile().
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefetchProfile:
    name: str
    top_k: int = _PREFETCH_TOP_K
    importance_weight: Optional[float] = None   # None -> recall() default
    vec_weight: Optional[float] = None
    fts_weight: Optional[float] = None
    temporal_weight: float = 0.2
    temporal_halflife: float = 48
    min_score: float = 0.20
    min_importance: float = 0.65
    min_topic_signal: float = 0.08
    raw_min_topic_signal: float = 0.18
    content_char_limit: int = 0                  # 0 -> use env / untruncated
    drop_low_quality: bool = True
    dedup: bool = True
    semantic_dedup: bool = True
    exclude_assistant: bool = True
    sources: Tuple[str, ...] = ("bank",)         # which registered sources to merge


_BUILTIN_PROFILES: Dict[str, PrefetchProfile] = {
    # Default per-turn injection: compact, relevance-first, and conservative
    # about raw transcript snippets.
    "general": PrefetchProfile(name="general"),
    # Favor recent, high-importance memories; same filter/dedup defaults.
    "social-chat": PrefetchProfile(
        name="social-chat", top_k=6,
        importance_weight=0.6, temporal_weight=0.35, temporal_halflife=24,
    ),
}


def register_profile(profile: "PrefetchProfile") -> None:
    """Register (or override) a named prefetch profile."""
    _BUILTIN_PROFILES[profile.name] = profile


def _resolve_profile(name: Optional[str]) -> PrefetchProfile:
    """Return the named profile, falling back to `general` for unknown/empty."""
    return _BUILTIN_PROFILES.get((name or "general"), _BUILTIN_PROFILES["general"])


def _norm_prefetch_line(line: str) -> str:
    """Normalize a content line for cross-source dedup: lowercase, collapse
    whitespace, drop a leading bracketed metadata prefix (e.g. timestamps)."""
    s = line.strip()
    while s.startswith("[") or s.startswith("("):
        close = s.find("]") if s.startswith("[") else s.find(")")
        if close == -1:
            break
        s = s[close + 1:].strip()
    return " ".join(s.lower().split())


def _dedup_blocks(blocks: List[str]) -> List[str]:
    """Collapse near-duplicate content lines across blocks, preserving each
    block's headers and order. A single block is returned unchanged."""
    seen: Set[str] = set()
    out: List[str] = []
    for block in blocks:
        kept: List[str] = []
        for line in block.split("\n"):
            is_header = line.lstrip().startswith("#") or not line.strip()
            if is_header:
                kept.append(line)
                continue
            norm = _norm_prefetch_line(line)
            if norm and norm in seen:
                continue
            if norm:
                seen.add(norm)
            kept.append(line)
        out.append("\n".join(kept))
    return out


def _coerce_source_output(out: Any, profile: "PrefetchProfile", header: str) -> str:
    """Turn a registered source's return value into an injectable block.

    A source may return a pre-formatted string (used verbatim) or a list of hit
    dicts ({"content", optional "timestamp"/"importance"}). Lists are formatted
    under `header`, low-quality-filtered + capped per the profile."""
    if not out:
        return ""
    if isinstance(out, str):
        return out
    try:
        hits = list(out)
    except TypeError:
        return ""
    lines = [header]
    limit = _prefetch_content_char_limit() or profile.content_char_limit
    for r in hits[: profile.top_k]:
        content = r.get("content", "") if isinstance(r, dict) else str(r)
        if profile.drop_low_quality and _is_low_quality_prefetch(content):
            continue
        content = _format_prefetch_content(content, limit)
        if isinstance(r, dict) and r.get("timestamp"):
            lines.append(f"  [{str(r['timestamp'])[:16]}] {content}")
        else:
            lines.append(f"  {content}")
    return "\n".join(lines) if len(lines) > 1 else ""



# Tool schemas
# ---------------------------------------------------------------------------

REMEMBER_SCHEMA = {
    "name": "mnemosyne_remember",
    "description": (
        "Store a durable memory in Mnemosyne. Use for ANY fact, preference, "
        "identity, insight, or context that should persist across sessions. Higher importance "
        "(0.0-1.0) surfaces the memory more often. Use scope='global' for user-level "
        "facts; scope='session' for conversation-specific context. Use valid_until "
        "(ISO date YYYY-MM-DD) for time-bound facts. Use extract_entities=True to "
        "extract named entities for fuzzy recall (e.g. 'Abdias' and 'Abdias J.' will match). "
        "Use extract=True to also pull subject-predicate-object fact triples via LLM "
        "for fact-aware recall. Use veracity to tag confidence: 'stated' for direct "
        "user assertions, 'tool' for deterministic tool output, 'inferred' for derived "
        "guesses; 'unknown' (default) gets no recall boost."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The memory content to store."},
            "importance": {"type": "number", "description": "Importance 0.0-1.0. Default 0.5.", "default": 0.5},
            "source": {"type": "string", "description": "Source tag: preference, fact, insight, identity, task, etc.", "default": "user"},
            "scope": {"type": "string", "description": "'session' (default) or 'global'.", "default": "session"},
            "valid_until": {"type": "string", "description": "Optional expiry date YYYY-MM-DD.", "default": ""},
            "extract_entities": {"type": "boolean", "description": "Extract named entities for fuzzy recall. Default False.", "default": False},
            "extract": {"type": "boolean", "description": "Extract subject-predicate-object fact triples via LLM for fact-aware recall. Default False.", "default": False},
            "metadata": {"type": "object", "description": "Optional dict of additional fields (source_doc, tags, page, etc.). Default empty.", "default": {}},
            "veracity": {"type": "string", "description": "Confidence label: 'stated' | 'inferred' | 'tool' | 'imported' | 'unknown'. Default 'unknown'.", "default": "unknown"},
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "mnemosyne_recall",
    "description": (
        "Search Mnemosyne for relevant memories. Uses hybrid ranking: by default "
        "50% vector similarity + 30% FTS5 text rank + 20% importance + optional "
        "temporal boost. Tune the per-query weights via vec_weight, fts_weight, "
        "importance_weight (omit to use environment defaults). Returns ranked results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language query."},
            "limit": {"type": "integer", "description": "Max results. Default 5.", "default": 5},
            "temporal_weight": {
                "type": "number",
                "description": "How much to boost recent memories (0.0 = ignore time, 0.2 = mild recency bias, 0.5 = strong recency bias). Default 0.0.",
                "default": 0.0,
            },
            "query_time": {
                "type": "string",
                "description": "ISO timestamp to treat as 'now' for temporal scoring. Default is current time.",
                "default": "",
            },
            "temporal_halflife": {
                "type": "number",
                "description": "Hours until temporal boost decays by half. Default 24. Lower = faster decay.",
                "default": 24,
            },
            "vec_weight": {
                "type": "number",
                "description": "Vector similarity weight in hybrid scoring. Omit (or pass null) to use MNEMOSYNE_VEC_WEIGHT env var or built-in default 0.5.",
            },
            "fts_weight": {
                "type": "number",
                "description": "Full-text search weight in hybrid scoring. Omit (or pass null) to use MNEMOSYNE_FTS_WEIGHT env var or built-in default 0.3.",
            },
            "importance_weight": {
                "type": "number",
                "description": "Importance score weight in hybrid scoring. Omit (or pass null) to use MNEMOSYNE_IMPORTANCE_WEIGHT env var or built-in default 0.2.",
            },
            "explain": {
                "type": "boolean",
                "description": "If true, return a structured per-query recall explain trace. Default false.",
                "default": False,
            },
        },
        "required": ["query"],
    },
}

SHARED_REMEMBER_SCHEMA = {
    "name": "mnemosyne_shared_remember",
    "description": (
        "Store compact cross-agent surface memory in a dedicated shared Mnemosyne DB. "
        "Use only for stable user/system/workflow metadata or general preferences. "
        "Normal mnemosyne_remember writes stay private."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Surface memory content to store."},
            "kind": {"type": "string", "description": "meta | preference | correction | identity", "default": "meta"},
            "importance": {"type": "number", "description": "Importance 0.0-1.0. Default 0.8.", "default": 0.8},
            "veracity": {"type": "string", "description": "stated | inferred | tool | imported | unknown", "default": "unknown"},
            "metadata": {"type": "object", "description": "Optional metadata object.", "default": {}},
        },
        "required": ["content"],
    },
}

SHARED_RECALL_SCHEMA = {
    "name": "mnemosyne_shared_recall",
    "description": "Search only the shared Mnemosyne surface DB.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
}

SHARED_FORGET_SCHEMA = {
    "name": "mnemosyne_shared_forget",
    "description": "Delete one working shared-surface memory by exact ID.",
    "parameters": {
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
}

SHARED_STATS_SCHEMA = {
    "name": "mnemosyne_shared_stats",
    "description": "Return shared surface DB path and counts.",
    "parameters": {"type": "object", "properties": {}},
}

SLEEP_SCHEMA = {
    "name": "mnemosyne_sleep",
    "description": (
        "Run the Mnemosyne consolidation cycle. Compresses old working memories "
        "into episodic summaries. Call after long sessions or when memory feels stale. "
        "Set all_sessions=true to consolidate eligible old working memories across inactive sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "all_sessions": {
                "type": "boolean",
                "description": "If true, consolidate eligible old working memories across all sessions instead of only the current session.",
                "default": False,
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, report what would be consolidated without writing changes.",
                "default": False,
            },
            "force": {
                "type": "boolean",
                "description": "If true, skip the age threshold and consolidate all non-consolidated working memories immediately.",
                "default": False,
            },
        },
    },
}

STATS_SCHEMA = {
    "name": "mnemosyne_stats",
    "description": "Return Mnemosyne memory statistics: working count, episodic count, BEAM tiers.",
    "parameters": {
        "type": "object",
        "properties": {}
    }
}

INVALIDATE_SCHEMA = {
    "name": "mnemosyne_invalidate",
    "description": (
        "Mark a memory as expired or superseded. Provide memory_id from recall results. "
        "Optionally provide replacement_id to chain old to new."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of memory to invalidate."},
            "replacement_id": {"type": "string", "description": "Optional new memory that replaces this one.", "default": ""},
        },
        "required": ["memory_id"],
    },
}

VALIDATE_SCHEMA = {
    "name": "mnemosyne_validate",
    "description": (
        "Attest, update, or invalidate a memory the caller did not necessarily author. "
        "Supports collaborative ownership: any agent can validate any memory in either "
        "the private bank or the shared surface. The original author is preserved; "
        "validator + validated_at are updated to record the most recent attester. "
        "A 3-entry ring buffer keeps lightweight history. "
        "Actions: 'attest' (confirm correctness), 'update' (replace content), "
        "'invalidate' (mark superseded), 'delete' (remove)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of memory to validate."},
            "action": {
                "type": "string",
                "enum": ["attest", "update", "invalidate", "delete"],
                "description": "What kind of validation to record.",
            },
            "validator": {
                "type": "string",
                "description": "Agent identifier performing the validation. Defaults to the caller's agent_identity if not set.",
                "default": "",
            },
            "new_content": {
                "type": "string",
                "description": "New content (only used with action='update').",
                "default": "",
            },
            "note": {
                "type": "string",
                "description": "Optional reason or evidence for this validation.",
                "default": "",
            },
            "bank": {
                "type": "string",
                "enum": ["private", "surface"],
                "description": "Which bank holds the memory. Default 'private'.",
                "default": "private",
            },
        },
        "required": ["memory_id", "action"],
    },
}

GET_SCHEMA = {
    "name": "mnemosyne_get",
    "description": (
        "Retrieve a single memory by its primary key. Pure read, no side effects. "
        "No semantic search. Returns the exact memory with the given ID or None. "
        "Use this when you already know the memory ID from a previous recall response."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "The memory ID to retrieve."},
        },
        "required": ["memory_id"],
    },
}

TRIPLE_ADD_SCHEMA = {
    "name": "mnemosyne_triple_add",
    "description": (
        "Add a temporal fact triple (subject, predicate, object) to the knowledge graph. "
        "Example: ('user', 'prefers', 'neovim'). Use for structured relationships. "
        "By default a new triple supersedes any prior fact with the same subject+predicate; "
        "set supersede=false for multi-valued facts that should coexist "
        "(e.g. ('user','speaks','English') and ('user','speaks','Spanish'))."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string"},
            "valid_from": {"type": "string", "description": "ISO date YYYY-MM-DD", "default": ""},
            "valid_until": {"type": "string", "description": "Optional ISO expiry date YYYY-MM-DD.", "default": ""},
            "source": {"type": "string", "description": "Provenance label.", "default": ""},
            "confidence": {"type": "number", "description": "0.0-1.0 (default 1.0).", "default": 1.0},
            "supersede": {"type": "boolean", "description": "If false, do not close prior same subject+predicate triples (multi-valued).", "default": True},
        },
        "required": ["subject", "predicate", "object"],
    },
}

TRIPLE_END_SCHEMA = {
    "name": "mnemosyne_triple_end",
    "description": (
        "Expire a fact in the knowledge graph WITHOUT replacing it (e.g. a relationship "
        "that simply ended). Closes all open triples for subject+predicate, or only the one "
        "matching object when given. Use mnemosyne_triple_add instead when a new value replaces the old."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "predicate": {"type": "string"},
            "object": {"type": "string", "description": "Optional: end only this exact triple; omit to end all open subject+predicate triples.", "default": ""},
            "valid_until": {"type": "string", "description": "ISO date YYYY-MM-DD the fact ended (default: today).", "default": ""},
        },
        "required": ["subject", "predicate"],
    },
}

TRIPLE_QUERY_SCHEMA = {
    "name": "mnemosyne_triple_query",
    "description": (
        "Query the temporal knowledge graph for facts matching subject/predicate/object patterns. "
        "Subject match is case-insensitive. Pass as_of to query facts valid on a past date."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "default": ""},
            "predicate": {"type": "string", "default": ""},
            "object": {"type": "string", "default": ""},
            "as_of": {"type": "string", "description": "ISO date YYYY-MM-DD; query facts valid as of this date (default: today).", "default": ""},
        },
    },
}

REMEMBER_CANONICAL_SCHEMA = {
    "name": "mnemosyne_remember_canonical",
    "description": (
        "Store a CANONICAL (single-source-of-truth) self-fact for the current "
        "profile. Each (category, name) slot holds exactly one current value: "
        "restating the same body is a no-op, and a new body supersedes the old "
        "one (kept as history). Use for stable identity cards — name, voice, "
        "stable preferences, relationships — that must not contradict themselves "
        "over time. Scoped privately to this profile. For relational facts use "
        "mnemosyne_triple_add; for episodic recall use mnemosyne_remember."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Slot group, e.g. 'identity', 'voice', 'preference'"},
            "name": {"type": "string", "description": "Slot key within the category, e.g. 'name', 'pronouns'"},
            "body": {"type": "string", "description": "The authoritative free-text value for this slot"},
            "source": {"type": "string", "description": "Optional provenance label", "default": ""},
            "confidence": {"type": "number", "description": "Optional 0..1 confidence", "default": 1.0},
        },
        "required": ["category", "name", "body"],
    },
}

RECALL_CANONICAL_SCHEMA = {
    "name": "mnemosyne_recall_canonical",
    "description": (
        "Read CANONICAL self-facts for the current profile. With category+name: "
        "return the single authoritative value for that slot. With category "
        "only: list that category's slots. With query: substring-search the "
        "profile's canonical values. With nothing: list all canonical slots. "
        "Set include_history=true to also return superseded versions of a slot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "default": ""},
            "name": {"type": "string", "default": ""},
            "query": {"type": "string", "description": "Substring search across the profile's canonical values", "default": ""},
            "include_history": {"type": "boolean", "description": "Include superseded versions (requires category+name)", "default": False},
            "limit": {"type": "integer", "description": "Max results for query/list modes", "default": 10},
        },
    },
}

FORGET_CANONICAL_SCHEMA = {
    "name": "mnemosyne_forget_canonical",
    "description": (
        "Retire a CANONICAL self-fact slot for the current profile. "
        "Stamps valid_until on the current row, preserving it as history. "
        "Returns whether a current row was retired. Nothing is deleted. "
        "Use this to remove a canonical fact (e.g. a stale preference or identity)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Slot group, e.g. 'identity', 'voice', 'preference'"},
            "name": {"type": "string", "description": "Slot key within the category, e.g. 'name', 'pronouns'"},
        },
        "required": ["category", "name"],
    },
}

APPLY_PENDING_SCHEMA = {
    "name": "mnemosyne_apply_pending",
    "description": (
        "Commit staged pending memory writes to Mnemosyne. "
        "When memory.write_approval is enabled, calls to mnemosyne_remember "
        "and mnemosyne_batch are staged to pending/memory/ instead of "
        "written directly. This tool replays approved pending records "
        "through the BEAM write path, committing them to the database."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pending_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of pending record IDs to commit (e.g., ['a1b2c3d4']). From the staged response.",
            },
        },
        "required": ["pending_ids"],
    },
}

MODEL_CARD_SCHEMA = {
    "name": "mnemosyne_model_card",
    "description": (
        "Render current canonical slots as a compact deterministic model card. "
        "Use this for Hindsight-style user, workflow, project, or agent mental-model "
        "summaries when the facts already live in canonical storage. This does not "
        "call an LLM or create a new memory; it is a view over current canonical facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Canonical category to render, e.g. 'model:user' or 'identity'"},
            "title": {"type": "string", "description": "Optional display title", "default": ""},
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional ordered subset of slot names to include",
                "default": [],
            },
        },
        "required": ["category"],
    },
}

MODEL_REFRESH_SCHEMA = {
    "name": "mnemosyne_model_refresh",
    "description": (
        "Inspect sleep-time LLM-inferred canonical model update outcomes. "
        "Normal behavior is automated during sleep: validated candidates are "
        "auto-applied or auto-rejected by policy. This tool is diagnostic only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list"], "default": "list"},
            "status": {"type": "string", "description": "pending, applied, rejected, or all", "default": "all"},
            "limit": {"type": "integer", "description": "Max proposals to list", "default": 20},
        },
    },
}

SCRATCHPAD_WRITE_SCHEMA = {
    "name": "mnemosyne_scratchpad_write",
    "description": "Write a temporary note to the Mnemosyne scratchpad.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["content"],
    },
}

SCRATCHPAD_READ_SCHEMA = {
    "name": "mnemosyne_scratchpad_read",
    "description": "Read the Mnemosyne scratchpad entries.",
    "parameters": {"type": "object", "properties": {}},
}

SCRATCHPAD_CLEAR_SCHEMA = {
    "name": "mnemosyne_scratchpad_clear",
    "description": "Clear all entries from the Mnemosyne scratchpad.",
    "parameters": {"type": "object", "properties": {}},
}

EXPORT_SCHEMA = {
    "name": "mnemosyne_export",
    "description": "Export all Mnemosyne memories to a JSON file for backup or migration.",
    "parameters": {
        "type": "object",
        "properties": {
            "output_path": {
                "type": "string",
                "description": "File path to write the export JSON (e.g., /tmp/mnemosyne_backup.json)",
            },
        },
        "required": ["output_path"],
    },
}

UPDATE_SCHEMA = {
    "name": "mnemosyne_update",
    "description": "Update the content or importance of an existing memory by ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the memory to update"},
            "content": {"type": "string", "description": "New content for the memory (optional)"},
            "importance": {"type": "number", "description": "New importance from 0.0 to 1.0 (optional)"},
        },
        "required": ["memory_id"],
    },
}

FORGET_SCHEMA = {
    "name": "mnemosyne_forget",
    "description": "Permanently delete a memory by ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the memory to delete"},
        },
        "required": ["memory_id"],
    },
}

BATCH_SCHEMA = {
    "name": "mnemosyne_batch",
    "description": (
        "Apply multiple Mnemosyne memory mutations atomically in one tool call. "
        "Supported v1 actions: remember, update, forget, invalidate. "
        "All operations are validated before mutation; on failure the whole batch rolls back. "
        "Destructive actions require exact memory IDs. Recall/search/canonical/persona/shared-surface operations are not included in v1."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "maxItems": 50,
                "description": "Ordered mutation operations to apply atomically.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["remember", "update", "forget", "invalidate"]},
                        "content": {"type": "string"},
                        "memory_id": {"type": "string"},
                        "importance": {"type": "number"},
                        "source": {"type": "string"},
                        "scope": {"type": "string"},
                        "valid_until": {"type": "string"},
                        "metadata": {"type": "object"},
                        "extract_entities": {"type": "boolean"},
                        "extract": {"type": "boolean"},
                        "veracity": {"type": "string"},
                        "replacement_id": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
            "dry_run": {"type": "boolean", "default": False},
            "bank": {"type": "string"},
            "author_id": {"type": "string"},
            "author_type": {"type": "string"},
            "channel_id": {"type": "string"},
        },
        "required": ["operations"],
    },
}

IMPORT_SCHEMA = {
    "name": "mnemosyne_import",
    "description": "Import Mnemosyne memories from a JSON file or another memory provider (Hindsight, Mem0). Idempotent by default.",
    "parameters": {
        "type": "object",
        "properties": {
            "input_path": {
                "type": "string",
                "description": "File path to read the export JSON from (for file imports)",
            },
            "provider": {
                "type": "string",
                "description": "Provider to import from: 'hindsight', 'mem0'. Requires api_key.",
            },
            "api_key": {
                "type": "string",
                "description": "API key for the source provider (can also be set via env var)",
            },
            "user_id": {
                "type": "string",
                "description": "Filter imported memories by user ID (provider-specific)",
            },
            "agent_id": {
                "type": "string",
                "description": "Filter imported memories by agent ID (provider-specific)",
            },
            "base_url": {
                "type": "string",
                "description": "Base URL for self-hosted provider instances",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, validate and transform but don't write any memories",
                "default": False,
            },
            "channel_id": {
                "type": "string",
                "description": "Channel to assign imported memories to",
            },
            "force": {
                "type": "boolean",
                "description": "If true, overwrite existing records instead of skipping",
                "default": False,
            },
        },
    },
}

DIAGNOSE_SCHEMA = {
    "name": "mnemosyne_diagnose",
    "description": "Run PII-safe diagnostics on Mnemosyne installation. Checks dependencies, database state, vector search readiness, and optional vec_working migration coverage. Never includes memory content or API keys.",
    "parameters": {
        "type": "object",
        "properties": {
            "repair_vec_working": {
                "type": "boolean",
                "description": "If true, idempotently backfill missing vec_working rows from memory_embeddings.",
                "default": False,
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true with repair_vec_working, report what would be repaired without writing.",
                "default": False,
            },
        },
    },
}

# These schemas intentionally expose operational surfaces rather than new
# memory-writing behavior: diagnostics lets operators observe recall health,
# while task_progress stores a curated current-state pointer in canonical facts.
# Keeping both as explicit tools prevents silent prompt injection or background
# transcript autosave from becoming the source of truth for task continuity.
RECALL_DIAGNOSTICS_SCHEMA = {
    "name": "mnemosyne_recall_diagnostics",
    "description": (
        "Return recall path diagnostics: per-tier hit counts, fallback rates, "
        "and total call counts. Use to monitor recall health — high fallback "
        "rates indicate weak-signal recall paths dominating. Pass reset=true "
        "to clear counters and start a fresh measurement window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reset": {
                "type": "boolean",
                "description": "If true, reset all counters after snapshotting. Default false.",
                "default": False,
            },
        },
    },
}

TASK_PROGRESS_SCHEMA = {
    "name": "mnemosyne_task_progress",
    "description": (
        "Track and recall cross-session task progression. Uses canonical "
        "memory slots with category 'task:progress' to store where you left "
        "off on a specific task. Set a task's current state with "
        "action='set', query the latest state with action='get', list all "
        "tracked tasks with action='list'. This solves the 'where did we "
        "leave off?' problem across sessions — session_search finds old "
        "transcripts, but this gives you the curated current state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "set | get | list | clear",
                "default": "get",
            },
            "task": {
                "type": "string",
                "description": "Task identifier (e.g. 'pdas-q08', 'mnemo-impl', 'qudomec-deploy'). Required for set/get/clear.",
                "default": "",
            },
            "state": {
                "type": "string",
                "description": "Current task state description. Required for set.",
                "default": "",
            },
            "metadata": {
                "type": "object",
                "description": "Optional metadata (status, next_step, blockers, etc.).",
                "default": {},
            },
        },
        "required": ["action"],
    },
}

GRAPH_QUERY_SCHEMA = {
    "name": "mnemosyne_graph_query",
    "description": "Traverse the memory graph to find memories related to a seed memory. Uses multi-hop BFS through graph_edges with optional edge_type and min_weight filtering.",
    "parameters": {
        "type": "object",
        "properties": {
            "seed_memory_id": {
                "type": "string",
                "description": "Memory ID to start traversal from",
            },
            "max_hops": {
                "type": "integer",
                "description": "Maximum traversal depth (default: 2)",
                "default": 2,
            },
            "edge_type": {
                "type": "string",
                "description": "Filter by edge type (empty = all types, e.g. 'ctx', 'rel', 'syn', 'references', 'caused', 'supersedes')",
                "default": "",
            },
            "min_weight": {
                "type": "number",
                "description": "Minimum edge weight threshold (0.0 to 1.0, default: 0.0 = no filter)",
                "default": 0.0,
            },
        },
        "required": ["seed_memory_id"],
    },
}

GRAPH_LINK_SCHEMA = {
    "name": "mnemosyne_graph_link",
    "description": "Declare a semantic edge between two memories in the graph. Use this to explicitly link related memories so graph traversal finds them.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "Source memory ID",
            },
            "target_id": {
                "type": "string",
                "description": "Target memory ID",
            },
            "relationship": {
                "type": "string",
                "description": "Relationship label (e.g. 'references', 'caused', 'supersedes', 'related_to')",
            },
            "weight": {
                "type": "number",
                "description": "Edge weight from 0.0 to 1.0 (default: 0.5)",
                "default": 0.5,
            },
        },
        "required": ["source_id", "target_id", "relationship"],
    },
}

try:
    from hermes_memory_provider.sync_adapter import ALL_SYNC_TOOL_SCHEMAS
except Exception:  # pragma: no cover - sync extras are optional at import time
    ALL_SYNC_TOOL_SCHEMAS = []

try:
    from hermes_memory_provider.persona_tools import (
        PERSONA_PROMOTE_SCHEMA,
        PERSONA_DEMOTE_SCHEMA,
        PERSONA_LIST_SCHEMA,
        PERSONA_REINFORCE_SCHEMA,
    )
    ALL_PERSONA_TOOL_SCHEMAS = [
        PERSONA_PROMOTE_SCHEMA,
        PERSONA_DEMOTE_SCHEMA,
        PERSONA_LIST_SCHEMA,
        PERSONA_REINFORCE_SCHEMA,
    ]
except Exception:  # pragma: no cover - persona extras are optional at import time
    ALL_PERSONA_TOOL_SCHEMAS = []

ALL_TOOL_SCHEMAS = [
    REMEMBER_SCHEMA, RECALL_SCHEMA, SHARED_REMEMBER_SCHEMA, SHARED_RECALL_SCHEMA,
    SHARED_FORGET_SCHEMA, SHARED_STATS_SCHEMA, SLEEP_SCHEMA, STATS_SCHEMA,
    INVALIDATE_SCHEMA, VALIDATE_SCHEMA, GET_SCHEMA, TRIPLE_ADD_SCHEMA, TRIPLE_QUERY_SCHEMA,
    TRIPLE_END_SCHEMA,
    REMEMBER_CANONICAL_SCHEMA, RECALL_CANONICAL_SCHEMA, FORGET_CANONICAL_SCHEMA, APPLY_PENDING_SCHEMA, MODEL_CARD_SCHEMA,
    MODEL_REFRESH_SCHEMA, SCRATCHPAD_WRITE_SCHEMA, SCRATCHPAD_READ_SCHEMA, SCRATCHPAD_CLEAR_SCHEMA,
    EXPORT_SCHEMA, UPDATE_SCHEMA, FORGET_SCHEMA, BATCH_SCHEMA, IMPORT_SCHEMA, DIAGNOSE_SCHEMA,
    RECALL_DIAGNOSTICS_SCHEMA, TASK_PROGRESS_SCHEMA,
    GRAPH_QUERY_SCHEMA, GRAPH_LINK_SCHEMA,
    *ALL_SYNC_TOOL_SCHEMAS,
    *ALL_PERSONA_TOOL_SCHEMAS,
]

# LOCAL PATCH (P18): keep Hermes' prompt/tool surface small unless operators opt in.
DEFAULT_TOOL_NAMES = (
    "mnemosyne_remember",
    "mnemosyne_recall",
    "mnemosyne_stats",
    "mnemosyne_forget",
)


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

try:
    from agent import memory_provider as _hermes_memory_provider_module
    MemoryProvider = _hermes_memory_provider_module.MemoryProvider
    # LOCAL PATCH: P24 advertise checkpoint v2 only to Hermes releases that
    # define the v2 host contract. Older managers keep the legacy best-effort
    # hook and never receive the new strict-mode kwargs.
    _HERMES_CHECKPOINT_API_VERSION = int(
        getattr(_hermes_memory_provider_module, "PRE_COMPRESS_CHECKPOINT_API_VERSION", 1)
    )
    _HERMES_RECALL_STATUS = getattr(_hermes_memory_provider_module, "RecallStatus", None)
except ImportError:
    # Graceful fallback if ABC not available (shouldn't happen in practice)
    MemoryProvider = object  # type: ignore
    _HERMES_CHECKPOINT_API_VERSION = 1
    _HERMES_RECALL_STATUS = None


def _parse_env_float(key: str, default: float) -> float:
    """Read a float env var, falling back to default on missing or invalid value."""
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    """Coerce config/env values to bool while preserving a safe default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _parse_env_bool(key: str, default: bool) -> bool:
    """Read a boolean env var, falling back to default on missing/invalid values."""
    return _coerce_bool(os.environ.get(key), default)


def _coerce_optional_int(value: Any, default: Optional[int]) -> Optional[int]:
    """Coerce config/env values to a non-negative int; negative means unlimited."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else None


def _parse_env_optional_int(key: str, default: Optional[int]) -> Optional[int]:
    """Read a non-negative int env var; negative values disable the cap."""
    return _coerce_optional_int(os.environ.get(key), default)


class MnemosyneMemoryProvider(HermesPersonaPromptMixin, MemoryProvider):
    """Mnemosyne native memory — local SQLite with vector + FTS5 hybrid search."""

    # LOCAL PATCH: P24 API-v2 is advertised only when the installed Hermes
    # provider contract exposes it. The v2 hook durably stores all normalized
    # evidence before it returns successfully.
    pre_compress_checkpoint_api_version = _HERMES_CHECKPOINT_API_VERSION

    # How long on_session_end will wait for sleep/consolidation to finish before
    # giving up and letting the daemon thread continue in the background. Tests
    # may shorten this to keep the suite fast. Override via MNEMOSYNE_SESSION_END_TIMEOUT.
    SESSION_END_SLEEP_TIMEOUT_SECONDS = _parse_env_float("MNEMOSYNE_SESSION_END_TIMEOUT", 15)

    # LOCAL PATCH: P22 kept for configuration compatibility. Auto-sleep starts a tracked
    # background worker without joining it on the turn-sync path (P22).
    _AUTO_SLEEP_TIMEOUT_SECONDS = _parse_env_float("MNEMOSYNE_AUTO_SLEEP_TIMEOUT", 5)

    _SYNC_TURN_SLOW_THRESHOLD_SECONDS = _parse_env_float("MNEMOSYNE_SYNC_TURN_SLOW_THRESHOLD", 5)

    # LOCAL PATCH (P16): delegation capture is explicit opt-in.
    _VALID_SYNC_ROLES: frozenset = frozenset({"user", "assistant", "tool", "delegation"})
    DELEGATION_MAX_CHARS = 4096
    COMPRESS_CHECKPOINT_MAX_MESSAGES = 100
    COMPRESS_CHECKPOINT_MAX_BYTES = 262144
    COMPRESS_CONTEXT_MAX_MESSAGES = 6
    COMPRESS_CONTEXT_MAX_CHARS = 8000

    def __init__(self):
        # Keep the wrapper alive whenever the provider adopts its BeamMemory.
        # Mnemosyne owns the thread-local connections, so allowing this local
        # wrapper to be garbage-collected would close the provider's beam.
        self._memory: Optional[Any] = None
        self._beam: Optional[Any] = None
        self._surface_beam: Optional[Any] = None
        self._shared_surface_bank = "surface"
        self._shared_surface_path: Optional[Path] = None
        # When true, mnemosyne_recall merges shared-surface results into the
        # private bank's recall response. Each result is tagged with `bank`
        # ("private" or "surface") so callers can distinguish provenance.
        # Default false preserves existing behavior for deployments that have
        # not opted in.
        self._shared_surface_read = False
        self._audit: Optional[Any] = None
        # C27: capture init exception so downstream methods can surface it
        # instead of silently no-op'ing. `_beam is None AND _init_error is None`
        # means a deliberate skip (subagent/cron/skill_loop context, or pre-init);
        # `_beam is None AND _init_error is not None` means a real failure that
        # users and operators need to see.
        self._init_error: Optional[BaseException] = None
        self._session_id = "hermes_default"
        self._current_session_id = "hermes_default"
        self._hermes_home = ""
        self._platform = "cli"
        self._agent_context = "primary"
        self._turn_count = 0
        # LOCAL PATCH: P24 this is per-turn provenance only. In group chats the
        # startup user_id/agent identity does not identify the current speaker.
        self._current_turn_author: Dict[str, Any] = {}
        self._sync_turn_lock = threading.Lock()
        # Serialize callers sharing the provider's Beam/SQLite handle (#498).
        # LOCAL PATCH: P22 consolidation owns a separate worker connection;
        # its slow reasoning must not hold the foreground handle's lock.
        # LOCAL PATCH: P19 also guards tool and prompt entry points. Reentrant
        # because the diagnostic tool handler takes this lock internally.
        self._beam_access_lock = threading.RLock()
        self._sync_turn_telemetry: Dict[str, Any] = {
            "pending_queue_length": 0,
            "max_queue_length": 0,
            "completed": 0,
            "failed": 0,
            # Reserved for a future bounded async queue; v1 keeps sync_turn
            # inline but exposes stable diagnostic keys.
            "merged": 0,
            "dropped": 0,
            "slow_sync_count": 0,
            "last_duration_ms": None,
            "max_duration_ms": 0.0,
            "last_error": None,
            "in_flight": 0,
        }
        self._auto_sleep_threshold = 50
        self._auto_sleep_enabled = _parse_env_bool("MNEMOSYNE_AUTO_SLEEP_ENABLED", True)
        # Reflection/sleep guardrails.  "reflection" maps to Mnemosyne's
        # sleep/consolidation path in the Hermes provider.  Cron skipping is
        # default-on per issue #337; max_calls_per_session defaults to 3 and
        # can be disabled with a negative value.
        self._reflect_disabled_for_cron = _parse_env_bool("MNEMOSYNE_REFLECT_DISABLED_FOR_CRON", True)
        self._reflect_max_calls_per_session = _parse_env_optional_int("MNEMOSYNE_REFLECT_MAX_CALLS_PER_SESSION", 3)
        self._reflect_calls_this_session = 0
        self._reflect_budget_lock = threading.Lock()
        self._ignore_patterns: List[str] = []  # Regex patterns to filter from memory
        self._sync_roles: Set[str] = {"user"}
        _sync_env = os.environ.get("MNEMOSYNE_SYNC_ROLES")
        if _sync_env is not None:
            _parsed_roles = {r.strip().lower() for r in _sync_env.split(",") if r.strip()}
            self._sync_roles = _parsed_roles & self._VALID_SYNC_ROLES
        self._skip_contexts = {"cron", "flush", "subagent", "background", "skill_loop"}  # Agent contexts to skip
        # Allow override via MNEMOSYNE_SKIP_CONTEXTS env var.
        # Set to empty string to skip nothing (enable all contexts).
        # Set to comma-separated names to customize which contexts skip.
        _skip_env = os.environ.get("MNEMOSYNE_SKIP_CONTEXTS")
        if _skip_env is not None:
            _parsed = {c.strip() for c in _skip_env.split(",") if c.strip()}
            self._skip_contexts = _parsed if _parsed else set()
        # Prefetch profile selection (env; default "general" = prior behavior).
        self._prefetch_profile = (
            os.environ.get("MNEMOSYNE_PREFETCH_PROFILE", "general").strip() or "general"
        )
        # Generic extra-source registry: name -> fn(query, *, session_id) -> hits|str.
        # A profile opts a source in via its `sources`. "bank" is built in.
        self._prefetch_sources: Dict[str, Callable[..., Any]] = {}
        # LOCAL PATCH: P23 keeps only a tiny exact-query warm cache. Hermes
        # already serializes provider callbacks on its background lane.
        self._prefetch_cache: OrderedDict = OrderedDict()
        self._prefetch_cache_lock = threading.Lock()
        self._prefetch_stats: Dict[str, Any] = {
            "cache_hits": 0, "cache_misses": 0, "queued": 0,
            "calls": 0, "last_chars": 0, "max_chars": 0,
            "truncations": 0, "last_duration_ms": 0.0,
            "last_lock_wait_ms": 0.0, "last_recall_ms": 0.0,
            "errors": 0, "last_error": None,
        }
        self._prefetch_errors: Dict[str, int] = {}
        self._prefetch_last_context = ""
        self._prefetch_last_bank_counts: Dict[str, int] = {}
        self._prefetch_recall_changes = 0
        self._prefetch_conn_identity = None
        # Profile memory isolation: when enabled, each Hermes profile gets its own
        # Mnemosyne bank (separate SQLite DB). Default OFF for backward compatibility.
        self._profile_isolation_enabled = False
        # Default scope for remember() calls when not explicitly specified.
        # "session" (default) scopes to current session; "global" persists across sessions.
        self._default_scope = "session"
        # T3: explicit SQLite path. Precedence (resolved in
        # _apply_provider_config): kwargs > memory.mnemosyne.db_path >
        # MNEMOSYNE_DB_PATH env > engine default (MNEMOSYNE_DATA_DIR >
        # $HERMES_HOME > ~/.hermes). None means "let the engine decide".
        self._db_path: Optional[str] = None
        self._configuration_overrides: Dict[str, Any] = {}
        # LOCAL PATCH: P22 one tracked worker for auto/session-end consolidation.
        self._consolidation_lock = threading.Lock()
        self._consolidation_thread: Optional[threading.Thread] = None
        self._consolidation_running = False
        self._consolidation_stopping = False
        self._consolidation_cleanup_pending = False
        # LOCAL PATCH: P29 bounded, process-local consolidation observability.
        self._consolidation_status: Dict[str, Any] = {
            "state": "idle", "last_trigger": None, "started_at": None,
            "finished_at": None, "duration_ms": None, "error_class": None,
            "reused_triggers": 0, "skipped_triggers": 0,
            "shutdown_timed_out": False, "running": False, "stopping": False,
        }
        self._consolidation_started_monotonic: Optional[float] = None
        self._host_llm_backend: Optional[Any] = None
        # C13: per-instance tracking of whether THIS provider contributed
        # to the module-level _active_provider_count. Lets each instance
        # increment exactly once on activate and decrement exactly once on
        # deactivate, even across re-init cycles, without producing a
        # negative count when shutdown is called on a never-activated
        # instance.
        self._is_active_in_module: bool = False

    def _activate_in_module(self) -> None:
        """Bump the module-level active-provider count exactly once per
        instance lifecycle. Called when this instance transitions into
        the active state (non-skip-context initialize completed)."""
        global _active_provider_count, _provider_active
        if not self._is_active_in_module:
            self._is_active_in_module = True
            _active_provider_count += 1
            _provider_active = True

    def _deactivate_in_module(self) -> None:
        """Drop this instance from the module-level active-provider
        count. Idempotent -- a never-activated instance is a no-op.
        ``_provider_active`` stays True as long as ANY other instance is
        still active (multi-instance refcount semantics)."""
        global _active_provider_count, _provider_active
        if self._is_active_in_module:
            self._is_active_in_module = False
            _active_provider_count = max(0, _active_provider_count - 1)
            _provider_active = (_active_provider_count > 0)

    def _init_audit_log(self) -> None:
        """Initialize audit log co-located with the active provider DB."""
        # LOCAL PATCH: P26 retire any prior target before resolving this one;
        # a failed retarget must never keep the old database writable.
        self._close_audit_log(reset=True)
        try:
            from hermes_memory_provider.audit import AuditLog
            db_path = getattr(self._beam, "db_path", None)
            if db_path:
                self._audit = AuditLog(Path(db_path))
                logger.debug("Audit log initialized: %s", db_path)
        except Exception as exc:
            logger.debug("Audit log init skipped: %s", exc)

    # LOCAL PATCH: P26 audit handles are short-lived internally, but their
    # lifecycle still follows the provider's active database target.
    def _close_audit_log(self, *, reset: bool) -> None:
        audit = getattr(self, "_audit", None)
        if audit is not None:
            try:
                audit.close()
            except Exception as exc:
                logger.debug("Audit log close skipped: %s", type(exc).__name__)
        if reset:
            self._audit = None

    def _audit_event(self, action: str, **kwargs) -> None:
        """Record an audit event. Never raises, never blocks."""
        if self._audit is None:
            return
        kwargs.setdefault("profile", getattr(self, "_agent_identity", None) or "")
        kwargs.setdefault("session_id", self._session_id)
        try:
            self._audit.record(action, **kwargs)
        except Exception:
            pass

    # LOCAL PATCH: P28 caller-controlled metadata cannot promote a source_ref
    # to host origin. Exact host refs are created only by sync_turn's helper.
    @staticmethod
    def _asserted_caller_metadata(metadata: Any) -> Any:
        from hermes_memory_provider.evidence import asserted_metadata
        return asserted_metadata(metadata)

    # LOCAL PATCH: P26 expose a bounded audit health snapshot for operators
    # without adding a tool to the provider's default four-tool surface.
    def get_audit_diagnostics(self) -> Dict[str, Any]:
        audit = getattr(self, "_audit", None)
        if audit is None:
            return {
                "state": "unavailable", "readonly": False,
                "counters": {"write_attempts": 0, "write_successes": 0,
                             "write_failures": 0, "read_attempts": 0,
                             "read_failures": 0},
                "errors": {"init": "AuditUnavailableError", "write": None, "read": None},
            }
        diagnostics = getattr(audit, "diagnostics", None)
        if callable(diagnostics):
            try:
                result = diagnostics()
                return dict(result) if isinstance(result, dict) else {
                    "state": "degraded", "readonly": False, "counters": {},
                    "errors": {"init": None, "write": None, "read": "InvalidDiagnostics"},
                }
            except Exception:
                pass
        # LOCAL PATCH: P26 diagnostics must fail visibly when the audit API is
        # unavailable; reporting a healthy zero-failure state would be false.
        return {
            "state": "degraded", "readonly": False, "counters": {},
            "errors": {"init": None, "write": None, "read": "DiagnosticsUnavailable"},
        }

    # LOCAL PATCH: P26 capture stored owner metadata for an audit event. Callers
    # append it only after the engine confirms an authorized mutation.
    def _audit_memory_owner(self, memory_id: str) -> Optional[Dict[str, Any]]:
        beam = getattr(self, "_beam", None)
        conn = getattr(beam, "conn", None)
        if conn is None:
            return None
        try:
            for table, store in (("working_memory", "working"), ("episodic_memory", "episodic")):
                row = conn.execute(
                    f"SELECT session_id, scope FROM {table} WHERE id = ? LIMIT 1",
                    (memory_id,),
                ).fetchone()
                if row is not None:
                    if row[1] not in ("session", "global"):
                        return None
                    scope = str(row[1])
                    owner: Dict[str, Any] = {
                        "bank": "global" if scope == "global" else "private",
                        "scope": scope,
                        "source_tool": "mnemosyne_update",
                        "metadata": {"memory_store": store},
                    }
                    if row[0] is not None:
                        owner["session_id"] = str(row[0])
                    return owner
        except Exception:
            # Auditing is best effort; an unknown owner must never be guessed.
            return None
        return None

    def _init_error_reason(self) -> str:
        """Return a human-readable failure reason for tool responses.

        Truncates the exception message to 200 chars so a verbose SQLite
        error (or similar) can't bloat downstream tool-call payloads.
        Collapses whitespace (including embedded newlines) into single
        spaces so the message can't break the system-prompt structure or
        look like multi-line instructions to the LLM -- defense in depth
        against an exception whose ``str()`` includes user-controllable
        text (e.g. a filesystem path supplied via MNEMOSYNE_DATA_DIR).
        Returns a generic string when init was never attempted (e.g. a
        subagent-context session that legitimately skipped initialize()).
        """
        if self._init_error is None:
            return "Mnemosyne not initialized"
        msg = str(self._init_error)
        # Collapse all whitespace (\n, \r, \t, runs of spaces) into a
        # single space. Codex finding #3: a multi-line exception text or
        # one containing tab-separated instruction-like content could
        # otherwise reach the LLM as structured input.
        import re
        msg = re.sub(r"\s+", " ", msg).strip()
        if len(msg) > 200:
            msg = msg[:200] + "..."
        return f"{type(self._init_error).__name__}: {msg}"

    @property
    def name(self) -> str:
        return "mnemosyne"

    def is_available(self) -> bool:
        """Check if Mnemosyne core is importable. No network calls.

        LOCAL PATCH: also records WHY it is unavailable, via
        unavailable_reason(). Upstream returned a bare False, which made a
        missing engine indistinguishable from "provider not configured".
        """
        if _ENGINE_IMPORT_ERROR is not None:
            self._unavailable_reason = (
                f"mnemosyne-memory engine not importable: {_ENGINE_IMPORT_ERROR}. "
                "Install the pinned engine into this venv, e.g. "
                "uv pip install 'mnemosyne-memory[embeddings]>=3.15.1,<3.16'"
            )
            return False
        try:
            _get_beam_class()
            self._unavailable_reason = ""
            return True
        except Exception as e:
            self._unavailable_reason = f"{type(e).__name__}: {e}"
            return False

    def unavailable_reason(self) -> str:
        """Why is_available() is False. Empty string when available.

        LOCAL PATCH: consumed by `hermes mnemosyne doctor`, the installer check
        and tests/test_provider_loader.py.
        """
        if _ENGINE_IMPORT_ERROR is not None:
            return (
                f"mnemosyne-memory engine not importable: {_ENGINE_IMPORT_ERROR}. "
                "Install the pinned engine into this venv, e.g. "
                "uv pip install 'mnemosyne-memory[embeddings]>=3.15.1,<3.16'"
            )
        return getattr(self, "_unavailable_reason", "")

    def _apply_provider_config(self, kwargs: Dict[str, Any]) -> None:
        """Apply provider-specific config from Hermes kwargs or config.yaml.

        Precedence: kwargs > config.yaml > env var > hardcoded defaults.
        """
        # auto_sleep: prefer kwargs, then config.yaml, then env var, defaulting
        # on to match Mnemosyne core's consolidation behavior for fresh installs.
        auto_sleep = kwargs.get("auto_sleep")
        if auto_sleep is None:
            auto_sleep = self._read_config_key("auto_sleep")
        if auto_sleep is not None:
            self._auto_sleep_enabled = _coerce_bool(auto_sleep, self._auto_sleep_enabled)
        # env var/default is already applied in __init__, so it is the base default

        # sleep_threshold: prefer kwargs, then config.yaml, then default 50
        sleep_threshold = kwargs.get("sleep_threshold")
        if sleep_threshold is None:
            sleep_threshold = self._read_config_key("sleep_threshold")
        if sleep_threshold is not None:
            try:
                self._auto_sleep_threshold = int(sleep_threshold)
            except (TypeError, ValueError):
                logger.warning("Mnemosyne: invalid sleep_threshold=%r, keeping %d",
                               sleep_threshold, self._auto_sleep_threshold)

        # reflect guardrails: prefer kwargs, then memory.mnemosyne.reflect,
        # then flat memory.mnemosyne keys, then env/defaults set in __init__.
        reflect_cfg = kwargs.get("reflect")
        if reflect_cfg is None:
            reflect_cfg = self._read_config_key("reflect")
        if not isinstance(reflect_cfg, dict):
            reflect_cfg = {}

        disabled_for_cron = kwargs.get("disabled_for_cron", kwargs.get("reflect_disabled_for_cron"))
        if disabled_for_cron is None:
            disabled_for_cron = reflect_cfg.get("disabled_for_cron")
        if disabled_for_cron is None:
            disabled_for_cron = self._read_config_key("reflect_disabled_for_cron")
        if disabled_for_cron is not None:
            self._reflect_disabled_for_cron = _coerce_bool(disabled_for_cron, self._reflect_disabled_for_cron)

        max_calls = kwargs.get("max_calls_per_session", kwargs.get("reflect_max_calls_per_session"))
        if max_calls is None:
            max_calls = reflect_cfg.get("max_calls_per_session")
        if max_calls is None:
            max_calls = self._read_config_key("reflect_max_calls_per_session")
        if max_calls is not None:
            self._reflect_max_calls_per_session = _coerce_optional_int(max_calls, self._reflect_max_calls_per_session)

        # vector_type: pass through to BeamMemory if supported, log if not yet wired
        vector_type = kwargs.get("vector_type") or self._read_config_key("vector_type")
        if vector_type and vector_type not in ("float32", "int8", "bit"):
            logger.warning("Mnemosyne: unknown vector_type=%r, ignoring", vector_type)

        # ignore_patterns: list of regex patterns to filter from memory storage
        patterns = kwargs.get("ignore_patterns") or self._read_config_key("ignore_patterns")
        if patterns:
            if isinstance(patterns, str):
                patterns = [p.strip() for p in patterns.replace(",", "\n").split("\n") if p.strip()]
            elif isinstance(patterns, list):
                patterns = [str(p).strip() for p in patterns if str(p).strip()]
            self._ignore_patterns = patterns

        # profile_isolation: separate DB per Hermes profile (bank-based).
        # Default OFF. When enabled, each profile derives its own Mnemosyne bank.
        profile_isolation = kwargs.get("profile_isolation")
        if profile_isolation is None:
            profile_isolation = self._read_config_key("profile_isolation")
        if profile_isolation is not None:
            if isinstance(profile_isolation, str):
                self._profile_isolation_enabled = profile_isolation.lower() in ("true", "1", "yes", "on")
            else:
                self._profile_isolation_enabled = bool(profile_isolation)

        shared_surface_path = kwargs.get("shared_surface_path")
        if shared_surface_path is None:
            shared_surface_path = self._read_config_key("shared_surface_path")
        if shared_surface_path:
            self._shared_surface_path = Path(str(shared_surface_path)).expanduser()

        # skip_contexts: kwargs > config.yaml > env var (already set in __init__)
        _skip_raw = kwargs.get("skip_contexts")
        if _skip_raw is None:
            _skip_raw = self._read_config_key("skip_contexts")
        if _skip_raw is not None:
            if isinstance(_skip_raw, str):
                _parsed = {c.strip() for c in _skip_raw.split(",") if c.strip()}
                self._skip_contexts = _parsed if _parsed else set()
            elif isinstance(_skip_raw, (list, tuple, set)):
                self._skip_contexts = set(str(s).strip() for s in _skip_raw if str(s).strip())

        # sync_roles: which conversation roles to autosave in sync_turn().
        # Default ["user"] avoids assistant transcript noise in automatic memory.
        # Set to ["user"] to save only user turns, [] to disable autosave.
        _sync_raw = kwargs.get("sync_roles")
        if _sync_raw is None:
            _sync_raw = self._read_config_key("sync_roles")
        if _sync_raw is not None:
            if isinstance(_sync_raw, str):
                _parsed_roles = {r.strip().lower() for r in _sync_raw.split(",") if r.strip()}
            elif isinstance(_sync_raw, (list, tuple, set)):
                _parsed_roles = {str(r).strip().lower() for r in _sync_raw if str(r).strip()}
            else:
                logger.warning("Mnemosyne: invalid sync_roles type %s (%r); keeping %s",
                               type(_sync_raw).__name__, _sync_raw, sorted(self._sync_roles))
                _sync_raw = None
            if _sync_raw is not None:
                _unknown = _parsed_roles - self._VALID_SYNC_ROLES
                if _unknown:
                    logger.warning("Mnemosyne: unknown sync_roles ignored: %s", sorted(_unknown))
                _valid = _parsed_roles & self._VALID_SYNC_ROLES
                if _parsed_roles and not _valid:
                    logger.warning("Mnemosyne: no valid sync_roles in %r; keeping %s",
                                   _parsed_roles, sorted(self._sync_roles))
                else:
                    self._sync_roles = _valid

        shared_surface_read = kwargs.get("shared_surface_read")
        if shared_surface_read is None:
            shared_surface_read = self._read_config_key("shared_surface_read")
        if shared_surface_read is not None:
            if isinstance(shared_surface_read, str):
                self._shared_surface_read = shared_surface_read.lower() in ("true", "1", "yes", "on")
            else:
                self._shared_surface_read = bool(shared_surface_read)

        # default_scope: overrides the scope argument for remember() calls when
        # scope is not explicitly set by the caller. "session" (default) limits
        # memories to the current session; "global" persists across sessions.
        default_scope = kwargs.get("default_scope")
        if default_scope is None:
            default_scope = self._read_config_key("default_scope")
        if default_scope is not None:
            scope_str = str(default_scope).lower().strip()
            if scope_str in ("session", "global"):
                self._default_scope = scope_str
            else:
                logger.warning("Mnemosyne: invalid default_scope=%r, must be 'session' or 'global'", default_scope)

        # db_path (T3): explicit SQLite path for this provider. Precedence:
        # kwargs > memory.mnemosyne.db_path > MNEMOSYNE_DB_PATH > engine default
        # (MNEMOSYNE_DATA_DIR > $HERMES_HOME > ~/.hermes). Without this the
        # advertised MNEMOSYNE_DB_PATH contract was ignored: the provider always
        # used the engine default, so a set env var silently pointed nowhere.
        # Only the Hermes config surface is consulted here (not the engine's own
        # config singleton) so the chain above is the whole story.
        # LOCAL PATCH: P31 all surfaces share profile-aware path resolution.
        self._db_path = resolve_db_path(self._hermes_home, overrides=kwargs,
                                        agent_identity=self._agent_identity)
        explicit_db_path = configured_db_path(self._hermes_home, overrides=kwargs)
        if explicit_db_path and self._profile_isolation_enabled:
            logger.warning(
                "Mnemosyne: both db_path=%s and profile_isolation are set; "
                "db_path wins and profile banks are ignored for this provider.",
                explicit_db_path,
            )

    def _reset_profile_settings(self) -> None:
        # LOCAL PATCH: P31 reinitialization restores defaults before a new
        # profile applies its values. Locks, source registrations and worker
        # ownership stay attached to this provider instance.
        self._auto_sleep_enabled = _parse_env_bool("MNEMOSYNE_AUTO_SLEEP_ENABLED", True)
        self._auto_sleep_threshold = 50
        self._reflect_disabled_for_cron = _parse_env_bool("MNEMOSYNE_REFLECT_DISABLED_FOR_CRON", True)
        self._reflect_max_calls_per_session = _parse_env_optional_int("MNEMOSYNE_REFLECT_MAX_CALLS_PER_SESSION", 3)
        self._reflect_calls_this_session = 0
        self._ignore_patterns = []
        self._profile_isolation_enabled = False
        self._shared_surface_path = None
        self._shared_surface_read = False
        self._default_scope = "session"
        self._db_path = None
        self._sync_roles = {"user"}
        sync_env = os.environ.get("MNEMOSYNE_SYNC_ROLES")
        if sync_env is not None:
            self._sync_roles = {r.strip().lower() for r in sync_env.split(",") if r.strip()} & self._VALID_SYNC_ROLES
        self._skip_contexts = {"cron", "flush", "subagent", "background", "skill_loop"}
        skip_env = os.environ.get("MNEMOSYNE_SKIP_CONTEXTS")
        if skip_env is not None:
            self._skip_contexts = {c.strip() for c in skip_env.split(",") if c.strip()}
        self._turn_count = 0
        self._current_turn_author = {}
        self._prefetch_cache.clear()
        self._prefetch_last_context = ""
        self._prefetch_last_bank_counts = {}
        self._prefetch_recall_changes = 0
        self._prefetch_conn_identity = None


    def _should_filter(self, content: str) -> bool:
        """Check if content matches any ignore pattern. Returns True if it should be skipped."""
        if not self._ignore_patterns:
            return False
        import re
        for pattern in self._ignore_patterns:
            try:
                if re.search(pattern, content, re.IGNORECASE):
                    return True
            except re.error:
                logger.debug("Mnemosyne: invalid ignore pattern %r, skipping", pattern)
        return False

    def _read_config_key(self, key: str) -> Any:
        """Read the selected profile's settings without creating config files."""
        # LOCAL PATCH: P31 the engine singleton belongs to the process, not
        # this profile. Its fallback could silently read another store's config.
        home = getattr(self, "_hermes_home", None)
        value = read_hermes_config_key(home, key)
        if value is not None:
            return value
        return read_engine_profile_key(home, key)

    def _configured_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return the curated default or the explicitly configured tool set.

        Omitted/null uses the small core surface. ``tools: ["*"]`` opts into
        every tool; ``tools: []`` disables tool exposure without disabling the
        provider's memory context/prefetch surface. Unknown names and mixed
        wildcard lists fail loudly rather than silently changing the surface.
        """
        configured = self._read_config_key("tools")
        if configured is None:
            configured = list(DEFAULT_TOOL_NAMES)
        if isinstance(configured, str):
            configured = [name.strip() for name in configured.replace(",", "\n").split("\n") if name.strip()]
        if not isinstance(configured, list) or not all(isinstance(name, str) for name in configured):
            raise ValueError("memory.mnemosyne.tools must be a list of tool names")
        if "*" in configured:
            if configured != ["*"]:
                raise ValueError("'*' must be the only entry in memory.mnemosyne.tools")
            return list(ALL_TOOL_SCHEMAS)

        available = {schema["name"]: schema for schema in ALL_TOOL_SCHEMAS}
        unknown = [name for name in configured if name not in available]
        if unknown:
            known = ", ".join(sorted(available))
            bad = ", ".join(str(name) for name in unknown)
            raise ValueError(f"Unknown Mnemosyne tool(s) in memory.mnemosyne.tools: {bad}. Known tools: {known}")
        return [available[name] for name in dict.fromkeys(configured)]

    def _configured_tool_names(self) -> Set[str]:
        return {schema["name"] for schema in self._configured_tool_schemas()}

    def has_tool(self, tool_name: str) -> bool:
        """Return whether a tool is currently exposed by this provider."""
        return tool_name in self._configured_tool_names()

    def _reflection_skip_response(self, reason: str, trigger: str) -> Dict[str, Any]:
        """Structured skip payload for reflection/sleep guardrails."""
        return {
            "status": "skipped",
            "reason": reason,
            "trigger": trigger,
            "reflect": {
                "calls_used": self._reflect_calls_this_session,
                "max_calls_per_session": self._reflect_max_calls_per_session,
                "disabled_for_cron": self._reflect_disabled_for_cron,
                "agent_context": self._agent_context,
            },
        }

    def _reserve_reflection_budget(self, trigger: str) -> Optional[Dict[str, Any]]:
        """Return a structured skip payload, or reserve one reflection call."""
        context = (self._agent_context or "").strip().lower()
        with self._reflect_budget_lock:
            if self._reflect_disabled_for_cron and context == "cron":
                return self._reflection_skip_response("reflect_disabled_for_cron", trigger)
            max_calls = self._reflect_max_calls_per_session
            if max_calls is not None and self._reflect_calls_this_session >= max_calls:
                return self._reflection_skip_response("reflect_budget_exhausted", trigger)
            self._reflect_calls_this_session += 1
        return None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        # LOCAL PATCH: P24 keep Hermes' first-run setup focused on the four
        # choices that change data placement or user-visible memory behavior.
        # Advanced options remain accepted by _apply_provider_config and are
        # documented in the provider config reference, without becoming setup
        # wizard prompts.
        return [
            {"key": "db_path", "description": "Explicit SQLite DB path for this provider. Precedence: memory.mnemosyne.db_path > MNEMOSYNE_DB_PATH env > engine default (MNEMOSYNE_DATA_DIR > $HERMES_HOME > ~/.hermes). Set only when the DB must live outside $HERMES_HOME; the provider warns and doctor flags that case.", "default": None},
            {"key": "profile_isolation", "description": "Enable per-profile memory isolation via Mnemosyne banks. Each Hermes profile gets its own SQLite database under mnemosyne/data/banks/<profile>/. Default false for backward compatibility.", "default": False},
            {"key": "default_scope", "description": "Default scope for remember() calls when not explicitly specified. 'session' (default) limits memories to the current session. 'global' persists memories across sessions.", "choices": ["session", "global"], "default": "session"},
            {"key": "tools", "description": "List of Mnemosyne tool names exposed to Hermes. Omit or set null for the curated core set (remember, recall, stats, forget). Set ['*'] to explicitly expose all tools, or [] to expose none while keeping memory context/prefetch enabled. Unknown names raise a clear startup/config error.", "default": list(DEFAULT_TOOL_NAMES)},
        ]

    # LOCAL PATCH: P24 discover a configured external SQLite store before
    # Hermes initializes the provider. Hermes itself filters paths to the
    # operating-system home, so arbitrary external volumes remain ineligible.
    def backup_paths(self) -> List[str]:
        try:
            from hermes_memory_provider.cli import resolve_effective_db_path
        except ImportError:
            try:
                from .cli import resolve_effective_db_path
            except ImportError:
                return []
        hermes_home = str(active_home(getattr(self, "_hermes_home", None)))
        try:
            db_path = str(self._beam.db_path) if self._beam is not None else resolve_effective_db_path(hermes_home)
            if not db_path or str(db_path) == ":memory:":
                return []
            user_home = Path.home().resolve()
            resolved_hermes_home = Path(hermes_home).expanduser().resolve()

            def eligible(raw_path: Any) -> Optional[str]:
                if not raw_path or str(raw_path) == ":memory:":
                    return None
                resolved = Path(str(raw_path)).expanduser().resolve()
                try:
                    resolved.relative_to(user_home)
                except ValueError:
                    return None
                try:
                    resolved.relative_to(resolved_hermes_home)
                except ValueError:
                    return str(resolved)
                return None

            paths: List[str] = []
            external_db = eligible(db_path)
            if external_db:
                paths.append(external_db)
            # The optional shared surface is a second provider-owned SQLite
            # store. Only an explicit configured path can be resolved without
            # initialization; its package-default path is outside user home.
            try:
                shared_path = self._shared_surface_path or self._read_config_key("shared_surface_path")
            except ProfileConfigError:
                raise
            except Exception:
                shared_path = None
            external_shared = eligible(shared_path)
            if external_shared and external_shared not in paths:
                paths.append(external_shared)
            return paths
        except ProfileConfigError:
            raise
        except (OSError, RuntimeError, ValueError):
            return []

    # LOCAL PATCH: P24 expose read-only configuration that changes provider
    # identity so Hermes gateway caches are invalidated without opening a DB.
    def identity_signature(self) -> Dict[str, Any]:
        home = str(active_home(getattr(self, "_hermes_home", None)))
        configured: Dict[str, Any] = {}
        for key in ("profile_isolation", "default_scope", "tools", "shared_surface_path", "shared_surface_read"):
            try:
                value = self._configuration_overrides.get(key)
                if value is None:
                    value = self._read_config_key(key)
            except ProfileConfigError:
                raise
            except Exception:
                value = None
            if value is not None:
                configured[key] = value
        try:
            from hermes_memory_provider.cli import resolve_effective_db_path
        except ImportError:
            try:
                from .cli import resolve_effective_db_path
            except ImportError:
                resolve_effective_db_path = None
        try:
            if self._configuration_overrides:
                db_path = resolve_effective_db_path(home, overrides=self._configuration_overrides,
                                                   agent_identity=getattr(self, "_agent_identity", None))
            else:
                db_path = resolve_effective_db_path(home)
        except ProfileConfigError:
            raise
        except Exception:
            db_path = None
        if db_path:
            configured["db_path"] = str(Path(str(db_path)).expanduser())
        configured.setdefault("profile_isolation", False)
        configured.setdefault("default_scope", "session")
        configured.setdefault("tools", list(DEFAULT_TOOL_NAMES))
        try:
            # Normalize unusual YAML objects into stable JSON-compatible values.
            configured = json.loads(json.dumps(configured, sort_keys=True, default=str))
        except (TypeError, ValueError):
            configured = {"db_path": str(db_path or "")}
        return {"mnemosyne": configured}

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist provider-specific config values."""
        try:
            import yaml, os
            config_path = os.path.join(hermes_home, "config.yaml") if hermes_home else ""
            if not config_path or not os.path.exists(config_path):
                return
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}
            memory_cfg = config.setdefault("memory", {}).setdefault("mnemosyne", {})
            memory_cfg.setdefault("auto_sleep", _parse_env_bool("MNEMOSYNE_AUTO_SLEEP_ENABLED", True))
            memory_cfg.update(values)
            with open(config_path, "w") as f:
                yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
        except Exception:
            logger.debug("Mnemosyne: could not persist config values", exc_info=True)

    import re
    _BANK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

    @staticmethod
    def _sanitize_bank_name(raw: str) -> str:
        """Sanitize a raw string into a valid bank name.

        Bank names become directory names. Rules:
        - Only [a-z0-9_-], max 64 chars
        - Must start with alphanumeric
        - Reject .. and / for path traversal safety
        - Fallback to 'default' if raw is empty or un-sanitizable
        """
        if not raw:
            return "default"
        # Lowercase and replace spaces/separators with underscore
        sanitized = raw.lower().strip()
        # Replace any disallowed characters with underscore
        sanitized = "".join(
            c if c.isalnum() or c in "_-" else "_"
            for c in sanitized
        )
        # Collapse consecutive underscores
        while "__" in sanitized:
            sanitized = sanitized.replace("__", "_")
        # Strip leading/trailing underscores/hyphens
        sanitized = sanitized.strip("_-")
        # Ensure starts with alphanumeric
        if not sanitized or not sanitized[0].isalnum():
            sanitized = "b_" + sanitized if sanitized else "default"
        # Truncate to 64 chars
        if len(sanitized) > 64:
            sanitized = sanitized[:64].rstrip("_-")
        # Reject path traversal
        if ".." in sanitized or "/" in sanitized:
            return "default"
        return sanitized or "default"

    def _resolve_profile_bank(self) -> str:
        # LOCAL PATCH: P31 lossy name sanitization can collapse distinct profiles.
        return profile_bank(self._hermes_home, getattr(self, "_agent_identity", None))

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize Mnemosyne beam for this session."""
        # LOCAL PATCH: P22 do not retarget a still-running worker's host backend.
        with self._ensure_consolidation_lock():
            if self._consolidation_running:
                raise RuntimeError("Mnemosyne consolidation must finish before reinitializing")
            self._consolidation_stopping = True
            # LOCAL PATCH: P29 each completed/retried provider initialization
            # gets a fresh in-memory status lifetime.
            self._consolidation_status = {
                "state": "idle", "last_trigger": None, "started_at": None,
                "finished_at": None, "duration_ms": None, "error_class": None,
                "reused_triggers": 0, "skipped_triggers": 0,
                "shutdown_timed_out": False, "running": False, "stopping": True,
            }
            self._consolidation_started_monotonic = None
            try:
                self._initialize_session(session_id, **kwargs)
            finally:
                self._consolidation_stopping = False
                self._consolidation_status = {**self._consolidation_status, "stopping": False}

    def _initialize_session(self, session_id: str, **kwargs) -> None:
        # C27: clear stale state from any prior init attempt so a re-init
        # returns the provider to a clean slate. _beam reset is critical
        # for the primary->skip-context re-init case (codex review finding
        # #1): without it, a previously-initialized primary session that
        # later re-initialized into a subagent context would leave the old
        # _beam active, causing system_prompt_block() to report "Active"
        # and handle_tool_call() to silently write into the wrong session.
        # _init_error reset complements this for the failure-recovery case.
        if self._memory is not None:
            try:
                self._memory.close()
            except Exception:
                logger.debug("Mnemosyne: could not close prior wrapper", exc_info=True)
        # LOCAL PATCH: P26 an unsuccessful or skipped reinitialize cannot
        # leave the prior database's audit writer attached to this provider.
        self._close_audit_log(reset=True)
        self._memory = None
        self._beam = None
        self._surface_beam = None
        self._init_error = None

        self._agent_context = kwargs.get("agent_context", "primary")
        self._platform = kwargs.get("platform", "cli")
        self._current_session_id = str(session_id or "hermes_default")
        stable_scope = kwargs.get("gateway_session_key") or session_id or "hermes_default"
        self._session_id = f"hermes_{stable_scope}"
        self._hermes_home = str(active_home(kwargs.get("hermes_home")))
        self._reset_profile_settings()
        self._configuration_overrides = {
            key: kwargs[key] for key in ("db_path", "profile_isolation", "default_scope", "tools",
                                         "shared_surface_path", "shared_surface_read")
            if kwargs.get(key) is not None
        }
        self._agent_identity = kwargs.get("agent_identity", None) or ""

        # Apply provider-specific config from kwargs (Hermes-passed) or config.yaml fallback
        try:
            self._apply_provider_config(kwargs)
        except Exception as exc:
            # LOCAL PATCH: P31 config refusal also deactivates a previous profile.
            self._init_error = exc
            self._deactivate_in_module()
            raise

        # LOCAL PATCH: P30 no Hermes version check at init; `doctor` reports the version.
        # C25: Register the Hermes auxiliary LLM backend BEFORE the skip-context
        # early return. The backend is process-global and needed by sleep even in
        # skip-context sessions (subagent/cron/flush can still run memory tools).
        try:
            from hermes_memory_provider.hermes_llm_adapter import register_hermes_host_llm
            from mnemosyne.core.llm_backends import get_host_llm_backend
            # LOCAL PATCH: P22 delayed cleanup may only clear its own registration.
            with _host_llm_registration_lock:
                existing_backend = get_host_llm_backend()
                if self._agent_context in self._skip_contexts and existing_backend is not None:
                    # A skipped child uses the primary registration without taking ownership.
                    # A reinitialized owner retains responsibility for its own registration.
                    if self._host_llm_backend is not existing_backend:
                        self._host_llm_backend = None
                elif register_hermes_host_llm():
                    self._host_llm_backend = get_host_llm_backend()
                    logger.info("Mnemosyne registered Hermes auxiliary LLM backend for memory operations")
        except Exception as exc:
            logger.debug("Mnemosyne could not register Hermes auxiliary LLM backend: %s", exc)

        if self._agent_context in self._skip_contexts:
            logger.debug("Mnemosyne skipped: non-primary context=%s", self._agent_context)
            # C13: a skip-context re-init must DEACTIVATE the instance if
            # it was previously active in this process. Without this, a
            # primary -> subagent re-init keeps _provider_active=True and
            # silences the legacy plugin's pre_llm_call for the subagent
            # session -- which the plugin used to handle (it has no
            # skip-context check of its own). Preserving legacy behavior
            # for the plugin in skip contexts is the smaller blast radius
            # vs. silently dropping memory injection for those sessions.
            self._deactivate_in_module()
            return

        try:
            # LOCAL PATCH: P31 open the explicit resolved store directly.
            # Mnemosyne's wrapper import used to initialize a default DB first.
            BeamMemory = _get_beam_class()
            self._beam = BeamMemory(session_id=self._session_id, db_path=self._db_path,
                                    seed_config=False, channel_id=kwargs.get("channel_id", ""))
            from .cli import describe_memory_location
            for line in describe_memory_location(self._db_path, self._hermes_home):
                if line.strip().startswith("WARNING"):
                    logger.warning("Mnemosyne: %s", line.strip())
                else:
                    logger.info("Mnemosyne: %s", line.strip())

        except Exception as e:
            # C27: capture the exception so system_prompt_block() can render a
            # visible "UNAVAILABLE" banner every turn and handle_tool_call()
            # can return a structured `memory_unavailable` response. Without
            # this, an operator misconfiguration (corrupt DB, missing extras,
            # permissions, schema mismatch) silently masquerades as "the agent
            # doesn't remember anything" with no signal to the user.
            logger.warning("Mnemosyne init failed: %s", e)
            self._beam = None
            self._init_error = e

        # C13: activate AFTER the BeamMemory init result is known. If
        # init succeeded (_beam is set) the provider is the live memory
        # surface and the plugin path should defer. If init FAILED the
        # provider can't serve prefetch() / handle_tool_call() either,
        # so leaving the plugin's pre_llm_call enabled preserves a
        # legacy fallback that at least keeps the agent's memory
        # surface functional rather than silently breaking both paths.
        # Once C27 (provider-init-error-visible) merges, this fallback
        # becomes redundant -- but until then it's the conservative
        # choice (codex review #1).
        if self._beam is not None:
            # Core BeamMemory.sleep() performs model-refresh auto-apply without
            # direct access to Hermes provider state. Attach the provider's
            # runtime identity so sleep writes canonical model facts into the
            # same owner namespace as explicit canonical tools, and so cron
            # contexts can suppress model-refresh mutation.
            self._beam.canonical_owner_id = self._canonical_owner()
            self._beam.agent_context = self._agent_context
            self._activate_in_module()
            self._init_audit_log()


    def system_prompt_block(self) -> str:
        if self._beam:
            # LOCAL PATCH: P23 accurately describes Hermes' native memory and
            # advertises only tools that this configuration actually exposes.
            tools: Optional[List[str]]
            try:
                tools = sorted(self._configured_tool_names())
            except Exception as exc:
                logger.warning("Could not resolve configured Mnemosyne tools for prompt: %s", exc)
                tools = None
            tool_line = (
                "Configured Mnemosyne tools: " + ", ".join(tools) + ".\n"
                if tools else
                "Mnemosyne tool exposure could not be resolved from configuration.\n"
                if tools is None else
                "No Mnemosyne tools are exposed; automatic memory context may still be available.\n"
            )
            tool_guidance: List[str] = []
            if tools and "mnemosyne_remember" in tools:
                tool_guidance.append("Use mnemosyne_remember for useful durable facts and corrections.")
            if tools and "mnemosyne_recall" in tools:
                tool_guidance.append("Use mnemosyne_recall when injected context does not answer the question.")
            base = (
                "# Mnemosyne Memory\n"
                "Hermes native memory (MEMORY.md and USER.md) remains active; keep it concise and use it for stable profile anchors. "
                "Use Mnemosyne for searchable episodic evidence and durable facts, and skills for procedural instructions.\n"
                + tool_line
                + "Call a Mnemosyne tool only when it appears in the configured tool list above. "
                + (" ".join(tool_guidance) + " " if tool_guidance else "")
                + "\n"
                + "When a `## Mnemosyne Context` block is injected into the current turn, "
                + "read it before calling retrieval tools. If it answers the user's question, "
                + "answer directly. Use session_search when both injected context and exposed Mnemosyne recall are missing, stale, or insufficient."
            )
            return self._with_persona_block(base)
        # C27: when init failed (as opposed to a deliberate skip-context),
        # surface the failure in the system prompt so the agent -- and through
        # it the user -- can see that memory is unavailable rather than
        # silently behaving as if nothing was stored. The skip-context case
        # still returns "" because that is the documented contract for
        # cron/subagent/skill_loop sessions.
        if self._init_error is not None:
            return (
                "# Mnemosyne Memory\n"
                f"⚠️ UNAVAILABLE: {self._init_error_reason()}\n"
                "Memory operations will fail this session. Resolve the underlying issue "
                "(check ~/.hermes/logs/agent.log for the WARNING) and restart Hermes to retry."
            )
        return ""

    def register_prefetch_source(self, name: str, fn: Callable[..., Any]) -> None:
        """Register an extra prefetch source. ``fn(query, *, session_id)`` returns
        either a pre-formatted block (str) or a list of hit dicts. A profile opts
        the source in by listing ``name`` in its ``sources``. ``"bank"`` is the
        built-in memory-bank source and cannot be overridden here."""
        if name and name != "bank":
            self._prefetch_sources[name] = fn
            self._ensure_prefetch_state()
            with self._prefetch_cache_lock:
                self._prefetch_cache.clear()

    # LOCAL PATCH: P23 exact-query cache stays opt-in until a deployment
    # measures a useful next-turn hit rate.
    def _prefetch_cache_enabled(self) -> bool:
        # Exact-query prewarming is opt-in until a deployment measures a useful
        # next-turn hit rate. The regular provider path remains unchanged.
        return _parse_env_bool("MNEMOSYNE_PREFETCH_CACHE_ENABLED", False)

    # LOCAL PATCH: P23 key on visibility, database generation and render knobs.
    def _prefetch_cache_key(self, query: str, session_id: str, profile: "PrefetchProfile") -> Optional[Tuple[Any, ...]]:
        """Build a private exact-query key including SQLite visibility state."""
        beam = self._beam
        if beam is None:
            return None
        try:
            cur = beam.conn.cursor()
            data_version = int(cur.execute("PRAGMA data_version").fetchone()[0])
            connection_identity = id(beam.conn)
            if getattr(self, "_prefetch_conn_identity", None) != connection_identity:
                self._prefetch_conn_identity = connection_identity
                self._prefetch_recall_changes = 0
                cache_lock = getattr(self, "_prefetch_cache_lock", None)
                if cache_lock is not None:
                    with cache_lock:
                        self._prefetch_cache.clear()
            local_changes = max(
                0,
                int(getattr(beam.conn, "total_changes", 0))
                - int(getattr(self, "_prefetch_recall_changes", 0)),
            )
        except Exception:
            # A broken/unsupported connection must never turn into a stale hit.
            return None
        try:
            db_path = str(Path(beam.db_path).resolve()) if getattr(beam, "db_path", None) else ""
        except Exception:
            db_path = str(getattr(beam, "db_path", "") or "")
        try:
            owner_id = self._canonical_owner()
        except Exception:
            owner_id = ""
        runtime_visibility = (
            str(getattr(beam, "session_id", "") or ""),
            str(getattr(beam, "author_id", "") or ""),
            str(getattr(beam, "channel_id", "") or ""),
            str(getattr(self, "_current_session_id", "") or ""),
            str(getattr(self, "_agent_context", "") or ""),
            bool(getattr(self, "_profile_isolation_enabled", False)),
        )
        source_revisions = []
        for source in profile.sources:
            if source == "bank":
                continue
            fn = getattr(self, "_prefetch_sources", {}).get(source)
            revision = getattr(fn, "cache_revision", None) if fn is not None else None
            if revision is None:
                # External sources may change without touching Mnemosyne's DB.
                return None
            source_revisions.append((source, str(revision)))
        render_knobs = tuple(
            (name, os.environ.get(name, ""))
            for name in (
                "MNEMOSYNE_PREFETCH_CONTENT_CHARS",
                "MNEMOSYNE_PREFETCH_TOTAL_CHARS",
                "MNEMOSYNE_PREFETCH_MODEL_SLOT_LIMIT",
                "MNEMOSYNE_PREFETCH_MODEL_SLOT_MIN_OVERLAP",
                "MNEMOSYNE_PREFETCH_INCLUDE_RAW_TOOL_CAPTURE",
                "MNEMOSYNE_PREFETCH_CACHE_TTL_SECONDS",
            )
        )
        query_digest = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()
        return (
            query_digest, str(session_id), repr(profile), db_path, runtime_visibility,
            tuple(source_revisions), render_knobs,
            str(owner_id or ""), data_version, local_changes,
        )

    # LOCAL PATCH: P23 diagnostics expose aggregate counts and timings only.
    def _prefetch_cache_snapshot(self) -> Dict[str, Any]:
        """PII-safe cache and output telemetry for doctor/evaluation."""
        self._ensure_prefetch_state()
        stats = getattr(self, "_prefetch_stats", {})
        lock = getattr(self, "_prefetch_cache_lock", None)
        with lock:
            snapshot = dict(stats)
        snapshot["cache_enabled"] = self._prefetch_cache_enabled()
        snapshot["cache_entries"] = len(getattr(self, "_prefetch_cache", {}))
        snapshot["total_char_budget"] = _prefetch_total_char_budget()
        try:
            snapshot["cache_ttl_seconds"] = min(
                max(float(os.environ.get("MNEMOSYNE_PREFETCH_CACHE_TTL_SECONDS", "30")), 1.0), 300.0
            )
        except ValueError:
            snapshot["cache_ttl_seconds"] = 30.0
        snapshot["bank"] = dict(getattr(self, "_prefetch_last_bank_counts", {}))
        snapshot["last_source_chars"] = dict(getattr(self, "_prefetch_last_source_chars", {}))
        snapshot["errors_by_source"] = dict(getattr(self, "_prefetch_errors", {}))
        return snapshot

    # LOCAL PATCH: P23 diagnose provider failures without storing query or
    # memory text; Hermes can distinguish a broken store from a no-match.
    def _record_prefetch_error(self, source: str, exc: BaseException) -> None:
        self._ensure_prefetch_state()
        with self._prefetch_cache_lock:
            self._prefetch_stats["errors"] = int(self._prefetch_stats.get("errors", 0)) + 1
            self._prefetch_stats["last_error"] = f"{source}: {type(exc).__name__}"
            self._prefetch_errors[source] = self._prefetch_errors.get(source, 0) + 1

    def get_prefetch_diagnostics(self) -> Dict[str, Any]:
        """Return prefetch timings, budget and cache counters without content."""
        return self._prefetch_cache_snapshot()

    # LOCAL PATCH: P24 emit Hermes' deterministic recall indicator only after
    # successful non-empty injection; failed recall and no-match stay distinct.
    def recall_status(self) -> Optional[Any]:
        status_type = _HERMES_RECALL_STATUS
        if status_type is None:
            try:
                from agent.memory_provider import RecallStatus as status_type
            except ImportError:
                return None
        stats = self._prefetch_cache_snapshot()
        if stats.get("last_error") or not str(getattr(self, "_prefetch_last_context", "") or "").strip():
            return None
        bank = getattr(self, "_prefetch_last_bank_counts", {})
        count = int(bank.get("selected", 0)) if isinstance(bank, dict) else 0
        try:
            return status_type(provider_label="Mnemosyne", count=count, glyph="🧠")
        except (TypeError, ValueError):
            return None

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for injection, driven by the active profile.

        The profile selects which sources to merge (default: just the memory
        ``bank``), the recall knobs, and the filter/dedup toggles. The default
        ``general`` profile retains the prior ranking thresholds; P23 source
        classification and aggregate budgeting apply to all profiles."""
        self._ensure_prefetch_state()
        with self._prefetch_cache_lock:
            self._prefetch_stats["last_error"] = None
            self._prefetch_last_context = ""
            self._prefetch_last_bank_counts = {}
            self._prefetch_last_recall_ids = []
        if not self._beam or self._agent_context in self._skip_contexts:
            return ""
        started = time.perf_counter()
        profile = _resolve_profile(self._prefetch_profile)
        key: Optional[Tuple[Any, ...]] = None
        if self._prefetch_cache_enabled():
            with self._ensure_beam_access_lock():
                key = self._prefetch_cache_key(query, session_id, profile)
                if key is not None:
                    with self._prefetch_cache_lock:
                        cached = self._prefetch_cache.get(key)
                        ttl = min(max(_parse_env_float("MNEMOSYNE_PREFETCH_CACHE_TTL_SECONDS", 30.0), 1.0), 300.0)
                        if cached is not None and time.monotonic() - cached[0] <= ttl:
                            self._prefetch_cache.move_to_end(key)
                        else:
                            if cached is not None:
                                self._prefetch_cache.pop(key, None)
                            cached = None
                    if cached is not None and self._bump_cached_prefetch_recall(cached[3]):
                        with self._prefetch_cache_lock:
                            self._prefetch_stats["cache_hits"] += 1
                            self._prefetch_stats["calls"] += 1
                            self._prefetch_stats["last_chars"] = len(cached[1])
                            self._prefetch_stats["max_chars"] = max(
                                self._prefetch_stats["max_chars"], len(cached[1])
                            )
                            self._prefetch_stats["last_duration_ms"] = (time.perf_counter() - started) * 1000
                            self._prefetch_last_context = cached[1]
                            self._prefetch_last_bank_counts = dict(cached[2])
                            self._prefetch_last_recall_ids = list(cached[3])
                            return cached[1]
                    if cached is not None:
                        with self._prefetch_cache_lock:
                            self._prefetch_cache.pop(key, None)
                    with self._prefetch_cache_lock:
                        self._prefetch_stats["cache_misses"] += 1
        blocks: List[str] = []
        source_chars: Dict[str, int] = {}
        for src in profile.sources:
            try:
                if src == "bank":
                    block = self._prefetch_bank(query, session_id, profile)
                else:
                    fn = self._prefetch_sources.get(src)
                    block = _coerce_source_output(
                        fn(query, session_id=session_id), profile,
                        header=f"## Context ({src})",
                    ) if fn else ""
            except Exception as e:
                logger.debug("Mnemosyne prefetch source %r failed: %s", src, e)
                self._record_prefetch_error(src, e)
                block = ""
            if block:
                blocks.append(block)
                source_chars[src] = len(block)
        # Per-contact identity memories must surface on EVERY turn, independent
        # of the semantic recall query. Routing them through recall is a latent
        # bug: a short/generic opener ("Hi", a nickname) does not match the
        # identity text, so it never enters recall's top_k window and the
        # importance filter never sees it -- the agent then loses track of who
        # it is talking to. Inject them deterministically at the FRONT, scoped
        # strictly to the active session_id, deduplicated against whatever
        # recall already surfaced. No identity rows == no-op (legacy behavior).
        # LOCAL PATCH: P19 identity/model reads need the same protection as bank recall.
        with self._ensure_beam_access_lock():
            model_block = self._prefetch_model_slots(query, profile)
            if model_block:
                blocks.insert(0, model_block)
                source_chars["model"] = len(model_block)
            identity_block = self._prefetch_identity(blocks, profile)
            if identity_block:
                blocks.insert(0, identity_block)
                source_chars["identity"] = len(identity_block)
        if profile.dedup:
            blocks = _dedup_blocks(blocks)
        rendered, was_truncated = _budget_prefetch_blocks(
            [b for b in blocks if b], _prefetch_total_char_budget()
        )
        # Cache only if no writer changed the SQLite view during assembly.
        with self._ensure_beam_access_lock():
            fresh_key = self._prefetch_cache_key(query, session_id, profile) if key is not None else None
            if key is not None and fresh_key == key:
                with self._prefetch_cache_lock:
                    self._prefetch_cache[key] = (
                        time.monotonic(), rendered,
                        dict(getattr(self, "_prefetch_last_bank_counts", {})),
                        list(getattr(self, "_prefetch_last_recall_ids", [])),
                    )
                    self._prefetch_cache.move_to_end(key)
                    while len(self._prefetch_cache) > 8:
                        self._prefetch_cache.popitem(last=False)
            with self._prefetch_cache_lock:
                self._prefetch_stats["calls"] += 1
                self._prefetch_stats["last_chars"] = len(rendered)
                self._prefetch_stats["max_chars"] = max(self._prefetch_stats["max_chars"], len(rendered))
                if was_truncated:
                    self._prefetch_stats["truncations"] += 1
                self._prefetch_stats["last_duration_ms"] = (time.perf_counter() - started) * 1000
                self._prefetch_last_source_chars = source_chars
                self._prefetch_last_context = rendered
        return rendered

    def _ensure_prefetch_state(self) -> None:
        """Support older/new test instances created without __init__()."""
        if not hasattr(self, "_prefetch_cache_lock"):
            self._prefetch_cache_lock = threading.Lock()
        if not hasattr(self, "_prefetch_cache"):
            self._prefetch_cache = OrderedDict()
        if not hasattr(self, "_prefetch_stats"):
            self._prefetch_stats = {
                "cache_hits": 0, "cache_misses": 0, "queued": 0,
                "calls": 0, "last_chars": 0, "max_chars": 0,
                "truncations": 0, "last_duration_ms": 0.0,
                "last_lock_wait_ms": 0.0, "last_recall_ms": 0.0,
                "errors": 0, "last_error": None,
            }
        if not hasattr(self, "_prefetch_errors"):
            self._prefetch_errors = {}
        if not hasattr(self, "_prefetch_last_context"):
            self._prefetch_last_context = ""
        if not hasattr(self, "_prefetch_last_bank_counts"):
            self._prefetch_last_bank_counts = {}
        if not hasattr(self, "_prefetch_last_recall_ids"):
            self._prefetch_last_recall_ids = []
        if not hasattr(self, "_prefetch_recall_changes"):
            self._prefetch_recall_changes = 0
        if not hasattr(self, "_prefetch_conn_identity"):
            self._prefetch_conn_identity = None

    # LOCAL PATCH: P23 retain BeamMemory's recall_count/last_recalled side
    # effects on cache hits, while treating those provider-owned updates as
    # recall bookkeeping rather than a content-generation change.
    def _bump_cached_prefetch_recall(self, recall_ids: List[Tuple[str, str]]) -> bool:
        if not recall_ids or self._beam is None:
            return True
        before = int(getattr(self._beam.conn, "total_changes", 0))
        now = datetime.now().isoformat()
        try:
            by_table: Dict[str, List[str]] = {"working_memory": [], "episodic_memory": []}
            for memory_id, tier in recall_ids:
                table = {"working": "working_memory", "episodic": "episodic_memory"}.get(tier)
                if table and memory_id:
                    by_table[table].append(memory_id)
            for table, ids in by_table.items():
                if not ids:
                    continue
                placeholders = ",".join("?" * len(ids))
                self._beam.conn.execute(
                    f"UPDATE {table} SET recall_count = recall_count + 1, last_recalled = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, *ids),
                )
            self._beam.conn.commit()
            self._prefetch_recall_changes += int(self._beam.conn.total_changes) - before
            return True
        except Exception as exc:
            self._record_prefetch_error("cache_recall_tracking", exc)
            return False

    def _prefetch_identity(self, existing_blocks: List[str], profile: "PrefetchProfile") -> str:
        """Render the always-inject identity block for the active session.

        Pulls identity memories straight from the active ``session_id`` (see
        ``_identity_fichas``) and renders them in the same format as the memory
        bank, tagged ``[IDENTITY]``. Rows whose content already appears in the
        blocks recall produced are dropped, so a query that *does* match the
        identity never yields a duplicate. Returns ``""`` when there is nothing
        to inject.
        """
        rows = self._identity_fichas()
        if not rows:
            return ""
        already = "\n".join(existing_blocks)
        content_limit = _prefetch_content_char_limit() or profile.content_char_limit
        lines: List[str] = ["## Mnemosyne Context"]
        seen: set = set()
        for r in rows:
            content = r.get("content", "")
            if not content or content in seen:
                continue
            disp = _format_prefetch_content(content, content_limit)
            # Dedup against anything recall already surfaced (raw or truncated).
            if content in already or disp in already:
                continue
            seen.add(content)
            ts = r.get("timestamp", "")[:16] if r.get("timestamp") else ""
            imp = r.get("importance", 0.95)
            lines.append(f"  [{ts}] (importance {imp:.2f}) [IDENTITY] {disp}")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def _prefetch_model_slots(self, query: str, profile: "PrefetchProfile") -> str:
        """Render relevant accepted canonical model slots for silent prefetch.

        Model cards are a display/debug view; normal prompt injection uses only
        selected canonical model slots with clear query overlap. This mirrors the
        useful part of Hindsight mental-model injection without globally
        injecting whole cards.
        """
        beam = self._beam
        if beam is None:
            return ""
        query_tokens = _prefetch_model_slot_tokens(query)
        if not query_tokens:
            return ""
        try:
            max_slots = int(os.environ.get("MNEMOSYNE_PREFETCH_MODEL_SLOT_LIMIT", "3") or "3")
        except (TypeError, ValueError):
            max_slots = 3
        try:
            min_signal = int(os.environ.get("MNEMOSYNE_PREFETCH_MODEL_SLOT_MIN_OVERLAP", "1") or "1")
        except (TypeError, ValueError):
            min_signal = 1
        try:
            store = getattr(beam, "canonical", None)
            if store is None:
                from mnemosyne.core.canonical import CanonicalStore
                store = CanonicalStore(db_path=beam.db_path, conn=beam.conn)
                beam.canonical = store
            owner_id = self._canonical_owner()
            rows = []
            for category in ("model:user", "model:workflow", "model:project", "model:agent"):
                rows.extend(store.list(owner_id, category=category))
        except Exception as e:
            logger.debug("Mnemosyne model-slot prefetch failed (non-fatal): %s", e)
            self._record_prefetch_error("model", e)
            return ""
        scored: List[tuple] = []
        for row in rows:
            text = " ".join(str(row.get(k) or "") for k in ("category", "name", "body"))
            tokens = _prefetch_model_slot_tokens(text)
            overlap = len(query_tokens & tokens)
            if overlap < min_signal:
                continue
            confidence = float(row.get("confidence") or 0.0)
            scored.append((overlap, confidence, row))
        if not scored:
            return ""
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        content_limit = _prefetch_content_char_limit() or profile.content_char_limit
        lines = ["## Mnemosyne Model Context"]
        for _, _, row in scored[:max_slots]:
            body = _format_prefetch_content(str(row.get("body") or ""), content_limit)
            body = " ".join(body.split())
            if not body:
                continue
            category = str(row.get("category") or "model")
            name = str(row.get("name") or "slot").replace("_", " ")
            lines.append(f"  [{category}] {name}: {body}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _identity_fichas(self) -> List[Dict[str, Any]]:
        """Return ALL identity memories for the ACTIVE session, deterministically.

        Identity memories (source='identity') answer "who am I talking to?" and
        must be injected on every turn regardless of the user's message. Routing
        them through semantic recall is a latent bug: a short/generic opener does
        not match the identity text, so it never enters recall's top_k window and
        the importance filter never sees it. This pulls them straight from the
        active session_id with a direct, query-independent SQL read. Strictly
        session-scoped, so there is zero cross-session leakage.
        """
        out: List[Dict[str, Any]] = []
        beam = self._beam
        if beam is None:
            return out
        try:
            cur = beam.conn.cursor()
            cur.execute(
                "SELECT content, importance, timestamp FROM working_memory "
                "WHERE source='identity' AND session_id=? "
                "ORDER BY importance DESC, timestamp DESC",
                (beam.session_id,),
            )
            for content, importance, timestamp in cur.fetchall():
                if not content:
                    continue
                out.append({
                    "content": content,
                    "importance": importance if importance is not None else 0.95,
                    "timestamp": timestamp or "",
                    "source": "identity",
                    "_always_inject": True,
                })
        except Exception as e:
            logger.debug("Mnemosyne identity read failed (non-fatal): %s", e)
            self._record_prefetch_error("identity", e)
        return out

    def _prefetch_bank(self, query: str, session_id: str, profile: "PrefetchProfile") -> str:
        """The built-in memory-bank source: hybrid recall with temporal weighting,
        relevance + low-quality filtering, scoped to author_id when available.
        Parameterized by *profile*."""
        try:
            import os
            author_id = self._beam.author_id or os.environ.get("MNEMOSYNE_AUTHOR_ID")
            overfetch = max(profile.top_k * 2, _PREFETCH_OVERFETCH)  # over-fetch; junk filtered below
            recall_kwargs: Dict[str, Any] = dict(
                query=query, top_k=overfetch,
                temporal_weight=profile.temporal_weight,
                temporal_halflife=profile.temporal_halflife,
            )
            # Pass tuning weights only when the profile sets them, so the default
            # profile still lets recall() resolve its own weights.
            if profile.importance_weight is not None:
                recall_kwargs["importance_weight"] = profile.importance_weight
            if profile.vec_weight is not None:
                recall_kwargs["vec_weight"] = profile.vec_weight
            if profile.fts_weight is not None:
                recall_kwargs["fts_weight"] = profile.fts_weight
            # Only pass author_id when explicitly non-empty.  Passing an empty
            # falsy author_id is harmless (no (1=1) bypass), but passing a real
            # non-empty one triggers the (1=1) clause in beam.recall() that
            # SKIPS session/channel filtering entirely -- which would defeat
            # the gateway_session_key thread isolation above.  Multi-agent
            # deployments that NEED author_id filtering can set it and accept
            # the wider scope; the common case (single-user, per-thread
            # sessions) should never bypass session scoping.
            if author_id:
                recall_kwargs["author_id"] = author_id
            # LOCAL PATCH: P23 separates database lock wait from engine recall,
            # and records BeamMemory's attribution writes for cache generation.
            lock_started = time.perf_counter()
            with self._ensure_beam_access_lock():
                lock_wait_ms = (time.perf_counter() - lock_started) * 1000
                recall_started = time.perf_counter()
                changes_before_recall = int(getattr(self._beam.conn, "total_changes", 0))
                results = self._beam.recall(**recall_kwargs)
                recall_ms = (time.perf_counter() - recall_started) * 1000
                recall_changes = int(getattr(self._beam.conn, "total_changes", 0)) - changes_before_recall
                self._prefetch_recall_changes += max(recall_changes, 0)
                self._prefetch_last_recall_ids = [
                    (str(row.get("id")), str(row.get("tier")))
                    for row in results
                    if row.get("id") and row.get("tier") in {"working", "episodic"}
                ]
            self._ensure_prefetch_state()
            with self._prefetch_cache_lock:
                self._prefetch_stats["last_lock_wait_ms"] = lock_wait_ms
                self._prefetch_stats["last_recall_ms"] = recall_ms
            if not results:
                self._prefetch_last_bank_counts = {
                    "rows_seen": 0, "selected": 0, "skipped_low_quality": 0,
                    "skipped_source": 0, "skipped_relevance": 0,
                }
                return ""
            # Filter out low-relevance results to prevent context pollution.
            # Importance alone is not enough for silent injection: a memory must
            # also have a real topical signal. Raw transcript rows need a
            # stronger topical signal than distilled facts/preferences.
            filtered = []
            counts = {
                "rows_seen": len(results), "selected": 0, "skipped_low_quality": 0,
                "skipped_source": 0, "skipped_relevance": 0,
            }
            for r in results:
                if profile.drop_low_quality and _is_low_quality_prefetch(r.get("content", "")):
                    counts["skipped_low_quality"] += 1
                    continue
                if profile.exclude_assistant and _prefetch_source_quality(r) <= 0:
                    counts["skipped_source"] += 1
                    continue
                signal = _prefetch_topic_signal(r)
                score = float(r.get("score") or 0.0)
                importance = float(r.get("importance") or 0.0)
                required_signal = profile.raw_min_topic_signal if _prefetch_is_raw(r) else profile.min_topic_signal
                if signal < required_signal:
                    counts["skipped_relevance"] += 1
                    continue
                if score < profile.min_score and importance < profile.min_importance:
                    counts["skipped_relevance"] += 1
                    continue
                filtered.append(r)

            filtered.sort(key=_prefetch_adjusted_score, reverse=True)
            if profile.semantic_dedup:
                filtered = _semantic_dedup_prefetch(filtered)
            # Cap back to the intended injection size after over-fetch+filter.
            filtered = filtered[:profile.top_k]
            counts["selected"] = len(filtered)
            self._prefetch_last_bank_counts = counts
            if not filtered:
                return ""
            lines = ["## Mnemosyne Context"]
            content_limit = _prefetch_content_char_limit() or profile.content_char_limit
            for r in filtered:
                content = _format_prefetch_content(
                    r.get("content", ""),
                    content_limit,
                )
                content = " ".join(content.split())
                ts = r.get("timestamp", "")[:16] if r.get("timestamp") else ""
                imp = r.get("importance", 0.0)
                trust = r.get("trust_tier", "STATED")
                trust_tag = f" [{trust}]" if trust != "STATED" else ""
                source = str(r.get("source") or "").strip()
                source_tag = f", source {source}" if source and source != "conversation" else ""
                lines.append(f"  [{ts}] (importance {imp:.2f}{source_tag}){trust_tag} {content}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Mnemosyne prefetch failed: %s", e)
            self._record_prefetch_error("bank", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Warm the exact next-use query on Hermes' serialized background lane.

        No provider thread or writer queue is created. The cache is bounded,
        keyed to the current database generation, and can be disabled with
        MNEMOSYNE_PREFETCH_CACHE_ENABLED=0 when evaluating a baseline.
        """
        # LOCAL PATCH: P23 use Hermes' existing serialized callback lane.
        if not self._beam or self._agent_context in self._skip_contexts or not self._prefetch_cache_enabled():
            return
        self._ensure_prefetch_state()
        with self._prefetch_cache_lock:
            self._prefetch_stats["queued"] += 1
        self.prefetch(query, session_id=session_id)

    def _ensure_sync_turn_telemetry(self) -> None:
        """Initialize sync_turn telemetry for tests that construct via __new__."""
        if not hasattr(self, "_sync_turn_lock"):
            self._sync_turn_lock = threading.Lock()
        self._ensure_beam_access_lock()
        if not hasattr(self, "_sync_turn_telemetry"):
            self._sync_turn_telemetry = {
                "pending_queue_length": 0,
                "max_queue_length": 0,
                "completed": 0,
                "failed": 0,
                # Reserved for a future bounded async queue; v1 keeps sync_turn
                # inline but exposes stable diagnostic keys.
                "merged": 0,
                "dropped": 0,
                "slow_sync_count": 0,
                "last_duration_ms": None,
                "max_duration_ms": 0.0,
                "last_error": None,
                "in_flight": 0,
            }

    def _ensure_beam_access_lock(self):
        """Return the per-provider Beam lock, including for __new__ test instances."""
        try:
            return self._beam_access_lock
        except AttributeError:
            # setdefault atomically publishes one per-instance lock when
            # concurrent __new__ callers both need lazy initialization.
            # LOCAL PATCH: P19 preserve reentrancy for lazily constructed instances.
            return self.__dict__.setdefault("_beam_access_lock", threading.RLock())

    def _sync_turn_diagnostics(self) -> Dict[str, Any]:
        """Return a PII-safe snapshot of sync_turn telemetry."""
        self._ensure_sync_turn_telemetry()
        with self._sync_turn_lock:
            return dict(self._sync_turn_telemetry)

    @staticmethod
    def _sanitize_sync_turn_error(exc: BaseException) -> str:
        """Bound error detail without including user/assistant content."""
        return f"{type(exc).__name__}: <redacted>"

    # LOCAL PATCH (F1): bounds for the tool/function turns captured out of
    # sync_turn(messages=...). Bounded because a tool transcript can be huge.
    SYNC_TOOL_MESSAGE_LIMIT = 5
    SYNC_TOOL_MESSAGE_CHAR_LIMIT = 2000

    # LOCAL PATCH: P24 keep the actual per-turn writer bounded and distinct
    # from initialization-time user_id/agent_identity.
    @staticmethod
    def _normalize_turn_author(author: Any) -> Dict[str, Any]:
        if not isinstance(author, dict):
            return {}
        normalized: Dict[str, Any] = {}
        author_id = author.get("id", author.get("author_id"))
        author_name = author.get("name", author.get("author_name"))
        is_bot = author.get("is_bot", author.get("author_is_bot"))
        if author_id is not None and str(author_id).strip():
            normalized["id"] = str(author_id).strip()[:200]
        if author_name is not None and str(author_name).strip():
            normalized["name"] = str(author_name).strip()[:200]
        if isinstance(is_bot, bool):
            normalized["is_bot"] = is_bot
        return normalized

    # LOCAL PATCH: P24 accept Hermes API-v2 per-turn speaker metadata while
    # keeping older managers compatible (they omit this optional keyword).
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None,
                  turn_author: Optional[Dict[str, Any]] = None) -> None:
        """Persist the turn to Mnemosyne episodic memory.

        LOCAL PATCH (F1): `messages` is accepted. Hermes only passes the full
        turn message list to providers whose signature accepts it (see
        MemoryManager._provider_sync_accepts_messages), and run_agent.py does
        pass it. The [USER]/[ASSISTANT] pair cannot carry tool/function turns,
        so those are stored when the operator opts in by adding 'tool' to
        sync_roles. With the default roles this method behaves exactly as
        upstream.
        """
        if not self._beam or self._agent_context in self._skip_contexts:
            return
        author = self._normalize_turn_author(
            turn_author if turn_author is not None else getattr(self, "_current_turn_author", {})
        )
        # LOCAL PATCH: P28 attach a host source reference only when one exact,
        # stable Hermes message ID matches the content and active session.
        from hermes_memory_provider.evidence import turn_metadata
        host_session_id = session_id or getattr(self, "_current_session_id", "")
        started = time.perf_counter()
        self._ensure_sync_turn_telemetry()
        with self._sync_turn_lock:
            self._sync_turn_telemetry["in_flight"] += 1
            in_flight = int(self._sync_turn_telemetry["in_flight"])
            # v1 does not introduce a separate async queue yet. Expose the
            # current in-flight sync work through the queue-shaped diagnostic
            # fields so operators can see backlog pressure without raw content.
            self._sync_turn_telemetry["pending_queue_length"] = in_flight
            self._sync_turn_telemetry["max_queue_length"] = max(
                int(self._sync_turn_telemetry.get("max_queue_length") or 0),
                in_flight,
            )
        try:
            with self._ensure_beam_access_lock():
                if "user" in self._sync_roles and user_content and len(user_content) > 5 and not self._should_filter(user_content):
                    user_limit = _sync_turn_user_limit()
                    uc = user_content[:user_limit] if user_limit > 0 else user_content
                    user_metadata = turn_metadata(
                        user_content, "user", messages, host_session_id, author,
                    )
                    self._beam.remember(
                        content=f"[USER] {uc}",
                        source="conversation",
                        importance=0.5,
                        scope=self._default_scope,
                        extract_entities=True,
                        metadata=user_metadata,
                    )
                    self._capture_identity_signals(
                        user_content, turn_author=author, source_metadata=user_metadata,
                    )
                if "assistant" in self._sync_roles and assistant_content and len(assistant_content) > 10 and not self._should_filter(assistant_content):
                    assistant_limit = _sync_turn_assistant_limit()
                    ac = assistant_content[:assistant_limit] if assistant_limit > 0 else assistant_content
                    assistant_metadata = turn_metadata(
                        assistant_content, "assistant", messages, host_session_id, author,
                    )
                    self._beam.remember(
                        content=f"[ASSISTANT] {ac}",
                        source="conversation",
                        importance=0.15,
                        scope=self._default_scope,
                        extract_entities=True,
                        metadata=assistant_metadata,
                    )
                if "tool" in self._sync_roles and messages:
                    # LOCAL PATCH (F1): store the tool/function turns that the
                    # user/assistant strings cannot carry. Opt-in via
                    # sync_roles, last N messages only, per-message cap.
                    tool_turns = [
                        m for m in messages
                        if str(m.get("role", "")).lower() in ("tool", "function")
                        and str(m.get("content") or "").strip()
                    ][-self.SYNC_TOOL_MESSAGE_LIMIT:]
                    for m in tool_turns:
                        text = str(m.get("content"))[:self.SYNC_TOOL_MESSAGE_CHAR_LIMIT]
                        role = str(m.get("role", "tool")).lower()
                        tool_metadata = turn_metadata(
                            str(m.get("content") or ""), role, messages, host_session_id, author,
                        )
                        tool_metadata.update({"role": role, "name": m.get("name")})
                        self._beam.remember(
                            content=f"[TOOL] {text}",
                            source="conversation_tool",
                            importance=0.2,
                            scope=self._default_scope,
                            metadata=tool_metadata,
                        )
            self._turn_count += 1
            if self._auto_sleep_enabled and self._turn_count % 10 == 0:
                self._maybe_auto_sleep()
            with self._sync_turn_lock:
                self._sync_turn_telemetry["completed"] += 1
                self._sync_turn_telemetry["last_error"] = None
        except Exception as e:
            with self._sync_turn_lock:
                self._sync_turn_telemetry["failed"] += 1
                self._sync_turn_telemetry["last_error"] = self._sanitize_sync_turn_error(e)
            logger.debug("Mnemosyne sync_turn failed: %s", self._sanitize_sync_turn_error(e))
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            slow = duration_ms >= (self._SYNC_TURN_SLOW_THRESHOLD_SECONDS * 1000.0)
            with self._sync_turn_lock:
                self._sync_turn_telemetry["in_flight"] = max(0, self._sync_turn_telemetry["in_flight"] - 1)
                self._sync_turn_telemetry["pending_queue_length"] = int(self._sync_turn_telemetry["in_flight"])
                self._sync_turn_telemetry["last_duration_ms"] = duration_ms
                self._sync_turn_telemetry["max_duration_ms"] = max(
                    float(self._sync_turn_telemetry.get("max_duration_ms") or 0.0),
                    duration_ms,
                )
                if slow:
                    self._sync_turn_telemetry["slow_sync_count"] += 1
                snapshot = dict(self._sync_turn_telemetry)
            if slow:
                logger.warning(
                    "Mnemosyne sync_turn slow: duration_ms=%.1f completed=%s failed=%s pending_queue_length=%s",
                    duration_ms,
                    snapshot["completed"],
                    snapshot["failed"],
                    snapshot["pending_queue_length"],
                )

    # Identity-significant expressions the user may voice about themselves or
    # their relationship to their work. When a match is found, the memory is
    # saved with source="identity" and higher importance so it survives
    # consolidation and remains recallable across sessions.
    _IDENTITY_SIGNALS: List[str] = [
        "feeling like",
        "imposter",
        "impostor",
        "barely know",
        "don't know my own",
        "don't even know how",
        "want them to feel",
        "i'm proud",
        "i feel like a",
        "i don't know how to",
    ]

    # LOCAL PATCH: P24 retain the caller's current-turn speaker on global
    # identity facts; no process-level startup identity is substituted.
    def _capture_identity_signals(
        self,
        user_content: str,
        *,
        turn_author: Optional[Dict[str, Any]] = None,
        # LOCAL PATCH: P28 preserve an exact host-provided reference when a
        # user statement is copied into the durable identity memory.
        source_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        content_lower = user_content.lower()
        author = self._normalize_turn_author(
            turn_author if turn_author is not None else getattr(self, "_current_turn_author", {})
        )
        for signal in self._IDENTITY_SIGNALS:
            if signal in content_lower:
                # Save identity memory with high importance for durable recall
                self._beam.remember(
                    content=f"[IDENTITY] {user_content[:400]}",
                    source="identity",
                    importance=0.85,
                    scope="global",
                    veracity="stated",
                    # LOCAL PATCH: P24 retain per-turn speaker provenance on
                    # identity captures without changing visibility scope.
                    metadata={
                        **(source_metadata or {}),
                        **({"turn_author": author} if author else {}),
                    },
                )
                break  # One identity memory per turn

    # LOCAL PATCH: P22 separate slow consolidation from foreground connection access.
    def _ensure_consolidation_lock(self):
        lock = self.__dict__.setdefault("_consolidation_lock", threading.Lock())
        with lock:
            self.__dict__.setdefault("_consolidation_thread", None)
            self.__dict__.setdefault("_consolidation_running", False)
            self.__dict__.setdefault("_consolidation_stopping", False)
            self.__dict__.setdefault("_consolidation_cleanup_pending", False)
            self.__dict__.setdefault("_consolidation_status", {
                "state": "idle", "last_trigger": None, "started_at": None,
                "finished_at": None, "duration_ms": None, "error_class": None,
                "reused_triggers": 0, "skipped_triggers": 0,
                "shutdown_timed_out": False, "running": False, "stopping": False,
            })
            self.__dict__.setdefault("_consolidation_started_monotonic", None)
        return lock

    def _consolidation_status_snapshot(self) -> Dict[str, Any]:
        """Return only bounded lifecycle metadata; never expose worker inputs."""
        # LOCAL PATCH: P29 read lock-free because diagnose is called while
        # holding the foreground lock, and admission may hold the lifecycle
        # lock while waiting briefly for that foreground lock. Writers replace
        # the fixed-key dict under the lifecycle lock, so this reference read
        # cannot wait behind model work or create a lock-order inversion.
        status = self._consolidation_status
        return dict(status)

    # LOCAL PATCH: P29 publish bounded outcome data for the existing worker.
    def _finish_consolidation(self, error: Optional[BaseException] = None) -> None:
        """Publish completion using a monotonic duration and an error type only."""
        with self._consolidation_lock:
            now = time.monotonic()
            started = self._consolidation_started_monotonic
            updates = {
                "finished_at": time.time(),
                "duration_ms": (
                    max(0.0, (now - started) * 1000.0) if started is not None else None
                ),
            }
            if error is not None:
                updates.update(state="failed", error_class=type(error).__name__[:128])
            else:
                updates.update(state="succeeded", error_class=None)
            self._consolidation_status = {**self._consolidation_status, **updates, "running": False}
            self._consolidation_running = False
            self._consolidation_started_monotonic = None
            if self._consolidation_cleanup_pending:
                self._unregister_host_llm()
                self._consolidation_cleanup_pending = False

    # LOCAL PATCH: P29 track admission, reuse, skip and worker-start failures.
    def _start_consolidation(self, trigger: str) -> Optional[threading.Thread]:
        """Start at most one worker; concurrent triggers reuse it without spending budget."""
        with self._ensure_consolidation_lock():
            if self._consolidation_stopping:
                self._consolidation_status = {**self._consolidation_status,
                                              "skipped_triggers": self._consolidation_status["skipped_triggers"] + 1}
                return None
            if self._consolidation_running:
                self._consolidation_status = {**self._consolidation_status,
                                              "reused_triggers": self._consolidation_status["reused_triggers"] + 1}
                return self._consolidation_thread
            beam_lock = self._ensure_beam_access_lock()
            with beam_lock:
                beam = self._beam
                if beam is None:
                    self._consolidation_status = {**self._consolidation_status,
                                                  "skipped_triggers": self._consolidation_status["skipped_triggers"] + 1}
                    return None
                if trigger == "auto_sleep":
                    working = beam.get_working_stats().get("total", 0)
                    if working <= self._auto_sleep_threshold:
                        self._consolidation_status = {**self._consolidation_status,
                                                      "skipped_triggers": self._consolidation_status["skipped_triggers"] + 1}
                        return None
                    cutoff = (datetime.now() - timedelta(hours=WORKING_MEMORY_TTL_HOURS // 2)).isoformat()
                    if not beam._count_unconsolidated_before(cutoff):
                        self._consolidation_status = {**self._consolidation_status,
                                                      "skipped_triggers": self._consolidation_status["skipped_triggers"] + 1}
                        return None
                # Copy values now, rather than dereferencing a mutable Beam in the worker.
                beam_kwargs = {
                    "session_id": beam.session_id,
                    "db_path": beam.db_path,
                    "author_id": beam.author_id,
                    "author_type": beam.author_type,
                    "channel_id": beam.channel_id,
                }
                foreground_conn = beam.conn
                owner_id = getattr(beam, "canonical_owner_id", "default")
                agent_context = getattr(beam, "agent_context", self._agent_context)
            skip = self._reserve_reflection_budget(trigger)
            if skip is not None:
                self._consolidation_status = {**self._consolidation_status,
                                              "skipped_triggers": self._consolidation_status["skipped_triggers"] + 1}
                logger.info("Mnemosyne %s skipped: %s", trigger, json.dumps(skip))
                return None

            trigger_name = trigger if trigger in {"auto_sleep", "session_end"} else "other"
            self._consolidation_status = {**self._consolidation_status, **{
                "state": "running", "last_trigger": trigger_name,
                "started_at": time.time(), "finished_at": None,
                "duration_ms": None, "error_class": None,
                "shutdown_timed_out": False, "running": True,
            }}
            self._consolidation_started_monotonic = time.monotonic()
            self._consolidation_running = True

            def _sleep_isolated():
                sleep_beam = None
                worker_conn = None
                worker_error: Optional[BaseException] = None
                try:
                    # Construction does schema work, so serialize it with provider access.
                    # After construction, every SQLite operation uses the worker's handle.
                    with beam_lock:
                        sleep_beam = _get_beam_class()(seed_config=False, **beam_kwargs)
                        worker_conn = sleep_beam.conn
                        if worker_conn is foreground_conn:
                            worker_conn = None
                            raise RuntimeError("consolidation requires an independent SQLite connection")
                        sleep_beam.canonical_owner_id = owner_id
                        sleep_beam.agent_context = agent_context
                    # The audited engine releases db_lock only for model work;
                    # all SQL/statement lifetimes remain serialized with P19.
                    sleep_beam.sleep(db_lock=beam_lock)
                except BaseException as inner:
                    worker_error = inner
                    if isinstance(inner, Exception):
                        logger.warning("Mnemosyne %s worker failed: %s", trigger, self._sanitize_sync_turn_error(inner))
                    else:
                        logger.warning("Mnemosyne %s worker exited with %s", trigger, type(inner).__name__[:128])
                        raise
                finally:
                    try:
                        if worker_conn is not None:
                            try:
                                with beam_lock:
                                    worker_conn.close()
                            except BaseException as inner:
                                if worker_error is None:
                                    worker_error = inner
                                if isinstance(inner, Exception):
                                    logger.debug("Mnemosyne consolidation close failed: %s", self._sanitize_sync_turn_error(inner))
                                else:
                                    logger.debug("Mnemosyne consolidation close exited with %s", type(inner).__name__[:128])
                                    raise
                    finally:
                        self._finish_consolidation(worker_error)

            try:
                thread = spawn_context_thread(_sleep_isolated, name=f"mnemosyne-{trigger}-sleep")
            except Exception as exc:
                self._consolidation_running = False
                self._consolidation_status = {**self._consolidation_status, **{
                    "state": "failed", "finished_at": time.time(),
                    "duration_ms": max(0.0, (time.monotonic() - self._consolidation_started_monotonic) * 1000.0),
                    "error_class": type(exc).__name__[:128], "running": False,
                }}
                self._consolidation_started_monotonic = None
                raise
            self._consolidation_thread = thread
            return thread

    def _maybe_auto_sleep(self) -> None:
        try:
            # No join here: Hermes' serialized sync executor must remain available
            # for the next turn while independent consolidation reasons in background.
            self._start_consolidation("auto_sleep")
        except Exception as exc:
            logger.warning("Mnemosyne auto-sleep failed: %s", self._sanitize_sync_turn_error(exc))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return configured tool schemas; independent of Beam initialization state."""
        return self._configured_tool_schemas()

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        # LOCAL PATCH: P19 tools must not read or commit sync_turn's open transaction.
        with self._ensure_beam_access_lock():
            return self._dispatch_tool_call(tool_name, args, **kwargs)

    def _dispatch_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if not self.has_tool(tool_name):
                return json.dumps({"error": f"Unknown Mnemosyne tool: {tool_name}"})
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if tool_name == "mnemosyne_sleep" and self._reflect_disabled_for_cron and (self._agent_context or "").strip().lower() == "cron":
            return json.dumps(self._reflection_skip_response("reflect_disabled_for_cron", "tool"))
        if not self._beam:
            # C27: structured response carries the actual failure reason
            # instead of a generic "not initialized" string. Status field
            # is parseable by tool consumers; `reason` is human-readable for
            # the agent to relay to the user. The `error` field is kept
            # alongside `status` so callers using the prior "if 'error' in
            # payload" pattern (codex review finding #4) don't silently
            # misclassify unavailable as success.
            reason = self._init_error_reason()
            return json.dumps({
                "status": "memory_unavailable",
                "tool": tool_name,
                "reason": reason,
                "error": f"Mnemosyne unavailable: {reason}",
            })
        try:
            if tool_name == "mnemosyne_remember":
                return self._handle_remember(args)
            elif tool_name == "mnemosyne_batch":
                return self._handle_batch(args)
            elif tool_name == "mnemosyne_recall":
                return self._handle_recall(args)
            elif tool_name == "mnemosyne_shared_remember":
                return self._handle_shared_remember(args)
            elif tool_name == "mnemosyne_shared_recall":
                return self._handle_shared_recall(args)
            elif tool_name == "mnemosyne_shared_forget":
                return self._handle_shared_forget(args)
            elif tool_name == "mnemosyne_shared_stats":
                return self._handle_shared_stats(args)
            elif tool_name == "mnemosyne_sleep":
                return self._handle_sleep(args)
            elif tool_name == "mnemosyne_stats":
                return self._handle_stats(args)
            elif tool_name == "mnemosyne_invalidate":
                return self._handle_invalidate(args)
            elif tool_name == "mnemosyne_validate":
                return self._handle_validate(args)
            elif tool_name == "mnemosyne_get":
                return self._handle_get(args)
            elif tool_name == "mnemosyne_triple_add":
                return self._handle_triple_add(args)
            elif tool_name == "mnemosyne_triple_end":
                return self._handle_triple_end(args)
            elif tool_name == "mnemosyne_triple_query":
                return self._handle_triple_query(args)
            elif tool_name == "mnemosyne_remember_canonical":
                return self._handle_remember_canonical(args)
            elif tool_name == "mnemosyne_recall_canonical":
                return self._handle_recall_canonical(args)
            elif tool_name == "mnemosyne_forget_canonical":
                return self._handle_forget_canonical(args)
            elif tool_name == "mnemosyne_apply_pending":
                return self._handle_apply_pending(args)
            elif tool_name == "mnemosyne_model_card":
                return self._handle_model_card(args)
            elif tool_name == "mnemosyne_model_refresh":
                return self._handle_model_refresh(args)
            elif tool_name == "mnemosyne_scratchpad_write":
                return self._handle_scratchpad_write(args)
            elif tool_name == "mnemosyne_scratchpad_read":
                return self._handle_scratchpad_read(args)
            elif tool_name == "mnemosyne_scratchpad_clear":
                return self._handle_scratchpad_clear(args)
            elif tool_name == "mnemosyne_export":
                return self._handle_export(args)
            elif tool_name == "mnemosyne_update":
                return self._handle_update(args)
            elif tool_name == "mnemosyne_forget":
                return self._handle_forget(args)
            elif tool_name == "mnemosyne_import":
                return self._handle_import(args)
            elif tool_name == "mnemosyne_diagnose":
                return self._handle_diagnose(args)
            elif tool_name == "mnemosyne_recall_diagnostics":
                return self._handle_recall_diagnostics(args)
            elif tool_name == "mnemosyne_task_progress":
                return self._handle_task_progress(args)
            elif tool_name == "mnemosyne_graph_query":
                return self._handle_graph_query(args)
            elif tool_name == "mnemosyne_graph_link":
                return self._handle_graph_link(args)
            elif tool_name.startswith("mnemosyne_sync_"):
                return self._handle_sync_tool(tool_name, args)
            elif tool_name.startswith("mnemosyne_persona_"):
                return self._handle_persona_tool(tool_name, args)
            else:
                return json.dumps({"error": f"Unknown Mnemosyne tool: {tool_name}"})
        except Exception as e:
            logger.error("Mnemosyne tool %s failed: %s", tool_name, e)
            return json.dumps({"error": f"Mnemosyne tool '{tool_name}' failed: {e}"})

    def _handle_sync_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        try:
            adapter = getattr(self, "_sync_adapter", None)
            if adapter is None:
                from hermes_memory_provider.sync_adapter import SyncAdapter
                adapter = SyncAdapter(self._beam, {})
                self._sync_adapter = adapter
            return adapter.handle_tool_call(tool_name, args)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": f"Sync adapter unavailable: {exc}",
            })

    def _handle_persona_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        try:
            adapter = getattr(self, "_persona_adapter", None)
            if adapter is None:
                from hermes_memory_provider.persona_adapter import PersonaAdapter
                adapter = PersonaAdapter(self._beam, {})
                self._persona_adapter = adapter
            return adapter.handle_tool_call(tool_name, args)
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": f"Persona adapter unavailable: {exc}",
            })

    def _handle_remember(self, args: Dict[str, Any]) -> str:
        # Import at call-site so the provider module loads even when
        # the optional veracity_consolidation chain isn't on path
        # (BeamMemory ships a fallback). At call-time the import is
        # always satisfied because BeamMemory is already constructed.
        from mnemosyne.core.veracity_consolidation import clamp_veracity

        content = args.get("content", "")
        importance = float(args.get("importance", 0.5))
        source = args.get("source", "user")
        scope = args.get("scope", self._default_scope)
        valid_until = args.get("valid_until", None) or None
        extract_entities = bool(args.get("extract_entities", False))
        extract = bool(args.get("extract", False))
        metadata = args.get("metadata") or None
        # LOCAL PATCH: P28 caller references stay explicitly asserted; they
        # are never upgraded to host-provided evidence at the provider edge.
        from hermes_memory_provider.evidence import asserted_metadata, source_reference
        metadata = asserted_metadata(metadata)
        veracity = clamp_veracity(
            args.get("veracity"), context="mnemosyne_remember"
        )
        if not content:
            return json.dumps({"error": "content is required"})

        # Write-approval gate: stage to pending when enabled.
        if _write_approval_enabled():
            pid = _stage_pending_write({
                "tool": "mnemosyne_remember",
                "content": content,
                "importance": importance,
                "source": source,
                "scope": scope,
                "valid_until": valid_until,
                "extract_entities": extract_entities,
                "extract": extract,
                "metadata": metadata,
                "veracity": veracity,
            })
            return json.dumps({
                "status": "staged",
                "pending_id": pid,
                "content_preview": content[:100],
                "message": "Write staged for approval. Use mnemosyne_apply_pending to commit.",
            })

        memory_id = self._beam.remember(
            content=content,
            importance=importance,
            source=source,
            scope=scope,
            valid_until=valid_until,
            extract_entities=extract_entities,
            extract=extract,
            metadata=metadata,
            veracity=veracity,
        )
        _source_ref = source_reference(metadata)
        self._audit_event(
            "remember", memory_id=memory_id,
            bank="global" if scope == "global" else "private",
            scope=scope, source_tool="mnemosyne_remember",
            metadata={
                "source_type": str(source)[:128],
                **({"source_id": _source_ref["message_id"]} if _source_ref else {}),
            },
        )
        return json.dumps({
            "status": "stored",
            "memory_id": memory_id,
            "content_preview": content[:100],
            "extract_entities": extract_entities,
            "extract": extract,
            "metadata": metadata,
            "veracity": veracity,
        })

    def _handle_batch(self, args: Dict[str, Any]) -> str:
        try:
            normalized = validate_batch_operations(args.get("operations"))
        except BatchValidationError as exc:
            return json.dumps(batch_validation_error_payload(exc))

        # LOCAL PATCH: P28 normalize batch remember metadata before preview,
        # approval staging, or the lower-level batch writer can persist it.
        for operation in normalized:
            payload = operation.get("payload")
            if (operation.get("action") == "remember" and isinstance(payload, dict)
                    and "metadata" in payload):
                payload["metadata"] = self._asserted_caller_metadata(payload["metadata"])

        if bool(args.get("dry_run", False)):
            return json.dumps(dry_run_batch(normalized))

        # Write-approval gate: stage each operation individually.
        if _write_approval_enabled():
            staged = []
            for op in normalized:
                payload = op.get("payload", {})
                pid = _stage_pending_write({
                    "tool": "mnemosyne_batch",
                    "action": op.get("action", ""),
                    "content": payload.get("content", ""),
                    "importance": payload.get("importance", 0.5),
                    "source": payload.get("source", "user"),
                    "scope": payload.get("scope", self._default_scope),
                    "metadata": payload.get("metadata"),
                    "veracity": payload.get("veracity"),
                })
                staged.append(pid)
            return json.dumps({
                "status": "staged",
                "pending_ids": staged,
                "count": len(staged),
                "message": f"{len(staged)} writes staged for approval. Use mnemosyne_apply_pending to commit.",
            })

        return json.dumps(apply_beam_batch(
            self._beam,
            normalized,
            default_scope=self._default_scope,
            remember_source_default="user",
            remember_source_tool="mnemosyne_batch",
            audit_event=self._audit_event,
            extract_defaults_global=False,
        ))

    def _handle_recall(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        top_k = int(args.get("limit", 5))
        temporal_weight = float(args.get("temporal_weight", 0.0))
        query_time = args.get("query_time") or None
        temporal_halflife_hours = float(args.get("temporal_halflife", 24))
        explain = bool(args.get("explain", False))
        if not query:
            return json.dumps({"error": "query is required"})

        # Forward configurable scoring weights ONLY when the caller actually
        # supplied them. beam.recall treats None as "fall back to env var or
        # default" via _normalize_weights; passing 0.0 / 0.5 / etc. when the
        # caller didn't ask for tuning would override that resolution and
        # break MNEMOSYNE_*_WEIGHT env-var deployments. See issue #45.
        recall_kwargs: Dict[str, Any] = {
            "top_k": top_k,
            "temporal_weight": temporal_weight,
            "query_time": query_time,
            "temporal_halflife": temporal_halflife_hours,
            "explain": explain,
        }
        for weight_key in ("vec_weight", "fts_weight", "importance_weight"):
            if weight_key in args:
                recall_kwargs[weight_key] = args[weight_key]

        recall_payload = self._beam.recall(query, **recall_kwargs)
        explain_payload = None
        if explain:
            explain_payload = recall_payload.get("explain", {})
            results = recall_payload.get("results", [])
        else:
            results = recall_payload

        # Tag private results with their bank so callers can distinguish from
        # shared-surface entries when surface read is enabled.
        for r in results:
            r.setdefault("bank", "private")

        # Optionally merge shared-surface results. Each surface result keeps
        # its own score (computed by the surface beam) and is tagged
        # bank="surface" / shared_surface=True. We merge the two ranked lists
        # by score (when present) and truncate to top_k overall.
        if self._shared_surface_read:
            try:
                self._ensure_surface_beam()
            except Exception as exc:
                logger.warning("Mnemosyne shared surface read failed: %s", exc)
            if self._surface_beam is not None:
                try:
                    surface_results = self._surface_beam.recall(query, top_k=top_k)
                    for r in surface_results:
                        r["shared_surface"] = True
                        r["bank"] = self._shared_surface_bank
                    combined = list(results) + list(surface_results)
                    combined.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
                    results = combined[:top_k]
                    if explain_payload is not None:
                        explain_payload.setdefault("provider", {})["shared_surface_untraced"] = len(surface_results)
                except Exception as exc:
                    logger.warning("Mnemosyne shared surface recall failed: %s", exc)

        response = {
            "query": query,
            "count": len(results),
            "temporal_weight": temporal_weight,
            "shared_surface_read": self._shared_surface_read,
            "results": results,
        }
        if explain_payload is not None:
            response["explain"] = explain_payload
        return json.dumps(response)

    @staticmethod
    def _surface_hash(content: str) -> str:
        import hashlib
        normalized = " ".join(str(content).lower().split())
        return hashlib.sha256(f"surface:v1:{normalized}".encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _surface_label(content: str, kind: str) -> str:
        prefixes = ("surface meta:", "surface preference:", "surface correction:", "surface identity:", "surface fact:")
        if content.lower().startswith(prefixes):
            return content
        label = {
            "meta": "Surface meta",
            "preference": "Surface preference",
            "correction": "Surface correction",
            "identity": "Surface identity",
        }.get(kind, "Surface meta")
        return f"{label}: {content}"

    def _ensure_surface_beam(self) -> None:
        if self._surface_beam is not None:
            return
        BeamMemory = _get_beam_class()
        shared_path = self._shared_surface_path or (_mnemosyne_root / "data" / "shared" / "mnemosyne.db")
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        self._shared_surface_path = shared_path
        self._surface_beam = BeamMemory(session_id="hermes_shared_surface", db_path=shared_path, seed_config=False)
        logger.info("Mnemosyne shared surface initialized: db=%s", shared_path)

    def _require_surface_beam(self) -> Optional[str]:
        try:
            self._ensure_surface_beam()
        except Exception as exc:
            logger.warning("Mnemosyne shared surface init failed: %s", exc)
        if self._surface_beam is None:
            return "shared surface DB is not initialized"
        return None

    def _handle_shared_remember(self, args: Dict[str, Any]) -> str:
        from mnemosyne.core.veracity_consolidation import clamp_veracity
        err = self._require_surface_beam()
        if err:
            return json.dumps({"error": err})
        content = (args.get("content") or "").strip()
        if not content:
            return json.dumps({"error": "content is required"})
        if content.startswith("[USER]") or content.startswith("[ASSISTANT]"):
            return json.dumps({"error": "raw conversation content is not allowed in shared memory"})
        kind = (args.get("kind") or "meta").strip().lower()
        if kind not in {"meta", "preference", "correction", "identity"}:
            return json.dumps({"error": "kind must be one of: meta, preference, correction, identity"})
        importance = max(0.0, min(float(args.get("importance", 0.8)), 1.0))
        metadata = args.get("metadata") or {}
        if not isinstance(metadata, dict):
            return json.dumps({"error": "metadata must be an object"})
        # LOCAL PATCH: P28 shared-memory callers cannot claim host-origin refs.
        metadata = self._asserted_caller_metadata(metadata)
        veracity = clamp_veracity(args.get("veracity"), context="mnemosyne_shared_remember")
        surface_content = self._surface_label(content, kind)
        stable_id = "sf_" + self._surface_hash(surface_content)
        meta = dict(metadata)
        meta.update({"shared_memory": True, "surface_kind": kind, "write_path": "manual_tool", "source_profile_session": self._session_id})
        existing_id = self._surface_beam._find_duplicate(surface_content)
        memory_id = self._surface_beam.remember(
            content=surface_content,
            source="surface_manual",
            importance=importance,
            metadata=meta,
            scope="global",
            memory_id=stable_id,
            veracity=veracity,
        )
        self._audit_event(
            "shared_remember", memory_id=memory_id, bank="surface",
            scope="global", source_tool="mnemosyne_shared_remember",
            metadata={"kind": kind, "existing": bool(existing_id)},
        )
        return json.dumps({
            "status": "existing_shared" if existing_id else "stored_shared",
            "memory_id": memory_id,
            "content_preview": surface_content[:120],
            "shared_db": str(self._shared_surface_path or ""),
            "kind": kind,
            "veracity": veracity,
        })

    def _handle_shared_recall(self, args: Dict[str, Any]) -> str:
        err = self._require_surface_beam()
        if err:
            return json.dumps({"error": err})
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})
        top_k = int(args.get("limit", 5))
        results = []
        for r in self._surface_beam.recall(query, top_k=top_k):
            r = dict(r)
            r["shared_surface"] = True
            r["bank"] = self._shared_surface_bank
            results.append(r)
        return json.dumps({"query": query, "count": len(results), "shared_db": str(self._shared_surface_path or ""), "results": results})

    def _handle_shared_forget(self, args: Dict[str, Any]) -> str:
        err = self._require_surface_beam()
        if err:
            return json.dumps({"error": err})
        memory_id = (args.get("memory_id") or "").strip()
        if not memory_id:
            return json.dumps({"error": "memory_id is required"})
        ok = self._surface_beam.forget_working(memory_id)
        if ok:
            self._audit_event(
                "shared_forget", memory_id=memory_id, bank="surface",
                source_tool="mnemosyne_shared_forget",
            )
        return json.dumps({"status": "deleted" if ok else "not_found", "memory_id": memory_id, "shared_db": str(self._shared_surface_path or "")})

    def _handle_shared_stats(self, args: Dict[str, Any]) -> str:
        err = self._require_surface_beam()
        if err:
            return json.dumps({"error": err})
        return json.dumps({"provider": "mnemosyne_shared", "shared_db": str(self._shared_surface_path or ""), "working": self._surface_beam.get_working_stats(), "episodic": self._surface_beam.get_episodic_stats()})

    def _handle_sleep(self, args: Dict[str, Any]) -> str:
        skip = self._reserve_reflection_budget("tool")
        if skip is not None:
            return json.dumps(skip)
        dry_run = bool(args.get("dry_run", False))
        force = bool(args.get("force", False))
        all_sessions = bool(args.get("all_sessions", False))
        if all_sessions and hasattr(self._beam, "sleep_all_sessions"):
            result = self._beam.sleep_all_sessions(dry_run=dry_run, force=force)
        else:
            result = self._beam.sleep(dry_run=dry_run, force=force)
        working = self._beam.get_working_stats()
        episodic = self._beam.get_episodic_stats()
        if not dry_run:
            self._audit_event(
                "sleep", bank="private", source_tool="mnemosyne_sleep",
                metadata={"all_sessions": all_sessions, "status": result.get("status")},
            )
        return json.dumps({"status": result.get("status", "consolidated"), "result": result, "working": working, "episodic": episodic})

    def _handle_stats(self, args: Dict[str, Any]) -> str:
        working = self._beam.get_working_stats()
        episodic = self._beam.get_episodic_stats()
        memoria = self._beam.get_memoria_stats()
        return json.dumps({"provider": "mnemosyne", "session_id": self._session_id, "working": working, "episodic": episodic, "memoria": memoria})

    def _handle_invalidate(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "")
        replacement_id = args.get("replacement_id", None) or None
        if not memory_id:
            return json.dumps({"error": "memory_id is required"})
        # LOCAL PATCH: P26 retain ownership for the durable deleted-row event.
        owner = self._audit_memory_owner(memory_id)
        ok = self._beam.invalidate(memory_id, replacement_id=replacement_id)
        if ok:
            if owner is not None:
                owner["source_tool"] = "mnemosyne_invalidate"
                owner["metadata"] = {
                    "memory_store": owner["metadata"].get("memory_store"),
                    "replacement_id": replacement_id,
                }
                self._audit_event("invalidate", memory_id=memory_id, **owner)
            return json.dumps({"status": "invalidated", "memory_id": memory_id})
        return json.dumps({"status": "memory_not_found", "memory_id": memory_id})

    def _handle_validate(self, args: Dict[str, Any]) -> str:
        """Collaborative attestation: any agent can attest, update, invalidate,
        or delete any memory in either bank. Original author_id is preserved.
        validator/validated_at/validation_count on the live row capture the
        most recent attester. memory_validations table holds last 3 entries
        (trim trigger maintains the ring buffer).
        """
        memory_id = args.get("memory_id", "")
        action = args.get("action", "")
        bank = args.get("bank", "private")
        validator = args.get("validator") or self._agent_identity or "unknown"
        new_content = args.get("new_content", "")
        note = args.get("note", "")

        if not memory_id:
            return json.dumps({"error": "memory_id is required"})
        if action not in ("attest", "update", "invalidate", "delete"):
            return json.dumps({"error": f"unknown action: {action}"})
        if bank not in ("private", "surface"):
            return json.dumps({"error": f"unknown bank: {bank}"})
        if action == "update" and not new_content:
            return json.dumps({"error": "new_content is required for action='update'"})

        # Pick the right beam (private vs surface)
        if bank == "surface":
            err = self._require_surface_beam()
            if err:
                return json.dumps({"error": err})
            target_beam = self._surface_beam
        else:
            if not self._beam:
                return json.dumps({"error": "private beam not initialized"})
            target_beam = self._beam
        conn = target_beam.conn

        # LOCAL PATCH: P21 validate honours the same session/global visibility
        # as get/update/invalidate/forget. The unscoped `WHERE id = ?` let any
        # session attest (and read the content of) private rows owned by
        # another session. The engine helpers also honour cross-session mode
        # from one runtime snapshot (P20/P21).
        _visible, _visible_params = self._visible_memory_clause(target_beam, memory_id)
        # Verify the memory exists (and is visible) in this bank
        existing = conn.execute(
            "SELECT id, author_id, content, session_id, scope FROM working_memory WHERE " + _visible,
            _visible_params,
        ).fetchone()
        if not existing:
            return json.dumps({
                "error": "memory_not_found",
                "memory_id": memory_id,
                "bank": bank,
            })

        author_id = existing[1]
        prev_content = existing[2]
        # LOCAL PATCH: P26 derive audit ownership from the exact visible row
        # check already performed for this validation operation.
        owner: Optional[Dict[str, Any]] = None
        if bank == "private" and existing[4] in ("session", "global"):
            owner = {
                "bank": "global" if existing[4] == "global" else "private",
                "scope": str(existing[4]),
                "session_id": str(existing[3] or self._session_id),
                "source_tool": "mnemosyne_validate",
            }

        # Apply the action atomically. Every mutation carries the same
        # visibility predicate (LOCAL PATCH: P21), so a row that fails the
        # check above can never be written through a raced or reused ID.
        try:
            if action == "delete":
                conn.execute(
                    "DELETE FROM working_memory WHERE " + _visible,
                    _visible_params,
                )
            elif action == "update":
                conn.execute(
                    "UPDATE working_memory SET content = ?, validator = ?, "
                    "validated_at = CURRENT_TIMESTAMP, "
                    "validation_count = COALESCE(validation_count, 0) + 1 "
                    "WHERE " + _visible,
                    (new_content, validator, *_visible_params),
                )
            elif action == "invalidate":
                conn.execute(
                    "UPDATE working_memory SET valid_until = CURRENT_TIMESTAMP, "
                    "validator = ?, validated_at = CURRENT_TIMESTAMP, "
                    "validation_count = COALESCE(validation_count, 0) + 1 "
                    "WHERE " + _visible,
                    (validator, *_visible_params),
                )
            else:  # attest
                conn.execute(
                    "UPDATE working_memory SET validator = ?, "
                    "validated_at = CURRENT_TIMESTAMP, "
                    "validation_count = COALESCE(validation_count, 0) + 1 "
                    "WHERE " + _visible,
                    (validator, *_visible_params),
                )

            # Append to ring buffer (trigger trims to last 3 per memory_id)
            conn.execute(
                "INSERT INTO memory_validations "
                "(memory_id, validator, action, new_content, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (memory_id, validator, action,
                 new_content if action == "update" else None,
                 note or None),
            )
            conn.commit()
        except Exception as exc:
            return json.dumps({
                "error": "validation_failed",
                "reason": str(exc),
                "memory_id": memory_id,
            })

        # Audit log if available. Private events keep the target's stored scope
        # and session, including for the delete action; surface events remain
        # unattributed unless their separate owner model can prove access.
        try:
            if hasattr(self, "_audit_event"):
                if owner is not None:
                    owner["source_tool"] = "mnemosyne_validate"
                    owner["metadata"] = {"source_type": f"validate_{action}"}
                    self._audit_event(
                        action=f"validate_{action}", memory_id=memory_id, **owner,
                    )
                elif bank == "surface":
                    self._audit_event(
                        action=f"validate_{action}", memory_id=memory_id,
                        bank=bank, source_tool="mnemosyne_validate",
                    )
        except Exception:
            logger.debug("Mnemosyne audit event failed for validate", exc_info=True)

        return json.dumps({
            "status": f"validation_{action}",
            "memory_id": memory_id,
            "bank": bank,
            "validator": validator,
            "author_id": author_id,
            "previous_content": prev_content[:200] if prev_content else None,
        })

    def _handle_get(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "")
        if not memory_id:
            return json.dumps({"error": "memory_id is required"})
        result = self._beam.get(memory_id)
        if result is None:
            return json.dumps({"status": "not_found", "memory_id": memory_id})
        return json.dumps({"status": "ok", "memory": result})

    def _handle_triple_add(self, args: Dict[str, Any]) -> str:
        subject = args.get("subject", "")
        predicate = args.get("predicate", "")
        obj = args.get("object", "")
        valid_from = args.get("valid_from", None) or None
        if not all([subject, predicate, obj]):
            return json.dumps({"error": "subject, predicate, and object are required"})
        valid_until = args.get("valid_until", None) or None
        source = args.get("source", "") or "inferred"
        confidence = args.get("confidence", 1.0)
        supersede = args.get("supersede", True)
        add_triple, _ = _get_triple_module()
        triple_id = add_triple(subject, predicate, obj, valid_from=valid_from,
                               valid_until=valid_until, source=source,
                               confidence=confidence, supersede=supersede,
                               db_path=self._beam.db_path)
        return json.dumps({"status": "stored", "triple_id": triple_id})

    def _handle_triple_end(self, args: Dict[str, Any]) -> str:
        subject = args.get("subject", "")
        predicate = args.get("predicate", "")
        if not all([subject, predicate]):
            return json.dumps({"error": "subject and predicate are required"})
        obj = args.get("object", "") or None
        valid_until = args.get("valid_until", None) or None
        from mnemosyne.core.triples import end_triple
        n = end_triple(subject, predicate, object=obj, valid_until=valid_until,
                       db_path=self._beam.db_path)
        return json.dumps({"status": "ended", "count": n})

    def _handle_triple_query(self, args: Dict[str, Any]) -> str:
        subject = args.get("subject", "") or None
        predicate = args.get("predicate", "") or None
        obj = args.get("object", "") or None
        as_of = args.get("as_of", "") or None
        _, query_triples = _get_triple_module()
        results = query_triples(subject=subject, predicate=predicate, object=obj,
                                as_of=as_of, db_path=self._beam.db_path)
        return json.dumps({"count": len(results), "results": results})

    def _canonical_owner(self) -> str:
        """Owner id for canonical reads/writes: the active profile identity.

        Derived internally and never taken from tool args, so a profile cannot
        read or write another profile's canonical bank — owner isolation is
        enforced by construction. Falls back to "default" when no profile
        identity is set (single-profile / non-persona deployments)."""
        return (getattr(self, "_agent_identity", None) or "").strip() or "default"

    def _handle_remember_canonical(self, args: Dict[str, Any]) -> str:
        category = (args.get("category") or "").strip()
        name = (args.get("name") or "").strip()
        body = (args.get("body") or "").strip()
        if not category or not name:
            return json.dumps({"error": "category and name are required"})
        if not body:
            return json.dumps({"error": "body is required"})
        source = args.get("source") or "canonical_tool"
        try:
            confidence = float(args.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        owner_id = self._canonical_owner()
        row = self._beam.canonical.remember(
            owner_id, category, name, body,
            source=source, confidence=confidence,
        )
        status = row.pop("status", "stored")
        self._audit_event(
            "remember_canonical", bank="canonical",
            source_tool="mnemosyne_remember_canonical",
            metadata={"category": category, "name": name, "status": status,
                      "version": row.get("version")},
        )
        return json.dumps({
            "status": status,
            "owner_id": owner_id,
            "category": category,
            "name": name,
            "version": row.get("version"),
            "body_preview": body[:120],
        })

    def _handle_recall_canonical(self, args: Dict[str, Any]) -> str:
        category = (args.get("category") or "").strip()
        name = (args.get("name") or "").strip()
        query = (args.get("query") or "").strip()
        include_history = bool(args.get("include_history", False))
        try:
            limit = int(args.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        owner_id = self._canonical_owner()
        store = self._beam.canonical

        # Free-text search mode.
        if query:
            results = store.search(owner_id, query, limit=limit)
            return json.dumps({"mode": "search", "owner_id": owner_id,
                               "query": query, "count": len(results),
                               "results": results})
        # Exact slot read (optionally with history).
        if category and name:
            if include_history:
                results = store.history(owner_id, category, name)
                return json.dumps({"mode": "history", "owner_id": owner_id,
                                   "category": category, "name": name,
                                   "count": len(results), "results": results})
            row = store.recall(owner_id, category, name)
            return json.dumps({"mode": "recall", "owner_id": owner_id,
                               "category": category, "name": name,
                               "found": row is not None, "result": row})
        # List mode (whole bank, or one category).
        results = store.list(owner_id, category=category or None)
        return json.dumps({"mode": "list", "owner_id": owner_id,
                           "category": category or None,
                           "count": len(results), "results": results})

    def _handle_forget_canonical(self, args: Dict[str, Any]) -> str:
        category = (args.get("category") or "").strip()
        name = (args.get("name") or "").strip()
        if not category or not name:
            return json.dumps({"error": "category and name are required"})
        owner_id = self._canonical_owner()
        store = getattr(self._beam, "canonical", None)
        if store is None:
            from mnemosyne.core.canonical import CanonicalStore
            store = CanonicalStore(db_path=self._beam.db_path, conn=self._beam.conn)
            self._beam.canonical = store
        retired = store.forget(owner_id, category, name)
        return json.dumps({"retired": retired, "owner_id": owner_id,
                           "category": category, "name": name})

    def _handle_model_card(self, args: Dict[str, Any]) -> str:
        category = (args.get("category") or "").strip()
        if not category:
            return json.dumps({"error": "category is required"})
        title = (args.get("title") or "").strip() or None
        raw_names = args.get("names") or []
        if isinstance(raw_names, str):
            names = [n.strip() for n in raw_names.split(",") if n.strip()]
        else:
            names = [str(n).strip() for n in raw_names if str(n).strip()]
        owner_id = self._canonical_owner()
        store = getattr(self._beam, "canonical", None)
        if store is None:
            from mnemosyne.core.canonical import CanonicalStore
            store = CanonicalStore(db_path=self._beam.db_path, conn=self._beam.conn)
            self._beam.canonical = store
        card = store.model_card(owner_id, category, title=title, names=names or None)
        return json.dumps(card)

    def _handle_apply_pending(self, args: Dict[str, Any]) -> str:
        """Replay staged pending writes through the BEAM write path.

        Calls beam.remember() directly, bypassing the write_approval
        gate so approved records are committed without re-staging.
        """
        from hermes_constants import get_hermes_home
        from mnemosyne.core.veracity_consolidation import clamp_veracity

        pending_ids = args.get("pending_ids") or []
        if isinstance(pending_ids, str):
            pending_ids = [pid.strip() for pid in pending_ids.split(",") if pid.strip()]

        pending_dir = get_hermes_home() / "pending" / "memory"
        pending_dir = pending_dir.resolve()
        applied = []
        failed = []

        for pid in pending_ids:
            # Validate pid is a safe identifier: no path traversal
            if not isinstance(pid, str) or not pid.strip():
                failed.append({"id": str(pid), "error": "invalid: empty"})
                continue
            pid = pid.strip()
            if not all(c.isalnum() and c.isascii() for c in pid):
                failed.append({"id": pid, "error": "invalid: non-alphanumeric"})
                continue
            if len(pid) > 64:
                failed.append({"id": pid, "error": "invalid: too long"})
                continue
            record_path = (pending_dir / f"{pid}.json").resolve()
            if str(record_path.parent) != str(pending_dir):
                failed.append({"id": pid, "error": "invalid: path traversal"})
                continue
            if not record_path.is_file():
                failed.append({"id": pid, "error": "pending record not found"})
                continue

            try:
                record = json.loads(record_path.read_text())
                if record.get("id") != pid:
                    failed.append({"id": pid, "error": "id mismatch"})
                    continue
                payload = record.get("payload", {})
                content = payload.get("content", "")
                if not content:
                    failed.append({"id": pid, "error": "empty content"})
                    record_path.unlink(missing_ok=True)
                    continue

                memory_id = self._beam.remember(
                    content=content,
                    importance=float(payload.get("importance", 0.5)),
                    source=payload.get("source", "user"),
                    scope=payload.get("scope", self._default_scope),
                    valid_until=payload.get("valid_until"),
                    extract_entities=bool(payload.get("extract_entities", False)),
                    extract=bool(payload.get("extract", False)),
                    # LOCAL PATCH: P28 pending files are caller assertions at
                    # replay time too; approval does not authenticate a source.
                    metadata=self._asserted_caller_metadata(payload.get("metadata")),
                    veracity=clamp_veracity(
                        payload.get("veracity"), context="mnemosyne_apply_pending"
                    ),
                )
                record_path.unlink(missing_ok=True)
                applied.append({"id": pid, "memory_id": memory_id})
            except Exception as exc:
                failed.append({"id": pid, "error": str(exc)})

        return json.dumps({
            "applied": applied,
            "failed": failed,
            "applied_count": len(applied),
            "failed_count": len(failed),
        })

    def _handle_model_refresh(self, args: Dict[str, Any]) -> str:
        action = (args.get("action") or "list").strip().lower()
        if action != "list":
            return json.dumps({"error": "mnemosyne_model_refresh is diagnostic-only; sleep applies or rejects proposals automatically"})
        from mnemosyne.core import model_refresh
        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        status = (args.get("status") or "all").strip().lower()
        proposals = model_refresh.list_model_refresh_proposals(
            self._beam, status=status, limit=limit,
        )
        return json.dumps({
            "status": "ok",
            "mode": "diagnostic",
            "filter": status,
            "count": len(proposals),
            "proposals": proposals,
        })

    def _handle_recall_diagnostics(self, args: Dict[str, Any]) -> str:
        """Return recall path diagnostics (fallback rates, tier hit counts).

        Gated behind MNEMOSYNE_RECALL_DIAGNOSTICS=1 so operators must opt in
        to expose the tool. When the flag is unset the tool returns a
        concise 'disabled' message instead of the snapshot. This prevents
        accidental information disclosure and keeps the tool surface clean
        for operators who have not enabled recall instrumentation.
        """
        import os as _os
        if _os.environ.get("MNEMOSYNE_RECALL_DIAGNOSTICS", "0") != "1":
            return json.dumps({
                "status": "disabled",
                "message": (
                    "Recall diagnostics are not enabled. Set "
                    "MNEMOSYNE_RECALL_DIAGNOSTICS=1 to expose recall "
                    "path counters."
                ),
            })

        from mnemosyne.core.recall_diagnostics import get_recall_diagnostics, reset_recall_diagnostics
        snapshot = get_recall_diagnostics()
        do_reset = bool(args.get("reset", False))
        if do_reset:
            reset_recall_diagnostics()
        return json.dumps({
            "diagnostics": snapshot,
            "reset": do_reset,
        }, indent=2, default=str)

    def _handle_task_progress(self, args: Dict[str, Any]) -> str:
        """Track and recall cross-session task progression.

        This is intentionally stored as canonical state instead of another
        ordinary memory row. Recent transcript recall can find evidence of
        past work, but it cannot reliably answer "what is the current state?"
        after retries, crashes, or superseded attempts. A task:progress slot
        gives agents one owner-scoped current value per task, while the normal
        recall/session-search paths remain available for the historical trail.
        """
        action = args.get("action", "get").strip().lower()
        task = args.get("task", "").strip()
        state = args.get("state", "").strip()
        metadata = args.get("metadata", {}) or {}

        owner_id = self._canonical_owner()
        store = getattr(self._beam, "canonical", None)
        if store is None:
            from mnemosyne.core.canonical import CanonicalStore
            store = CanonicalStore(db_path=self._beam.db_path, conn=self._beam.conn)
            self._beam.canonical = store

        if action == "set":
            if not task:
                return json.dumps({"error": "task is required for set"})
            if not state:
                return json.dumps({"error": "state is required for set"})
            body = state
            if metadata:
                body += "\n" + json.dumps(metadata, default=str)
            store.remember(
                owner_id=owner_id,
                category="task:progress",
                name=task,
                body=body,
            )
            self._audit_event(
                "task_progress_set",
                bank="private",
                source_tool="mnemosyne_task_progress",
                metadata={"task": task},
            )
            return json.dumps({"status": "set", "owner_id": owner_id, "task": task, "state": state})

        elif action == "get":
            if not task:
                return json.dumps({"error": "task is required for get"})
            result = store.recall(owner_id, "task:progress", task)
            if result is None:
                return json.dumps({"status": "not_found", "task": task})
            return json.dumps({
                "status": "found",
                "task": task,
                "owner_id": owner_id,
                "state": result.get("body", ""),
                "valid_from": result.get("valid_from"),
                "created_at": result.get("created_at"),
            }, default=str)

        elif action == "list":
            all_facts = store.list(owner_id)
            tasks = [
                {
                    "task": f.get("name", ""),
                    "state": (f.get("body") or "")[:200],
                    "valid_from": f.get("valid_from"),
                    "created_at": f.get("created_at"),
                }
                for f in all_facts
                if f.get("category") == "task:progress"
            ]
            return json.dumps({"tasks": tasks, "count": len(tasks)}, default=str)

        elif action == "clear":
            if not task:
                return json.dumps({"error": "task is required for clear"})
            store.forget(owner_id, "task:progress", task)
            return json.dumps({"status": "cleared", "task": task})

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use set/get/list/clear."})

    def _handle_scratchpad_write(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "").strip()
        if not content:
            return json.dumps({"error": "Content is required"})
        pad_id = self._beam.scratchpad_write(content)
        return json.dumps({"status": "written", "id": pad_id})

    def _handle_scratchpad_read(self, args: Dict[str, Any]) -> str:
        entries = self._beam.scratchpad_read()
        return json.dumps({"entries_count": len(entries), "entries": entries})

    def _handle_scratchpad_clear(self, args: Dict[str, Any]) -> str:
        self._beam.scratchpad_clear()
        return json.dumps({"status": "cleared"})

    def _handle_export(self, args: Dict[str, Any]) -> str:
        output_path = args.get("output_path", "").strip()
        if not output_path:
            return json.dumps({"error": "output_path is required"})
        from mnemosyne.core.memory import Mnemosyne
        mem = Mnemosyne(session_id=self._session_id, db_path=self._beam.db_path, seed_config=False)
        result = mem.export_to_file(output_path)
        return json.dumps(result)

    def _handle_update(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return json.dumps({"error": "memory_id is required"})
        content = args.get("content")
        importance = args.get("importance")
        # LOCAL PATCH: P20 the engine returns False both for "no such row" and
        # for "nothing to change"; only the first one is a not_found.
        if content is None and importance is None:
            return json.dumps({"error": "content or importance is required", "memory_id": memory_id})
        # LOCAL PATCH: P26 capture the authorized target's owner before writing.
        owner = self._audit_memory_owner(memory_id)
        ok = self._beam.update_working(memory_id, content=content, importance=importance)
        memory_store: Optional[str] = "working"
        if not ok:
            # LOCAL PATCH: P20 update_working only matches working rows owned by
            # this session, while get/invalidate/forget also resolve global rows
            # and get/invalidate fall back to episodic memory.
            memory_store = self._update_get_visible_memory(memory_id, content, importance)
            ok = memory_store is not None
        result: Dict[str, Any] = {
            "status": "updated" if ok else "not_found",
            "memory_id": memory_id,
        }
        if ok:
            result["memory_store"] = memory_store
            if owner is not None:
                owner["source_tool"] = "mnemosyne_update"
                owner["metadata"] = {"memory_store": memory_store}
                self._audit_event("update", memory_id=memory_id, **owner)
        return json.dumps(result)

    def _update_get_visible_memory(self, memory_id: str, content: Optional[str],
                                   importance: Optional[float]) -> Optional[str]:
        """LOCAL PATCH: P20 update a row that `get` resolves but `update_working` misses.

        Uses the engine's visibility helpers and runtime cross-session mode
        over working memory first, then episodic memory, so
        an ID returned by `mnemosyne_get` is always editable. Returns the store
        that was updated, or None when the ID is not visible to this session.
        """
        beam = self._beam
        conn = getattr(beam, "conn", None)
        if conn is None:
            return None
        visible, visible_params = self._visible_memory_clause(beam, memory_id)
        updates: List[str] = []
        params: List[Any] = []
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if importance is not None:
            updates.append("importance = ?")
            params.append(importance)
        for table, store in (("working_memory", "working"), ("episodic_memory", "episodic")):
            row = conn.execute(
                f"SELECT rowid FROM {table} WHERE {visible}", visible_params
            ).fetchone()
            if row is None:
                continue
            try:
                conn.execute(
                    f"UPDATE {table} SET {', '.join(updates)} WHERE {visible}",
                    (*params, *visible_params),
                )
                if content is not None:
                    self._refresh_updated_embedding(store, memory_id, int(row[0]), content)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            invalidate_cache = getattr(beam, "_invalidate_query_cache", None)
            if callable(invalidate_cache):
                invalidate_cache()
            return store
        return None

    def _visible_memory_clause(self, beam: Any, memory_id: str):
        # LOCAL PATCH: P20/P21 use canonical engine scope and matching binds.
        from mnemosyne.core.beam import (
            _cross_session_enabled, _session_scope_filter, _session_scope_params,
        )
        cross_session = _cross_session_enabled()
        session_id = getattr(beam, "session_id", self._session_id)
        clause = _session_scope_filter(cross_session=cross_session)
        params = _session_scope_params(session_id, cross_session=cross_session)
        return f"id = ? AND {clause}", (memory_id, *params)

    def _refresh_updated_embedding(self, store: str, memory_id: str, rowid: int, content: str) -> None:
        """LOCAL PATCH: P20 keep dense recall in step with edited content.

        FTS is maintained by the engine's wm_au/em_au triggers; vectors are not.
        Best effort, like the engine's own update_working: a failed refresh must
        not lose the content edit.
        """
        try:
            if store == "episodic":
                refresh = getattr(self._beam, "_refresh_episodic_embedding", None)
                if callable(refresh):
                    refresh(memory_id, rowid, content)
                return
            from mnemosyne.core import beam as engine_beam
            embeddings = getattr(engine_beam, "_embeddings", None)
            store_embedding = getattr(engine_beam, "_store_working_embedding", None)
            if embeddings is None or store_embedding is None or not embeddings.available():
                return
            vec = embeddings.embed([content])
            if vec is not None and len(vec) > 0:
                store_embedding(self._beam.conn, memory_id, vec[0])
        except Exception as exc:
            logger.warning(
                "mnemosyne_update: embedding refresh failed for %s (%s): %s",
                memory_id, type(exc).__name__, exc,
            )

    def _handle_forget(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return json.dumps({"error": "memory_id is required"})
        # LOCAL PATCH: P26 retain ownership before the row is removed.
        owner = self._audit_memory_owner(memory_id)
        ok = self._beam.forget_working(memory_id)
        if ok and owner is not None:
            owner["source_tool"] = "mnemosyne_forget"
            owner["metadata"] = {"memory_store": owner["metadata"].get("memory_store")}
            self._audit_event(
                "forget", memory_id=memory_id, **owner,
            )
        return json.dumps({
            "status": "deleted" if ok else "not_found",
            "memory_id": memory_id,
        })

    def _handle_import(self, args: Dict[str, Any]) -> str:
        provider = (args.get("provider") or "").strip().lower()
        input_path = args.get("input_path", "").strip()
        dry_run = bool(args.get("dry_run", False))
        force = bool(args.get("force", False))

        from mnemosyne.core.memory import Mnemosyne
        mem = Mnemosyne(session_id=self._session_id, db_path=self._beam.db_path, seed_config=False)

        if provider:
            api_key = args.get("api_key", "").strip()
            user_id = args.get("user_id", "").strip() or None
            agent_id = args.get("agent_id", "").strip() or None
            base_url = args.get("base_url", "").strip() or None
            channel_id = args.get("channel_id")

            if not api_key:
                import os
                env_key = f"{provider.upper()}_API_KEY"
                api_key = os.environ.get(env_key, "")
            if not api_key:
                return json.dumps({
                    "error": f"api_key required for {provider} import. "
                             f"Set {provider.upper()}_API_KEY env var or pass api_key parameter.",
                })

            from mnemosyne.core.importers import import_from_provider
            result = import_from_provider(
                provider, mem,
                api_key=api_key,
                user_id=user_id,
                agent_id=agent_id,
                base_url=base_url,
                dry_run=dry_run,
                channel_id=channel_id,
            )
            return json.dumps(result.to_dict())

        if not input_path:
            return json.dumps({
                "error": "Either input_path (for file import) or provider "
                         "(for cross-provider import) is required",
            })
        stats = mem.import_from_file(input_path, force=force)
        self._audit_event(
            "import", bank="private", source_tool="mnemosyne_import",
            metadata={"input_path": input_path, "force": force, "stats": stats},
        )
        return json.dumps({"status": "imported", "stats": stats})

    def _handle_diagnose(self, args: Dict[str, Any]) -> str:
        from mnemosyne.diagnose import run_diagnostics
        repair_requested = bool(args.get("repair_vec_working", False))
        dry_run = bool(args.get("dry_run", False))
        diagnostic_kwargs: Dict[str, Any] = {
            "repair_vec_working": repair_requested,
            "dry_run": dry_run,
            # LOCAL PATCH: P31 diagnostics must inspect/repair the exact active store.
            "db_path": str(self._beam.db_path) if self._beam is not None else resolve_db_path(self._hermes_home),
            "seed_config": False,
            "log_dir": str(active_home(self._hermes_home) / "mnemosyne/logs"),
        }
        if self._beam is None:
            result = run_diagnostics(**diagnostic_kwargs)
            result["audit"] = self.get_audit_diagnostics()
            # LOCAL PATCH: P29 expose only bounded worker lifecycle metadata.
            result["consolidation"] = self._consolidation_status_snapshot()
            return json.dumps(result, indent=2, default=str)

        # The active provider bank shares its SQLite database with auto_sleep's
        # separate Beam connection. Keep every active-bank diagnostic access in
        # one critical section so checkpointing cannot invalidate statements in
        # the other connection (#498).
        with self._ensure_beam_access_lock():
            result = run_diagnostics(**diagnostic_kwargs)
            result["sync_turn"] = self._sync_turn_diagnostics()
            # LOCAL PATCH: P29 expose only bounded worker lifecycle metadata.
            result["consolidation"] = self._consolidation_status_snapshot()
            # LOCAL PATCH: P26 expose audit health without adding a provider tool.
            result["audit"] = self.get_audit_diagnostics()
            # LOCAL PATCH: P23 expose only timings, counts, cache state and
            # output size; prefetch telemetry never includes query or memory text.
            result["prefetch"] = self._prefetch_cache_snapshot()

            active_db = None
            try:
                active_db = getattr(self._beam, "db_path", None)
            except Exception:
                active_db = None

            if active_db:
                result["active_provider_db_path"] = str(active_db)
                result["profile_isolation_enabled"] = bool(self._profile_isolation_enabled)
                result.setdefault("key_findings", []).append(
                    f"Active Hermes Mnemosyne provider DB: {active_db}"
                )
                try:
                    import sqlite3
                    from mnemosyne.diagnose import _memory_orphan_diagnostics
                    # LOCAL PATCH: P31 diagnostic inspection cannot recreate a missing store.
                    con = sqlite3.connect(Path(active_db).resolve().as_uri() + "?mode=ro", uri=True)
                    try:
                        cur = con.cursor()
                        result["active_provider_counts"] = {
                            "working_memory": cur.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0],
                            "episodic_memory": cur.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0],
                            "facts": cur.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                        }
                        result["active_provider_orphan_diagnostics"] = _memory_orphan_diagnostics(con)
                    finally:
                        con.close()
                    try:
                        from mnemosyne.core.beam import repair_vec_working as _repair_vec_working, vec_working_coverage
                        if repair_requested:
                            result["active_provider_vec_working_repair"] = _repair_vec_working(
                                self._beam.conn, dry_run=dry_run
                            )
                            result["active_provider_vec_working"] = result[
                                "active_provider_vec_working_repair"
                            ].get("after", {})
                        else:
                            result["active_provider_vec_working"] = vec_working_coverage(self._beam.conn)
                    except Exception as exc:
                        result["active_provider_vec_working_error"] = str(exc)
                except Exception as exc:
                    result["active_provider_counts_error"] = str(exc)

            return json.dumps(result, indent=2, default=str)

    def _handle_graph_query(self, args: Dict[str, Any]) -> str:
        seed_id = args.get("seed_memory_id", "").strip()
        if not seed_id:
            return json.dumps({"error": "seed_memory_id is required"})
        depth = int(args.get("max_hops", 2))
        if depth < 1:
            return json.dumps({"error": "max_hops must be greater than 0"})
        edge_type = args.get("edge_type", "") or ""
        min_weight = float(args.get("min_weight", 0.0))
        if not (0.0 <= min_weight <= 1.0):
            return json.dumps({"error": "min_weight must be between 0.0 and 1.0"})
        if self._beam.episodic_graph is None:
            return json.dumps({"error": "Episodic graph not available"})
        related = self._beam.episodic_graph.find_related_memories(
            seed_id, depth=depth, edge_type=edge_type, min_weight=min_weight
        )
        return json.dumps({
            "seed_memory_id": seed_id,
            "max_hops": depth,
            "edge_type": edge_type or "all",
            "min_weight": min_weight,
            "count": len(related),
            "results": related,
        })

    def _handle_graph_link(self, args: Dict[str, Any]) -> str:
        source_id = args.get("source_id", "").strip()
        target_id = args.get("target_id", "").strip()
        relationship = args.get("relationship", "").strip()
        weight = float(args.get("weight", 0.5))
        if not (0.0 <= weight <= 1.0):
            return json.dumps({"error": "weight must be between 0.0 and 1.0"})
        if not all([source_id, target_id, relationship]):
            return json.dumps({
                "error": "source_id, target_id, and relationship are required",
            })
        if self._beam.episodic_graph is None:
            return json.dumps({"error": "Episodic graph not available"})
        edge = GraphEdge(
            source=source_id,
            target=target_id,
            edge_type=relationship,
            weight=weight,
            timestamp=datetime.now().isoformat(),
        )
        self._beam.episodic_graph.add_edge(edge)
        return json.dumps({
            "status": "linked",
            "source": source_id,
            "target": target_id,
            "relationship": relationship,
            "weight": weight,
        })

    # LOCAL PATCH: P24 capture the current speaker on every turn; never infer a
    # group-chat author from initialization-time user_id or agent_identity.
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._turn_count = turn_number
        author = kwargs.get("turn_author")
        if not isinstance(author, dict):
            author = {
                "id": kwargs.get("author_id"),
                "name": kwargs.get("author_name"),
                "is_bot": kwargs.get("author_is_bot"),
            }
        self._current_turn_author = self._normalize_turn_author(author)

    # LOCAL PATCH (P17): keep per-transcript counters separate from the stable memory namespace.
    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Track Hermes transcript rotation without changing the memory namespace."""
        self._current_session_id = str(new_session_id or "hermes_default")
        if reset or rewound:
            self._turn_count = 0
            with self._reflect_budget_lock:
                self._reflect_calls_this_session = 0

    # LOCAL PATCH (P17): compression context is bounded; the host swallows callback errors.
    # LOCAL PATCH: P24 API v2 writes the complete normalized evidence as an
    # idempotent durable archive. Old Hermes releases continue through the
    # bounded legacy excerpt/checkpoint path below.
    def on_pre_compress(
        self,
        messages: List[Dict[str, Any]],
        *,
        require_checkpoint: bool = False,
    ) -> str:
        """Return a bounded excerpt and preserve compression evidence.

        Hermes checkpoint API v2 always archives the full normalized evidence
        durably before returning. Older hosts retain the legacy behavior:
        snapshots are opt-in through ``memory.mnemosyne.require_checkpoint``,
        and those managers may swallow hook errors while compression proceeds.
        """
        if self.pre_compress_checkpoint_api_version >= 2:
            self._write_full_evidence_checkpoint(messages)
            return self._format_compression_excerpt(messages)

        required_value = self._read_config_key("require_checkpoint")
        required = (
            required_value
            if isinstance(required_value, bool)
            else str(required_value).strip().lower() in {"1", "true", "yes", "on"}
        )
        if not isinstance(messages, list):
            if required:
                raise CheckpointError("required pre-compression checkpoint received invalid messages")
            return ""

        selected = []
        total_bytes = 0
        overflow = False
        for item in messages[-self.COMPRESS_CHECKPOINT_MAX_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            if len(content) > self.COMPRESS_CHECKPOINT_MAX_BYTES:
                overflow = True
                break
            content = content.strip()
            if not content:
                continue
            try:
                content_bytes = len(content.encode("utf-8"))
            except UnicodeEncodeError:
                overflow = True
                break
            total_bytes += content_bytes
            if total_bytes > self.COMPRESS_CHECKPOINT_MAX_BYTES:
                overflow = True
                break
            selected.append({"role": role, "content": content})

        if required and (overflow or len(messages) > self.COMPRESS_CHECKPOINT_MAX_MESSAGES):
            raise CheckpointError("required pre-compression checkpoint exceeds configured bounds")
        if required and not selected:
            raise CheckpointError("required pre-compression checkpoint has no text messages")
        if required:
            self._write_compression_checkpoint(selected)
        if not selected:
            return ""

        excerpt = selected[-self.COMPRESS_CONTEXT_MAX_MESSAGES:]
        remaining = self.COMPRESS_CONTEXT_MAX_CHARS
        lines = ["Mnemosyne pre-compression excerpts (conversation text; preserve useful facts):"]
        for item in excerpt:
            text = item["content"][:remaining]
            if not text:
                break
            lines.append(f"[{item['role']}] {text}")
            remaining -= len(text)
        return "\n".join(lines)

    def _format_compression_excerpt(self, messages: Any) -> str:
        """Build the bounded summary-prompt excerpt from normalized evidence."""
        if not isinstance(messages, list):
            return ""
        excerpt: List[Dict[str, str]] = []
        for item in messages[-self.COMPRESS_CONTEXT_MAX_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("speaker") or "evidence").strip()[:40]
            content = item.get("content", item.get("text"))
            if not isinstance(content, str) or not content.strip():
                continue
            excerpt.append({"role": role, "content": content.strip()})
        remaining = self.COMPRESS_CONTEXT_MAX_CHARS
        lines = ["Mnemosyne pre-compression excerpts (conversation evidence; preserve useful facts):"]
        for item in excerpt:
            text = item["content"][:remaining]
            if not text:
                break
            lines.append(f"[{item['role']}] {text}")
            remaining -= len(text)
        return "\n".join(lines) if len(lines) > 1 else ""

    def _write_full_evidence_checkpoint(self, evidence: Any) -> Path:
        """Durably archive all normalized evidence, deduplicated by content hash."""
        if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
            raise CheckpointError("API-v2 checkpoint requires a list of normalized evidence records")
        home = str(getattr(self, "_hermes_home", "") or "").strip()
        session_id = str(getattr(self, "_current_session_id", "") or "").strip()
        if not home or not session_id:
            raise CheckpointError("API-v2 checkpoint lacks Hermes home or session")
        temp_path: Optional[str] = None
        try:
            normalized = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            evidence_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            directory = Path(home) / "mnemosyne" / "checkpoints"
            target = directory / f"{session_digest}-{evidence_digest}.json"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise OSError("checkpoint directory must not be a symlink")
            if os.name != "nt":
                os.chmod(directory, 0o700)
            payload = {
                "version": 2,
                "session_id_sha256": session_digest,
                "evidence_sha256": evidence_digest,
                "evidence_messages": evidence,
            }
            serialized = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise OSError("existing checkpoint must be a regular file")
                if target.read_bytes() == serialized:
                    if os.name != "nt":
                        dir_fd = os.open(directory, os.O_RDONLY)
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    return target
                raise OSError("existing checkpoint differs from normalized evidence")
            fd, temp_path = tempfile.mkstemp(prefix=f".{session_digest}.", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link publication is no-clobber and makes concurrent retries
            # idempotent; the temporary inode already contains fsynced bytes.
            try:
                os.link(temp_path, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or target.read_bytes() != serialized:
                    raise OSError("concurrent checkpoint differs from normalized evidence")
            os.unlink(temp_path)
            temp_path = None
            if os.name != "nt":
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            return target
        except Exception as exc:
            raise CheckpointError("API-v2 normalized evidence checkpoint could not be written") from exc
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _write_compression_checkpoint(self, messages: List[Dict[str, str]]) -> Path:
        """Atomically replace this Hermes session's bounded local checkpoint."""
        home = str(getattr(self, "_hermes_home", "") or "").strip()
        session_id = str(getattr(self, "_current_session_id", "") or "").strip()
        if not home or not session_id:
            raise CheckpointError("required pre-compression checkpoint lacks Hermes home or session")
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        directory = Path(home) / "mnemosyne" / "checkpoints"
        target = directory / f"{session_digest}.json"
        temp_path = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                if directory.is_symlink():
                    raise OSError("checkpoint directory must not be a symlink")
                os.chmod(directory, 0o700)
            payload = {
                "version": 1,
                "session_id_sha256": session_digest,
                "created_at": datetime.now().isoformat(),
                "messages": messages,
            }
            serialized = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(serialized) + 1 > self.COMPRESS_CHECKPOINT_MAX_BYTES:
                raise ValueError("checkpoint JSON exceeds the byte limit")
            fd, temp_path = tempfile.mkstemp(prefix=f".{session_digest}.", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(serialized + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, target)
            return target
        except Exception as exc:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise CheckpointError("required pre-compression checkpoint could not be written") from exc

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # LOCAL PATCH: P22 reuse any auto-sleep worker; never run duplicate
        # consolidation or charge a second reflection call for the same work.
        if not self._beam:
            return
        try:
            sleep_thread = self._start_consolidation("session_end")
            if sleep_thread is None:
                return
            timeout = self.SESSION_END_SLEEP_TIMEOUT_SECONDS
            sleep_thread.join(timeout=timeout)
            if sleep_thread.is_alive():
                logger.warning(
                    "Mnemosyne session-end sleep timed out after %ss — consolidation deferred",
                    timeout,
                )
        except Exception as e:
            logger.debug("Mnemosyne session-end sleep failed: %s", e)

    # LOCAL PATCH (P16): parent-side delegation capture remains opt-in via sync_roles.
    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        """Persist a bounded parent-side delegation record only when opted in."""
        if (
            not self._beam
            or "delegation" not in self._sync_roles
            or self._agent_context in self._skip_contexts
        ):
            return
        if not isinstance(task, str) or not isinstance(result, str):
            return
        if not task and not result:
            return
        task_header = "[DELEGATION TASK] "
        result_header = "\n[DELEGATION RESULT] "
        content_budget = self.DELEGATION_MAX_CHARS - len(task_header) - len(result_header)
        task_limit = content_budget // 2
        result_limit = content_budget - task_limit
        task_text = task[:task_limit].strip()
        result_text = result[:result_limit].strip()
        if not task_text and not result_text:
            return
        content = f"{task_header}{task_text}{result_header}{result_text}"
        if self._should_filter(content):
            return
        try:
            with self._ensure_beam_access_lock():
                self._beam.remember(
                    content=content,
                    source="conversation_delegation",
                    importance=0.2,
                    scope=self._default_scope,
                    metadata={"child_session_id": str(child_session_id or "")[:200]},
                )
        except Exception as exc:
            logger.debug("Mnemosyne delegation mirror failed: %s", self._sanitize_sync_turn_error(exc))

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror a built-in Hermes memory write into Mnemosyne.

        LOCAL PATCH (F2): `metadata` is accepted and passed to the engine's own
        `remember(metadata=...)` parameter. Hermes only sends metadata to
        providers whose signature accepts it (see
        MemoryManager._provider_memory_write_metadata_mode); upstream dropped it
        silently.
        """
        # LOCAL PATCH: P24 use previous_content or an explicit native entry id
        # to retire only a durable, provider-owned mirror association.
        if not self._beam or action not in ("add", "replace", "remove"):
            return
        if target not in {"memory", "user"}:
            logger.warning("Mnemosyne native-memory mirror ignored unknown target %r", target)
            return
        details = dict(metadata or {})
        native_entry_id = details.get("native_entry_id") or details.get("entry_id")
        native_entry_id = str(native_entry_id).strip()[:200] if native_entry_id else None
        hermes_session_id = str(
            details.get("session_id") or getattr(self, "_current_session_id", "") or ""
        ).strip()[:200]
        previous_content = details.get("previous_content")
        if action == "remove":
            with self._ensure_beam_access_lock():
                retired = self._retire_native_mirror(
                    target=target,
                    hermes_session_id=hermes_session_id,
                    native_entry_id=native_entry_id,
                    previous_content=previous_content if isinstance(previous_content, str) else None,
                )
            if not retired:
                logger.warning(
                    "Mnemosyne could not retire native %s mirror for target=%s session=%s: "
                    "no exact owned mapping was available",
                    action, target, hermes_session_id or "unknown",
                )
            return
        if not isinstance(content, str) or not content.strip():
            return
        recorded = False
        try:
            scope = "global" if target == "user" else "session"
            # LOCAL PATCH: P19 mirror writes cannot commit an in-flight turn's transaction.
            with self._ensure_beam_access_lock():
                mirror_metadata = dict(details)
                # LOCAL PATCH: P28 native-write metadata is supplied by the
                # Hermes callback and is not a verified source lookup.
                mirror_metadata = self._asserted_caller_metadata(mirror_metadata)
                mirror_metadata["hermes_native_mirror"] = {
                    "target": target,
                    "session_id": hermes_session_id,
                    "entry_id": native_entry_id,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
                # LOCAL PATCH: P24 keep native mirrors distinct from equal-text
                # user-authored engine rows; remember() dedupes by content.
                stored_content = self._native_mirror_content(target, content)
                memory_id = self._beam.remember(
                    content=stored_content,
                    source=f"builtin_memory_{target}",
                    importance=0.7 if target == "user" else 0.5,
                    scope=scope,
                    metadata=mirror_metadata,
                )
                recorded = self._record_native_mirror(
                    target=target,
                    hermes_session_id=hermes_session_id,
                    native_entry_id=native_entry_id,
                    content=content,
                    memory_id=memory_id,
                )
        except Exception as e:
            logger.debug("Mnemosyne mirror write failed: %s", e)
            return
        # LOCAL PATCH: P24 retain the previous evidence until the replacement
        # and its durable mapping both succeed. Identical-content replacements
        # are already represented by the existing engine row.
        if action == "replace" and recorded and (
            (isinstance(previous_content, str) and previous_content != content)
            or (previous_content is None and native_entry_id)
        ):
            with self._ensure_beam_access_lock():
                retired = self._retire_native_mirror(
                    target=target,
                    hermes_session_id=hermes_session_id,
                    native_entry_id=native_entry_id,
                    previous_content=(
                        previous_content if isinstance(previous_content, str) else None
                    ),
                    replacement_id=str(memory_id) if memory_id else None,
                    exclude_memory_id=(
                        str(memory_id)
                        if not isinstance(previous_content, str) and memory_id
                        else None
                    ),
                )
            if not retired:
                logger.warning(
                    "Mnemosyne replacement was added but prior native mirror could not be retired "
                    "for target=%s session=%s",
                    target, hermes_session_id or "unknown",
                )

    # LOCAL PATCH: P24 persist exact native-to-engine ownership in the same
    # SQLite database. User memories are global to the configured database;
    # session-scoped Hermes ids must not prevent their later retirement.
    def _native_mirror_connection(self):
        beam = getattr(self, "_beam", None)
        conn = getattr(beam, "conn", None)
        if conn is None:
            return None
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mnemosyne_hermes_native_mirror_mappings ("
            "mapping_id TEXT PRIMARY KEY, target TEXT NOT NULL, source TEXT NOT NULL, "
            "hermes_session_id TEXT NOT NULL, owner_id TEXT NOT NULL, native_entry_id TEXT, "
            "content_sha256 TEXT NOT NULL, engine_memory_id TEXT NOT NULL)"
        )
        return conn

    def _native_mirror_owner_id(self) -> str:
        """Return the canonical database and profile owner for native mirrors."""
        db_path = getattr(getattr(self, "_beam", None), "db_path", None)
        if not db_path:
            return ""
        path = os.path.realpath(os.path.abspath(os.path.expanduser(str(db_path))))
        profile_owner = self._canonical_owner()
        return json.dumps([os.path.normcase(path), profile_owner], separators=(",", ":"))

    @staticmethod
    def _native_mirror_content(target: str, content: str) -> str:
        marker = "[HERMES NATIVE USER]" if target == "user" else "[HERMES NATIVE MEMORY]"
        return f"{marker}\n{content}"

    def _record_native_mirror(
        self,
        *,
        target: str,
        hermes_session_id: str,
        native_entry_id: Optional[str],
        content: str,
        memory_id: Any,
    ) -> bool:
        if not memory_id:
            logger.warning("Mnemosyne native mirror returned no durable memory id for target=%s", target)
            return False
        try:
            conn = self._native_mirror_connection()
            if conn is None:
                logger.warning("Mnemosyne native mirror has no durable mapping connection")
                return False
            owner_id = self._native_mirror_owner_id()
            if not owner_id:
                logger.warning("Mnemosyne native mirror has no canonical database owner")
                return False
            source = f"builtin_memory_{target}"
            content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            association = json.dumps(
                [target, source, owner_id, hermes_session_id, native_entry_id, content_digest, str(memory_id)],
                ensure_ascii=False, separators=(",", ":"),
            )
            mapping_id = hashlib.sha256(association.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO mnemosyne_hermes_native_mirror_mappings "
                "(mapping_id, target, source, hermes_session_id, owner_id, native_entry_id, content_sha256, engine_memory_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mapping_id, target, source, hermes_session_id, owner_id,
                    native_entry_id, content_digest, str(memory_id),
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.warning("Mnemosyne native mirror mapping could not be persisted: %s", type(exc).__name__)
            return False

    def _retire_native_mirror(
        self,
        *,
        target: str,
        hermes_session_id: str,
        native_entry_id: Optional[str],
        previous_content: Optional[str],
        replacement_id: Optional[str] = None,
        exclude_memory_id: Optional[str] = None,
    ) -> bool:
        try:
            conn = self._native_mirror_connection()
            if conn is None:
                return False
            source = f"builtin_memory_{target}"
            owner_id = self._native_mirror_owner_id()
            if not owner_id:
                return False
            if previous_content is not None:
                digest = hashlib.sha256(previous_content.encode("utf-8")).hexdigest()
                if target == "user":
                    identity_clause = " AND native_entry_id=?" if native_entry_id else " AND native_entry_id IS NULL"
                    params = [target, source, owner_id, digest]
                    if native_entry_id:
                        params.append(native_entry_id)
                    rows = conn.execute(
                        "SELECT mapping_id, engine_memory_id FROM mnemosyne_hermes_native_mirror_mappings "
                        "WHERE target=? AND source=? AND owner_id=? "
                        "AND content_sha256=?" + identity_clause,
                        params,
                    ).fetchall()
                elif hermes_session_id:
                    identity_clause = " AND native_entry_id=?" if native_entry_id else " AND native_entry_id IS NULL"
                    params = [target, source, owner_id, hermes_session_id, digest]
                    if native_entry_id:
                        params.append(native_entry_id)
                    rows = conn.execute(
                        "SELECT mapping_id, engine_memory_id FROM mnemosyne_hermes_native_mirror_mappings "
                        "WHERE target=? AND source=? AND owner_id=? "
                        "AND hermes_session_id=? AND content_sha256=?" + identity_clause,
                        params,
                    ).fetchall()
                else:
                    return False
            elif native_entry_id and target == "user":
                exclude_clause = " AND engine_memory_id<>?" if exclude_memory_id else ""
                rows = conn.execute(
                    "SELECT mapping_id, engine_memory_id FROM mnemosyne_hermes_native_mirror_mappings "
                    "WHERE target=? AND source=? AND owner_id=? AND native_entry_id=?"
                    + exclude_clause,
                    (target, source, owner_id, native_entry_id)
                    + ((exclude_memory_id,) if exclude_memory_id else ()),
                ).fetchall()
            elif native_entry_id and hermes_session_id:
                exclude_clause = " AND engine_memory_id<>?" if exclude_memory_id else ""
                rows = conn.execute(
                    "SELECT mapping_id, engine_memory_id FROM mnemosyne_hermes_native_mirror_mappings "
                    "WHERE target=? AND source=? AND owner_id=? "
                    "AND hermes_session_id=? AND native_entry_id=?"
                    + exclude_clause,
                    (target, source, owner_id, hermes_session_id, native_entry_id)
                    + ((exclude_memory_id,) if exclude_memory_id else ()),
                ).fetchall()
            else:
                return False
            if not rows:
                return False
            # LOCAL PATCH: P24 invalidate exact mapped ids. The engine applies
            # its visibility rules and expiry semantics to working and episodic
            # rows, including global rows from an earlier provider session.
            removed = False
            selected_by_memory = {}
            for mapping_id, memory_id in rows:
                selected_by_memory.setdefault(str(memory_id), []).append(str(mapping_id))
            for memory_id, mapping_ids in selected_by_memory.items():
                placeholders = ",".join("?" for _ in mapping_ids)
                other_associations = conn.execute(
                    "SELECT COUNT(*) FROM mnemosyne_hermes_native_mirror_mappings "
                    "WHERE target=? AND source=? AND owner_id=? AND engine_memory_id=? "
                    f"AND mapping_id NOT IN ({placeholders})",
                    (target, source, owner_id, memory_id, *mapping_ids),
                ).fetchone()[0]
                if other_associations:
                    conn.executemany(
                        "DELETE FROM mnemosyne_hermes_native_mirror_mappings WHERE mapping_id=?",
                        ((mapping_id,) for mapping_id in mapping_ids),
                    )
                    removed = True
                    continue
                invalidate = getattr(self._beam, "invalidate", None)
                if not callable(invalidate):
                    logger.warning("Mnemosyne engine has no exact-id invalidation API")
                    continue
                if invalidate(memory_id, replacement_id=replacement_id):
                    removed = True
                    conn.executemany(
                        "DELETE FROM mnemosyne_hermes_native_mirror_mappings WHERE mapping_id=?",
                        ((mapping_id,) for mapping_id in mapping_ids),
                    )
            conn.commit()
            return removed
        except Exception as exc:
            logger.warning("Mnemosyne native mirror retirement failed: %s", type(exc).__name__)
            return False

    # Bounded drain for either consolidation trigger. If it overruns, the worker
    # unregisters the host backend after finishing, preserving LLM inheritance.
    SHUTDOWN_DRAIN_TIMEOUT_SECONDS = _parse_env_float("MNEMOSYNE_SHUTDOWN_DRAIN_TIMEOUT", 2)

    def _unregister_host_llm(self) -> None:
        # LOCAL PATCH: P22 only clear this instance's host backend registration.
        try:
            from hermes_memory_provider.hermes_llm_adapter import unregister_hermes_host_llm
            from mnemosyne.core.llm_backends import get_host_llm_backend
            with _host_llm_registration_lock:
                owned_backend = getattr(self, "_host_llm_backend", None)
                if owned_backend is not None and get_host_llm_backend() is owned_backend:
                    unregister_hermes_host_llm()
                self._host_llm_backend = None
        except Exception as exc:
            logger.debug("Mnemosyne could not unregister Hermes auxiliary LLM backend: %s", exc)

    def shutdown(self) -> None:
        # LOCAL PATCH: P22 stop admission before draining BOTH background triggers.
        with self._ensure_consolidation_lock():
            self._consolidation_stopping = True
            self._consolidation_status = {**self._consolidation_status, "stopping": True}
            thread = self._consolidation_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.SHUTDOWN_DRAIN_TIMEOUT_SECONDS)
        with self._consolidation_lock:
            if self._consolidation_running:
                self._consolidation_cleanup_pending = True
                # LOCAL PATCH: P29 report drain timeout without claiming the worker failed.
                self._consolidation_status = {**self._consolidation_status,
                                              "shutdown_timed_out": True}
                logger.debug("Mnemosyne shutdown: host backend cleanup deferred to consolidation worker")
            else:
                self._unregister_host_llm()
                self._consolidation_thread = None
                self._consolidation_status = {**self._consolidation_status,
                                              "stopping": True}
        with self._ensure_beam_access_lock():
            if self._memory is not None:
                try:
                    self._memory.close()
                except Exception:
                    logger.debug("Mnemosyne: could not close wrapper", exc_info=True)
            # LOCAL PATCH: P26 close admission under the same foreground lock
            # as memory operations, retaining the object for closed diagnostics.
            self._close_audit_log(reset=False)
            self._memory = None
            self._beam = None

        # C13: decrement this instance's contribution to the module-level
        # active-provider count. ``_provider_active`` stays True if other
        # provider instances are still active in the process (codex
        # review #3 -- a single shared bool can't represent multi-
        # instance lifecycle).
        self._deactivate_in_module()


# ---------------------------------------------------------------------------
# Plugin registration (used when loaded via plugins.memory discovery)
# ---------------------------------------------------------------------------

def register_memory_provider(ctx):
    """Called by Hermes memory provider discovery system."""
    provider = MnemosyneMemoryProvider()
    ctx.register_memory_provider(provider)


# ---------------------------------------------------------------------------
# Plugin registration (used when loaded via Hermes plugin system)
# ---------------------------------------------------------------------------

def register(ctx):
    """Called by Hermes plugin loader to register CLI commands and tools."""
    from .cli import register_cli, mnemosyne_command
    ctx.register_cli_command(
        name="mnemosyne",
        help="Manage Mnemosyne local memory",
        description="Inspect, consolidate, and manage Mnemosyne native memory.",
        setup_fn=register_cli,
        handler_fn=mnemosyne_command,
    )

    # LOCAL PATCH: hand the provider to the loader explicitly. Upstream only
    # registered the CLI command here, so `plugins.memory.load_memory_provider()`
    # captured nothing from register() and reached the provider only through its
    # fallback scan for a MemoryProvider subclass. One provider, one registration
    # path. Raise (don't swallow) on failure: the loader logs it and still falls
    # back to the class scan.
    ctx.register_memory_provider(MnemosyneMemoryProvider())

    # Also register tools and hooks from hermes_plugin (sibling directory).
    # This way a single symlink to hermes_memory_provider/ gives us the
    # full Mnemosyne experience: CLI + tools + hooks.
    try:
        # T7: only add the repo sibling dir when it really holds hermes_plugin.
        # A copied install has no such sibling; inserting the plugins dir would
        # also shadow the engine (see the module-top guard). The import below
        # still works when hermes_plugin is installed as a package.
        _repo_root = Path(__file__).resolve().parent.parent
        if (_repo_root / "hermes_plugin").is_dir() and str(_repo_root) not in sys.path:
            sys.path.insert(0, str(_repo_root))
        from hermes_plugin import register as _plugin_register
        _plugin_register(ctx)
    except Exception:
        pass  # Graceful degradation — CLI still works without plugin tools
