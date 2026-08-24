# MCP client configuration examples

Mnemosyne is a local MCP stdio server. Every client should launch the same
public command and set the same database environment variable; no client-
specific protocol adapter is required.

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "mnemosyne",
      "args": ["mcp"],
      "env": {
        "MNEMOSYNE_DB_PATH": "~/.local/share/mnemosyne/mnemosyne.db"
      }
    }
  }
}
```

Use this `mcpServers` object in the client's MCP settings for **Claude Code**,
**Cursor**, **Windsurf**, and **OpenClaw**. If the client requires an absolute
command path, replace `mnemosyne` with `$HOME/.local/bin/mnemosyne` (expanded to
an absolute path in the actual settings file).

## Codex

Codex uses TOML for MCP server entries. The equivalent configuration is:

```toml
[mcp_servers.mnemosyne]
command = "mnemosyne"
args = ["mcp"]

[mcp_servers.mnemosyne.env]
MNEMOSYNE_DB_PATH = "~/.local/share/mnemosyne/mnemosyne.db"
```

## Generic MCP clients

For clients that expose the standard MCP server map, use the JSON example
above. The server writes JSON-RPC only to stdout and sends diagnostics to
stderr, which keeps the stdio transport clean. Existing dotted tool names and
Hermes-compatible underscore aliases are both advertised.

After adding the server, verify the executable independently:

```bash
mnemosyne mcp --help
mnemosyne --version
```

For installation, import, namespaces, and keyless remember/recall, see
[HERMES_INTEGRATION.md](HERMES_INTEGRATION.md).
