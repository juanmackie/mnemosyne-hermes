"""Shared local-memory tools for MCP and the Hermes provider."""
from lib.storage import PythonMemoryStorage

SKIP_CONTEXTS = {"cron", "flush", "subagent", "background", "skill_loop"}


def tool_schemas():
    namespace = {"type": "string", "minLength": 1}
    search = {"query": {"type": "string", "minLength": 1}, "namespace": namespace,
              "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
              "min_importance": {"type": "integer", "minimum": 0, "maximum": 10}}
    definitions = [
        ("mnemosyne_memory_search", "Search local memory by literal substring", search, ["query"]),
        ("mnemosyne_memory_remember", "Store a local memory without enrichment",
         {"content": {"type": "string", "minLength": 1}, "namespace": namespace,
          "importance": {"type": "integer", "minimum": 0, "maximum": 10},
          "context": {"type": "string"}}, ["content"]),
        ("mnemosyne_prefetch", "Recall context for a conversation", search, ["query"]),
        ("mnemosyne_sync_turn", "Capture user-authored text from a completed turn",
         {"user_text": {"type": "string"}, "assistant_text": {"type": "string"},
          "namespace": namespace, "session_id": {"type": "string"},
          "execution_context": {"type": "string"}, "speaker": {"type": "string"},
          "policy_owner": {"type": "string"}}, ["user_text"]),
    ]
    return [{"name": name, "description": description,
             "parameters": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False}}
            for name, description, properties, required in definitions]


def call_tool(storage: PythonMemoryStorage, name, arguments, namespace="agent:hermes"):
    name = name.replace("mnemosyne.", "mnemosyne_", 1)
    name = {"mnemosyne_remember": "mnemosyne_memory_remember",
            "mnemosyne_recall": "mnemosyne_memory_search"}.get(name, name)
    schemas = {schema["name"]: schema["parameters"] for schema in tool_schemas()}
    if name not in schemas:
        raise ValueError(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    schema = schemas[name]
    for key in schema["required"]:
        if key not in arguments:
            raise ValueError(f"Missing argument: {key}")
    for key, value in arguments.items():
        rule = schema["properties"].get(key)
        if rule is None:
            raise ValueError(f"Unknown argument: {key}")
        if rule["type"] == "string":
            if not isinstance(value, str) or (rule.get("minLength") and not value.strip()):
                raise ValueError(f"{key} must be a nonempty string" if rule.get("minLength") else f"{key} must be a string")
        elif type(value) is not int or not rule["minimum"] <= value <= rule["maximum"]:
            raise ValueError(f"Invalid {key}")
    namespace = arguments.get("namespace", namespace)
    if name == "mnemosyne_memory_remember":
        result = storage.remember(arguments["content"], namespace, arguments.get("importance", 5),
                                  context=arguments.get("context"))
        return {"ok": True, "results": [result], "count": 1, "namespace": namespace}
    if name == "mnemosyne_sync_turn":
        text = arguments["user_text"]
        if (arguments.get("execution_context") in SKIP_CONTEXTS
                or arguments.get("speaker", "user") != "user"
                or arguments.get("policy_owner", "mnemosyne") not in ("", "mnemosyne", "mnemosyne-rust")
                or not text.strip()):
            return {"ok": True, "synced": False, "status": "skipped"}
        result = storage.remember(text, namespace, 5, context="sync_turn")
        return {"ok": True, "synced": True, "status": "captured", "source_memory_id": result["id"]}
    results = storage.recall(arguments["query"], namespace=namespace,
                             max_results=arguments.get("max_results", 10),
                             min_importance=arguments.get("min_importance"))
    # ponytail: bounded keyword fallback for conversational prefetch; use ranked
    # semantic retrieval only when measured recall quality justifies it.
    if not results and name == "mnemosyne_prefetch":
        import re
        seen = set()
        for word in list(dict.fromkeys(re.findall(r"\w{4,}", arguments["query"].lower())))[:8]:
            for row in storage.recall(word, namespace=namespace,
                                      max_results=arguments.get("max_results", 10),
                                      min_importance=arguments.get("min_importance")):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    results.append(row)
        results = sorted(results, key=lambda r: (r["importance"], r["created_at"]), reverse=True)[:arguments.get("max_results", 10)]
    result = {"ok": True, "results": results, "count": len(results), "namespace": namespace}
    if name == "mnemosyne_prefetch":
        result["text"] = "\n".join("- " + row["content"][:2000] for row in results)
    return result
