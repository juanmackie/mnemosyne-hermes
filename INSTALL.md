# Installation Guide

Mnemosyne — self-hostable Python memory system for Hermes and MCP-compatible agents.

## Quick Install (Recommended)

### Prerequisites

- Python 3.11–3.14
- Git
- No Rust toolchain required
- No cloud API key required for core memory operations

### Install from Source

```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
mnemosyne --version
```

### Verify

```bash
# Core memory works without API keys
mnemosyne remember --content "test memory" --namespace agent:hermes --no-enrich
mnemosyne recall --query "test" --namespace agent:hermes
mnemosyne diagnostics
```

## MCP Integration

Add to Hermes `~/.hermes/config.yaml` under `mcp.servers`:

```yaml
mcp:
  servers:
    mnemosyne:
      command: mnemosyne
      args: ["mcp"]
      env:
        MNEMOSYNE_DB_PATH: ~/.hermes/mnemosyne/mnemosyne.db
```

Or for Claude Code, add to `.claude/mcp_config.json`:

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "mnemosyne",
      "args": ["mcp"]
    }
  }
}
```

## Hermes Integration

Two modes (independent, can enable both):

| Mode | What you get | Requires |
|---|---|---|
| **Native provider** (recommended) | Automatic capture + context injection | `mnemosyne` binary + Python adapter |
| **MCP-only** | Agent calls memory tools explicitly | `mnemosyne` binary |

Both modes drive the same local SQLite store.

## Python SDK

```python
from lib.storage import PythonMemoryStorage

storage = PythonMemoryStorage("~/.mnemosyne/mnemosyne.db")
storage.remember("The user prefers local-only storage", "agent:hermes", 5)
results = storage.recall("preferences", namespace="agent:hermes")
```

## CLI Reference

```bash
mnemosyne init                          # Initialize database schema
mnemosyne remember --content "..."      # Store a memory
mnemosyne recall --query "..."          # Search memories
mnemosyne list                          # List memories
mnemosyne bootstrap                     # Return constraints, facts, policies, guardrails
mnemosyne backup                        # Backup database
mnemosyne restore --backup <path>       # Restore from backup
mnemosyne maintenance                   # Dedup, near-dup proposals
mnemosyne diagnostics                   # Counts, timings, versions, queue state
mnemosyne embed                         # Embedding rebuilds (requires upstream source)
mnemosyne migrate                       # Migrations (requires upstream source)
```

## Upgrade

```bash
pip install -e . --upgrade
```

## Uninstall

```bash
pip uninstall mnemosyne
rm -rf ~/.mnemosyne/
```

## Troubleshooting

- `mnemosyne: command not found` — ensure `.venv/bin` is on PATH, or use `python -m mnemosyne.cli`
- `import mnemosyne` fails — reinstall: `pip install -e .`
- Database locked — another process is using it; wait or check `mnemosyne diagnostics`
- `mnemosyne-memory 3.15.1` upstream source missing — documented blocker; core memory works without it
- `secrets.age` not found — run `mnemosyne secrets init` (optional; core memory is keyless)

**Last Updated**: 2026-09-15
**Version**: 2.4.0
