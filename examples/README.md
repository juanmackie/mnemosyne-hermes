# Examples

Small, runnable examples for the lite surface (`mnemosyne-lite`). They assume
the lite package is installed in its own virtualenv:

```bash
pip install -e .
mnemosyne-lite init          # creates ~/.mnemosyne-lite/mnemosyne.db
```

Do **not** install it into a Hermes venv: the engine (`mnemosyne-memory`) owns
the `mnemosyne` distribution name, import package and console script, and the
two must not merge. See [../README.md](../README.md).

```text
examples/
├── basic-usage/          the lite CLI
│   ├── store-memory.sh       remember, with no LLM and no API key
│   ├── search-memories.sh    recall, with namespace and importance filters
│   └── export-markdown.sh    dump the store to a file (and when to use backup)
└── hermes/
    ├── HERMES.md             a memory-workspace convention for a Hermes agent
    └── mcp-config.json       an MCP client entry for the lite stdio server
```

## `basic-usage/`

Each script takes optional arguments and prints what it is doing. They all read
`MNEMOSYNE_DB_PATH` and accept `--db-path` where it makes sense.

```bash
./examples/basic-usage/store-memory.sh
./examples/basic-usage/search-memories.sh "architecture decision"
./examples/basic-usage/export-markdown.sh memories.txt
```

## `hermes/`

`HERMES.md` is an example of the convention file a Hermes agent reads to decide
which namespace a memory belongs in. `mcp-config.json` is the MCP client entry
for the lite server — copy it into your client's MCP settings and replace the
absolute paths.

The lite MCP server exposes `mnemosyne_memory_search`,
`mnemosyne_memory_remember`, `mnemosyne_prefetch` and `mnemosyne_sync_turn`.
See [../MCP_SERVER.md](../MCP_SERVER.md) and
[../docs/MCP_CLIENT_CONFIGS.md](../docs/MCP_CLIENT_CONFIGS.md).

## Provider examples

The Hermes **provider** is not exercised here; it is installed and verified by
`./install.sh` and `hermes mnemosyne doctor`. Start with
[../integrations/hermes-provider/README.md](../integrations/hermes-provider/README.md)
and [../docs/AGENT_SETUP.md](../docs/AGENT_SETUP.md).
