"""Static description of the lite MCP tools and their accepted aliases."""

from copy import deepcopy

from .records import ToolSpec

_NAMESPACE = {"type": "string", "minLength": 1}
_SEARCH = {
    "query": {"type": "string", "minLength": 1},
    "namespace": _NAMESPACE,
    "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
    "min_importance": {"type": "integer", "minimum": 0, "maximum": 10},
    "max_chars": {"type": "integer", "minimum": 50, "maximum": 2000},
}

TOOLS = (
    ToolSpec(
        "mnemosyne_memory_search",
        "Search local memory by ranked full-text (FTS5/BM25). Returns capped cited cards.",
        _SEARCH,
        ("query",),
    ),
    ToolSpec(
        "mnemosyne_memory_remember",
        "Store a local memory without enrichment",
        {
            "content": {"type": "string", "minLength": 1},
            "namespace": _NAMESPACE,
            "importance": {"type": "integer", "minimum": 0, "maximum": 10},
            "context": {"type": "string"},
        },
        ("content",),
    ),
    ToolSpec(
        "mnemosyne_prefetch",
        "Recall context for a conversation. Same ranked search plus card text for injection.",
        _SEARCH,
        ("query",),
    ),
    ToolSpec(
        "mnemosyne_sync_turn",
        "Capture user-authored text from a completed turn",
        {
            "user_text": {"type": "string"},
            "assistant_text": {"type": "string"},
            "namespace": _NAMESPACE,
            "session_id": {"type": "string"},
            "execution_context": {"type": "string"},
            "speaker": {"type": "string"},
            "policy_owner": {"type": "string"},
        },
        ("user_text",),
    ),
)
TOOL_BY_NAME = {tool.name: tool for tool in TOOLS}
ALIASES = {
    "mnemosyne_remember": "mnemosyne_memory_remember",
    "mnemosyne_recall": "mnemosyne_memory_search",
}


def schemas():
    """Return caller-owned schema objects while keeping definitions centralized."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": deepcopy(tool.properties),
                "required": list(tool.required),
                "additionalProperties": False,
            },
        }
        for tool in TOOLS
    ]
