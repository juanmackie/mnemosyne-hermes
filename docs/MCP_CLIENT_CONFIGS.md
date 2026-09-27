# MCP client configuration examples

`mnemosyne-lite` is a local MCP stdio server for the standalone lite store. It
is not the Hermes provider: install it in its own virtualenv, never in a Hermes
venv, where the engine owns the `mnemosyne` name. Every client launches the same
command and sets the same database environment variable; no client-specific
protocol adapter is required.

```json
{
  "mcpServers": {
    "mnemosyne-lite": {
      "command": "mnemosyne-lite",
      "args": ["mcp"],
      "env": {
        "MNEMOSYNE_DB_PATH": "~/.mnemosyne-lite/mnemosyne.db"
      }
    }
  }
}
```

Use this `mcpServers` object in the client's MCP settings for **Claude Code**,
**Cursor**, **Windsurf**, and **OpenClaw**. If the client requires an absolute
command path, replace `mnemosyne-lite` with the absolute path of the installed
console script (for a `uv tool install` or a virtualenv, it is that
environment's `bin/mnemosyne-lite`).

## Codex

Codex uses TOML for MCP server entries. The equivalent configuration is:

```toml
[mcp_servers.mnemosyne-lite]
command = "mnemosyne-lite"
args = ["mcp"]

[mcp_servers.mnemosyne-lite.env]
MNEMOSYNE_DB_PATH = "~/.mnemosyne-lite/mnemosyne.db"
```

## Generic MCP clients

For clients that expose the standard MCP server map, use the JSON example
above. The server writes JSON-RPC only to stdout and sends diagnostics to
stderr, which keeps the stdio transport clean.

The advertised tools are `mnemosyne_memory_search`, `mnemosyne_memory_remember`,
`mnemosyne_prefetch` and `mnemosyne_sync_turn`. The dotted aliases
`mnemosyne.recall` and `mnemosyne.remember` are accepted on a call but are not
advertised by `tools/list`. A failed call returns `isError: true` with the
reason, so a client can tell "no match" from "store broken".

After adding the server, verify the executable independently:

```bash
mnemosyne-lite --version
mnemosyne-lite diagnostics        # prints the resolved DB path
```

For the provider, the database path precedence and the setup runbook, see
[AGENT_SETUP.md](AGENT_SETUP.md), [HERMES_INTEGRATION.md](HERMES_INTEGRATION.md)
and `integrations/hermes-provider/README.md`.
