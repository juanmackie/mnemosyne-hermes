# Mnemosyne MCP Server

## Overview

The Mnemosyne MCP (Model Context Protocol) server provides a JSON-RPC 2.0 interface over stdio for Claude Code integration. It exposes memory tools organized around the OODA loop, including hierarchical retrieval and bounded result surfaces.

## Running the Server

```bash
# Start MCP server (stdio mode for Hermes/Claude Code)
mnemosyne mcp

# With custom DB path
MNEMOSYNE_DB_PATH=~/.hermes/mnemosyne/mnemosyne.db mnemosyne mcp
```

## Protocol

### JSON-RPC 2.0

All communication uses JSON-RPC 2.0 over stdin/stdout:
- **Requests**: JSON objects on stdin, one per line
- **Responses**: JSON objects on stdout, one per line
- **Logs**: Sent to stderr (not stdout)

### Initialize

Before using the server, send an initialize request:

**Request:**
```json
{"jsonrpc":"2.0","method":"initialize","id":1}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "protocolVersion": "2024-11-05",
    "serverInfo": {
      "name": "mnemosyne",
      "version": "0.1.0"
    },
    "capabilities": {
      "tools": {}
    }
  },
  "id": 1
}
```

## Tools

### List Available Tools

**Request:**
```json
{"jsonrpc":"2.0","method":"tools/list","id":2}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "tools": [
      {
        "name": "mnemosyne.recall",
        "description": "Search memories by semantic query, keywords, or tags...",
        "input_schema": { ... }
      },
      ...
    ]
  },
  "id": 2
}
```

### Memory Tools (MCP)

The adapter exposes these tool schemas (verified):

- `mnemosyne_memory_search` — Search memories by keyword/namespace
- `mnemosyne_memory_remember` — Store a memory (keyless, no API key required)
- `mnemosyne_prefetch` — Prefetch memories for session
- `mnemosyne_sync_turn` — Record turn to memory (skips cron/flush/subagent/background/skill_loop contexts)

All tools use direct SQLite access (`PythonMemoryStorage`). No subprocess overhead. No LLM required for core operations.

#### Search

**Request:**
```json
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"mnemosyne_memory_search","arguments":{"query":"test","namespace":"agent:hermes","max_results":10},"id":3}
```

**Response:**
```json
{"jsonrpc":"2.0","result":{"ok":true,"results":[{"id":"...","content":"test memory","namespace":"agent:hermes","importance":5}],"count":1,"namespace":"agent:hermes"},"id":3}
```

#### Remember

**Request:**
```json
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"mnemosyne_memory_remember","arguments":{"content":"test memory","namespace":"agent:hermes","importance":5},"id":4}
```

**Response:**
```json
{"jsonrpc":"2.0","result":{"ok":true,"results":[{"content":"test memory","namespace":"agent:hermes"}],"count":1,"namespace":"agent:hermes"},"id":4}
```

#### ORIENT Tools

##### 3. mnemosyne.graph
Get memory graph from seed IDs.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "mnemosyne.graph",
    "arguments": {
      "seed_ids": ["uuid-1", "uuid-2"],
      "max_hops": 2
    }
  },
  "id": 5
}
```

**Status:** ✅ **Implemented** - Uses bounded storage-backend graph traversal. `max_hops` is capped at 8 and `max_results` defaults to 100 (maximum 1000); truncated responses are marked explicitly.

##### 4. mnemosyne.context
Get full context for memory IDs.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "mnemosyne.context",
    "arguments": {
      "memory_ids": ["uuid-1", "uuid-2"],
      "include_links": true
    }
  },
  "id": 6
}
```

**Status:** ✅ **Implemented** - Fetches memories from storage and optionally expands links. `max_results` defaults to 100 (maximum 1000), and oversized input/link expansions are rejected or marked as truncated.

#### DECIDE Tools

##### 5. mnemosyne.remember
Store new memory with LLM enrichment.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "mnemosyne.remember",
    "arguments": {
      "content": "Decided to use PostgreSQL for user database because...",
      "namespace": "project:myapp",
      "importance": 9,
      "context": "Database selection discussion"
    }
  },
  "id": 7
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"memory_id\": \"uuid\", \"summary\": \"...\", \"importance\": 9, \"tags\": [...]}"
      }
    ]
  },
  "id": 7
}
```

**Status:** ✅ **Implemented** - Uses LLM service for enrichment
**Requires:** ANTHROPIC_API_KEY

##### 6. mnemosyne.consolidate
Merge/supersede similar memories.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "mnemosyne.consolidate",
    "arguments": {
      "memory_ids": ["uuid-1", "uuid-2"],
      "namespace": "project:myapp"
    }
  },
  "id": 8
}
```

**Status:** Phase 5 - Currently returns placeholder

#### ACT Tools

##### 7. mnemosyne.update
Update existing memory.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "mnemosyne.update",
    "arguments": {
      "memory_id": "uuid",
      "content": "Updated content",
      "importance": 10,
      "add_tags": ["critical", "reviewed"]
    }
  },
  "id": 9
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"memory_id\": \"uuid\", \"updated\": true}"
      }
    ]
  },
  "id": 9
}
```

**Status:** ✅ **Implemented** - Updates via storage backend

##### 8. mnemosyne.delete
Archive (soft delete) memory.

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "mnemosyne.delete",
    "arguments": {
      "memory_id": "uuid"
    }
  },
  "id": 10
}
```

