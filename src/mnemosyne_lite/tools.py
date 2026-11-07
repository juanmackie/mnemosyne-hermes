"""Tools exposed by the lite MCP server.

These are the *lite* tools, backed by the lite SQLite store. The Hermes
provider does not use them: it is engine-backed and exposes the engine's own
`mnemosyne_memory_*` tools (see integrations/hermes-provider/).
"""

from .plan import fallback_terms
from .storage import PythonMemoryStorage
from .tooltable import ALIASES, TOOL_BY_NAME, schemas

SKIP_CONTEXTS = {"cron", "flush", "subagent", "background", "skill_loop"}


def tool_schemas():
    return schemas()


def _validate(name, arguments):
    spec = TOOL_BY_NAME[name]
    for key in spec.required:
        if key not in arguments:
            raise ValueError(f"Missing argument: {key}")
    for key, value in arguments.items():
        rule = spec.properties.get(key)
        if rule is None:
            raise ValueError(f"Unknown argument: {key}")
        if rule["type"] == "string":
            if not isinstance(value, str) or (rule.get("minLength") and not value.strip()):
                raise ValueError(
                    f"{key} must be a nonempty string"
                    if rule.get("minLength")
                    else f"{key} must be a string"
                )
        elif type(value) is not int or not rule["minimum"] <= value <= rule["maximum"]:
            raise ValueError(f"Invalid {key}")


def _remember(storage, args, namespace):
    result = storage.remember(
        args["content"], namespace, args.get("importance", 5), context=args.get("context")
    )
    return {"ok": True, "results": [result], "count": 1, "namespace": namespace}


def _sync_turn(storage, args, namespace):
    text = args["user_text"]
    if (
        args.get("execution_context") in SKIP_CONTEXTS
        or args.get("speaker", "user") != "user"
        # "mnemosyne-rust" was the retired Rust policy owner; accepting it
        # would let a client that no longer exists capture turns here.
        or args.get("policy_owner", "mnemosyne") not in ("", "mnemosyne")
        or not text.strip()
    ):
        return {"ok": True, "synced": False, "status": "skipped"}
    result = storage.remember(text, namespace, 5, context="sync_turn")
    return {"ok": True, "synced": True, "status": "captured", "source_memory_id": result["id"]}


def _search(storage, args, namespace, *, prefetch):
    results = storage.recall(
        args["query"],
        namespace=namespace,
        max_results=args.get("max_results", 10),
        min_importance=args.get("min_importance"),
    )
    # ponytail: bounded keyword fallback for conversational prefetch; use ranked
    # semantic retrieval only when measured recall quality justifies it.
    used_fallback = False
    if not results and prefetch:
        seen = set()
        for word in fallback_terms(args["query"]):
            for row in storage.recall(
                word,
                namespace=namespace,
                max_results=args.get("max_results", 10),
                min_importance=args.get("min_importance"),
            ):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    results.append(row)
        results = sorted(results, key=lambda r: (r["importance"], r["created_at"]), reverse=True)[
            : args.get("max_results", 10)
        ]
        used_fallback = True
    from .cards import DEFAULT_CARD_CHARS, render_recall_cards

    max_chars = args.get("max_chars", DEFAULT_CARD_CHARS)
    total = (
        len(results)
        if used_fallback
        else storage.count_matching(
            args["query"],
            namespace=namespace,
            min_importance=args.get("min_importance"),
        )
    )
    indexed = storage.count()
    cards = render_recall_cards(
        results,
        args["query"],
        total,
        indexed,
        max_chars,
        total_is_bounded=used_fallback,
    )
    result = {
        "ok": True,
        "results": results,
        "count": len(results),
        "shown": len(results),
        "total": total,
        "namespace": namespace,
        "text": cards,
    }
    if used_fallback:
        result["total_is_bounded"] = True
    return result


def call_tool(storage: PythonMemoryStorage, name, arguments, namespace="default"):
    name = name.replace("mnemosyne.", "mnemosyne_", 1)
    name = ALIASES.get(name, name)
    if name not in TOOL_BY_NAME:
        raise ValueError(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    _validate(name, arguments)
    namespace = arguments.get("namespace", namespace)
    if name == "mnemosyne_memory_remember":
        return _remember(storage, arguments, namespace)
    if name == "mnemosyne_sync_turn":
        return _sync_turn(storage, arguments, namespace)
    return _search(storage, arguments, namespace, prefetch=name == "mnemosyne_prefetch")
