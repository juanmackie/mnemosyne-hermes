# Mnemosyne + Hermes Agent

This is the canonical setup guide for using Mnemosyne as a local memory layer
for Hermes. It covers the shortest path from zero installation to a verified
memory, then shows how to migrate an existing Python `mnemosyne-memory` store.

## 1. Install a release

The release installer does not require Rust, Cargo, Python, or a cloud API key.
It detects Linux x86_64/aarch64 and macOS x86_64/arm64, verifies the SHA-256
checksum, and installs to `~/.local/bin`:

```bash
curl -fsSL https://raw.githubusercontent.com/juanmackie/mnemosyne-hermes/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
mnemosyne --version
```

For a pinned release:

```bash
curl -fsSL https://raw.githubusercontent.com/juanmackie/mnemosyne-hermes/main/install.sh \
  | sh -s -- --version 2.3.1
```

A source checkout remains available when developing the project:

```bash
./install.sh --from-source
# equivalent: ./scripts/install/install.sh --skip-api-key --no-mcp
```

## 2. Connect Hermes

Hermes can use the standard MCP stdio transport directly. Add this to the
active Hermes configuration (usually `~/.hermes/config.yaml`):

```yaml
mcp:
  servers:
    mnemosyne:
      command: mnemosyne
      args: ["mcp"]
      env:
        MNEMOSYNE_DB_PATH: "~/.local/share/mnemosyne/mnemosyne.db"
```

If you are using Hermes' provider selector, enable the same local provider:

```bash
hermes config set memory.provider mnemosyne
hermes memory status
```

`mcp` and the legacy `serve` command are equivalent. MCP stdout is reserved for
JSON-RPC; diagnostics are sent to stderr, so the process is safe for stdio
clients.

## 3. Tool surface

Mnemosyne retains its original dotted MCP names and advertises Hermes-compatible
underscore aliases with identical schemas:

| Hermes tool | Compatibility name | Purpose |
|---|---|---|
| `mnemosyne_remember` | `mnemosyne.remember` | Store durable memory |
| `mnemosyne_recall` | `mnemosyne.recall` | Search ranked memories |
| `mnemosyne_forget` | `mnemosyne.delete` | Archive a memory |
| `mnemosyne_list` | `mnemosyne.list` | Browse recent memories |
| `mnemosyne_context` | `mnemosyne.context` | Load linked context |
| `mnemosyne_graph` | `mnemosyne.graph` | Traverse memory links |
| `mnemosyne_hierarchy` | `mnemosyne.hierarchy` | Browse topic hierarchy |
| `mnemosyne_update` | `mnemosyne.update` | Amend a memory |
| `mnemosyne_consolidate` | `mnemosyne.consolidate` | Find/consolidate candidates |
| `mnemosyne_used` | `mnemosyne.used` | Report useful recalls |

The provider surfaces also include `mnemosyne_persona`,
`mnemosyne_canonical`, and `mnemosyne_triples`. Persona reads durable
preference/constraint memories; canonical facts provide one current value per
(category, name) slot; triples provide add/query operations with one current
object per (subject, predicate) slot and archived superseded values. Imported
canonical/triple rows remain tagged memory records, so the source data is still
portable even when a provider version has extra columns.

## 4. Migrate an existing Python memory store

Keep the original database as a backup. The importer reads the source and never
writes to it. It supports the common Python provider tables when present:
`working_memory`, `episodic_memory`, legacy `memories`, `canonical_facts`,
`triples`, `facts`, and `annotations`.

Preview counts first:

```bash
mnemosyne import --from ~/.hermes/mnemosyne/data/mnemosyne.db \
  --namespace agent:hermes --dry-run --format json
```

Import into the default Rust store (or set `MNEMOSYNE_DB_PATH`):

```bash
MNEMOSYNE_DB_PATH="$HOME/.local/share/mnemosyne/mnemosyne.db" \
  mnemosyne import --from ~/.hermes/mnemosyne/data/mnemosyne.db \
  --namespace agent:hermes --format json
```

Import IDs are deterministic. Running the same command again skips rows already
present instead of duplicating them. The report includes scanned/imported/skipped
counts and source-table metadata. Use a different `--namespace` for each Hermes
profile or memory bank.

## 5. Verify without a cloud key

```bash
unset ANTHROPIC_API_KEY OPENAI_API_KEY
MNEMOSYNE_DB_PATH="$HOME/.local/share/mnemosyne/mnemosyne.db" \
  mnemosyne remember --content "The user prefers local-only storage" \
  --namespace agent:hermes --no-enrich --format json
MNEMOSYNE_DB_PATH="$HOME/.local/share/mnemosyne/mnemosyne.db" \
  mnemosyne recall --query "where should memory be stored" \
  --namespace agent:hermes --format json
```

Core storage, keyword search, import, list, graph, MCP discovery, and the
release binary's deterministic fallback embeddings do not require an API key or
network access. The default release intentionally excludes the ONNX model
runtime; it uses a deterministic hash embedding for local remember/recall. A
source build can opt into the larger model-backed path with
`cargo build --release --features local-embeddings` (or `--features full`
for the model-backed embeddings plus full ICS syntax grammars).

## Configuration and namespaces

- `MNEMOSYNE_DB_PATH` selects the local SQLite/LibSQL database.
- `global` stores personal facts shared across projects.
- `agent:hermes` isolates a Hermes identity.
- `project:<name>` isolates a workspace.
- `session:<project>:<id>` isolates temporary context.

For other MCP clients, use the same `mnemosyne mcp` stdio command and the
standard `mcpServers` configuration shape. The underscore aliases are safe for
clients that expose provider tools as native commands. See
[MCP client configuration examples](MCP_CLIENT_CONFIGS.md) for Claude Code,
Cursor, Codex, Windsurf, OpenClaw, and generic MCP clients.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mnemosyne: command not found` | Add `~/.local/bin` to `PATH` or pass `--bin-dir` during install. |
| Release checksum fails | Delete the partial download and retry; do not bypass verification. |
| Hermes cannot start the server | Run `mnemosyne mcp --help`; use an absolute command path in Hermes config. |
| Memories are in the wrong store | Set `MNEMOSYNE_DB_PATH` in the MCP server `env` block and in CLI commands. |
| Import reports zero rows | Run `--dry-run --format json`; inspect source table presence and keep the original DB unchanged. |
| No vector model is available | The release uses deterministic fallback embeddings; build with `--features local-embeddings` only when model-backed vectors are needed. |

For protocol details, see [MCP_SERVER.md](../MCP_SERVER.md). For retrieval
quality methodology, see [benchmark/retrieval/README.md](../benchmark/retrieval/README.md).