**Status:** ✅ **Implemented** - Archives via storage backend

## Error Handling

### JSON-RPC Errors

The server returns standard JSON-RPC 2.0 errors:

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32601,
    "message": "Method not found: invalid_method"
  },
  "id": 1
}
```

**Standard Error Codes:**
- `-32700`: Parse error
- `-32600`: Invalid request
- `-32601`: Method not found
- `-32602`: Invalid params
- `-32603`: Internal error
- `-32000`: Application error (tool execution failed)

## Configuration

### API Key Setup

Core memory operations work without any API key (keyless by design). Optional LLM enrichment uses the active Hermes model from `$HERMES_HOME/config.yaml` (no second API key required).

```bash
# Verify keyless operation
unset ANTHROPIC_API_KEY OPENAI_API_KEY
mnemosyne remember --content "test" --namespace agent:hermes --no-enrich
```

**Note:** The server starts without an API key. LLM-dependent tools (optional enrichment) return gracefully when no backend is configured.

## Testing

### Manual Testing

```bash
# Test initialize
echo '{"jsonrpc":"2.0","method":"initialize","id":1}' | mnemosyne mcp

# Test list tools
echo '{"jsonrpc":"2.0","method":"tools/list","id":2}' | mnemosyne mcp

# Test recall
echo '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"mnemosyne_memory_search","arguments":{"query":"test"}},"id":3}' | mnemosyne mcp
```

### Test Scripts

```bash
# Run adapter contract tests
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v

# Skip LLM-dependent tests
./test-all.sh --skip-llm
```

## Implementation Status

| Tool | Status | Phase | Notes |
|------|--------|-------|-------|
| mnemosyne.recall | ✅ Complete | Current | Hybrid factual search, independently returned anchored response guidance, optional abstention, token/degradation metadata |
| mnemosyne.list | ✅ Complete | Current | Namespace-based listing with offset pagination |
| mnemosyne.graph | ✅ Complete | Phase 4 | Storage backend integration |
| mnemosyne.context | ✅ Complete | Phase 4 | Memory retrieval |
| mnemosyne.remember | ✅ Complete | Phase 4 | LLM enrichment working |
| mnemosyne.consolidate | ⏳ Pending | Phase 5 | LLM-guided consolidation |
| mnemosyne.update | ✅ Complete | Phase 4 | Storage backend integration |
| mnemosyne.delete | ✅ Complete | Phase 4 | Soft delete (archive) |

## Architecture

```mermaid
flowchart TD
    User([User/Agent])

    subgraph Claude["Claude Code"]
        UI[Slash Commands]
        Client[MCP Client]
    end

    Protocol{{JSON-RPC 2.0<br/>over stdio}}

    subgraph Server["Mnemosyne MCP Server"]
        Handler[Protocol Handler]
        Router[Tool Router<br/>10 Memory Tools]

        subgraph Services["Core Services"]
            Storage[(Storage<br/>LibSQL + FTS5 + vectors)]
            LLM[LLM Service<br/>Claude Haiku]
            Config[Config Manager<br/>OS Keychain]
            NS[Namespace<br/>Git-aware]
        end
    end

    API[/Anthropic API\]
    DB[(LibSQL<br/>FTS5 + Graph + vectors)]

    User --> UI
    UI --> Client
    Client <--> Protocol
    Protocol <--> Handler
    Handler --> Router

    Router --> Storage
    Router --> LLM
    Router --> Config
    Router --> NS

    Storage <--> DB
    LLM --> API
    NS --> DB
```

**Communication**: JSON-RPC 2.0 over stdin/stdout for seamless integration with Claude Code.

### Recall channels

`mnemosyne.recall` keeps `results` factual and returns explicit response
policies separately in `response_guidance`. The `channels` object reports the
factual/guidance quotas and independent abstention reasons. If
`budget_tokens` is supplied, `token_ledger` reports the existing context
assembler's budget accounting. Interaction policies are global, anchored,
evidence-backed guidance for response style only; they must not be quoted as
facts about the user.

**OODA-Aligned Tools**:

| Phase | Tool | Purpose |
|-------|------|---------|
| **Observe** | `recall` | Search memories by query |
| **Observe** | `list` | Browse memories by filters |
| **Orient** | `graph` | Explore semantic relationships |
| **Orient** | `context` | Load full project context |
| **Decide** | `remember` | Store new memory with enrichment |
| **Decide** | `consolidate` | Merge/supersede duplicate memories |
| **Act** | `update` | Modify existing memory |
| **Act** | `delete` | Archive memory (soft delete) |
| **Observe** | `used` | Report which recalled memories were helpful |
| **Observe** | `hierarchy` | Browse the hierarchical topic tree |

## Next Steps

**Remaining work**
1. Continue improving consolidation quality and safety
2. Expand protocol-level contract tests
3. Add further bounded context surfaces as retrieval features grow
