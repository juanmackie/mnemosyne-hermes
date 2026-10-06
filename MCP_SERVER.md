# Mnemosyne MCP Server

The lite surface ships an MCP stdio server. It speaks newline-delimited
JSON-RPC 2.0 on stdin/stdout, is backed by the lite SQLite store, and needs no
API key or network access.

This is **not** the Hermes provider. The provider is a Hermes plugin (see
[integrations/hermes-provider/README.md](integrations/hermes-provider/README.md))
and does not use this server or its tools.

## Running it

```bash
mnemosyne-lite init        # once: create the store
mnemosyne-lite mcp         # serve it (alias: mnemosyne-lite serve)
```

The store is resolved the same way as every other lite command: `--db-path` >
`MNEMOSYNE_DB_PATH` > `DATABASE_URL` (only the `sqlite`, `sqlite3` and `file`
schemes are accepted; anything else is rejected with an error) > the default
`~/.mnemosyne-lite/mnemosyne.db`.

The server opens the store **before** the request loop, so a missing or foreign
database fails at startup instead of answering every request with an error. It
never creates a store: run `mnemosyne-lite init` first.

## Protocol

One JSON object per line on stdin, one per line on stdout. Nothing else is
written to stdout, so the process is safe to attach to a stdio client. Requests
without an `id` are notifications and get no response.

### initialize

```json
{"jsonrpc":"2.0","method":"initialize","id":1}
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "mnemosyne-lite", "version": "<package __version__>"}
  }
}
```

`protocolVersion` echoes the client's value when it is one of `2024-11-05`,
`2025-03-26` or `2025-06-18`; anything else (or nothing) gets `2025-06-18`.
`serverInfo.version` is the installed package's `mnemosyne_lite.__version__` —
the same string `mnemosyne-lite --version` prints, so the sample above shows a
placeholder rather than a number that goes stale.

### ping

Returns `{"jsonrpc": "2.0", "id": <id>, "result": {}}`.

### tools/list

Returns every lite tool as `{"name", "description", "inputSchema"}`, where
`inputSchema` is a JSON Schema object with `additionalProperties: false`.
Each description carries a one-line store summary (memory count and
namespaces); run `mnemosyne-lite describe` for the full summary.

### tools/call

```json
{"jsonrpc":"2.0","method":"tools/call",
 "params":{"name":"mnemosyne_memory_search","arguments":{"query":"storage"}},"id":3}
```

A successful call returns the payload three times over: `structuredContent`
(the raw dict), `content[0].text` (the same dict as a JSON string) and
`isError: false`.

## Tools

All four tools are listed by `tools/list`. Their schemas set
`additionalProperties: false`, so an unknown argument is an error rather than a
silently ignored field.

### `mnemosyne_memory_search`

Arguments: `query` (required), `namespace`, `max_results` (1-100, default 10),
`min_importance` (0-10), `max_chars` (50-2000, default 500).

Ranked full-text search (FTS5/BM25). Returns the ranked `results` plus card
`text` with a `shown N of M` header, `shown`/`total` counts, and per-card
`match:` snippets. Truncation is marked with `…`.

### `mnemosyne_memory_remember`

Arguments: `content` (required), `namespace`, `importance` (0-10, default 5),
`context`.

Stores a memory. No enrichment, no LLM.

### `mnemosyne_prefetch`

Arguments: same as `mnemosyne_memory_search`.

Recall for a conversation. It returns the same ranked cards plus a `text`
field for injection. When the ranked search finds nothing it retries on the
4+ letter words of the query and returns the top results by importance and
recency.

### `mnemosyne_sync_turn`

Arguments: `user_text` (required), `assistant_text`, `namespace`, `session_id`,
`execution_context`, `speaker`, `policy_owner`.

Captures user-authored text from a completed turn. It returns
`{"ok": true, "synced": false, "status": "skipped"}` instead of writing when
`execution_context` is `cron`, `flush`, `subagent`, `background` or
`skill_loop`, when `speaker` is not `user`, when `policy_owner` is neither `""`
nor `mnemosyne`, or when `user_text` is blank. Otherwise it stores the text and
returns `status: "captured"` with `source_memory_id`.

Namespaces default to `default`. `agent:hermes` is accepted but is the Hermes
provider's namespace, not this server's default.

Aliases: the tool name has its first `.` rewritten to `_`, so `mnemosyne.recall`
maps to `mnemosyne_memory_search` and `mnemosyne.remember` maps to
`mnemosyne_memory_remember`. Any other name is an error.

## Errors

| Situation | Response |
| --- | --- |
| Unparsable line | JSON-RPC error `-32700` |
| Not a JSON-RPC 2.0 request, or `params` is not an object | error `-32600` |
| Unknown method | error `-32601` |
| Tool fails | `result` with `isError: true` |

A failing tool call is deliberately **not** a JSON-RPC error: the call itself
succeeded, the tool did not, and the reason is in `content[0].text`. Strict
argument validation means an unknown argument, a missing required argument or a
wrongly typed value all surface this way.

## Client entry

```json
{
  "mcpServers": {
    "mnemosyne-lite": {
      "command": "mnemosyne-lite",
      "args": ["mcp"],
      "env": {
        "MNEMOSYNE_DB_PATH": "/home/you/.mnemosyne-lite/mnemosyne.db"
      }
    }
  }
}
```

Use an absolute `command` path if the client does not inherit your `PATH`. The
same shape works in Hermes, Claude Code, Cursor, Windsurf and OpenClaw; see
[docs/MCP_CLIENT_CONFIGS.md](docs/MCP_CLIENT_CONFIGS.md) for per-client files.

## Manual test

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","method":"initialize","id":1}' \
  '{"jsonrpc":"2.0","method":"tools/list","id":2}' \
  '{"jsonrpc":"2.0","method":"tools/call",'\
  '"params":{"name":"mnemosyne_memory_search",'\
  '"arguments":{"query":"storage"}},"id":3}' \
  | mnemosyne-lite mcp
```

Three response lines, one per request. The test suite that keeps this contract
honest runs with `./test-all.sh`.
