# Mnemosyne + Hermes Agent

This is the canonical setup guide for using Mnemosyne as a local memory layer
for Hermes. It covers the shortest path from zero installation to a verified
memory, then shows how to migrate an existing Python `mnemosyne-memory` store.

## 1. Install a release

The release installer does not require Rust, Cargo, Python, or a cloud API key.
It detects Linux x86_64/aarch64 and macOS x86_64/arm64, verifies the SHA-256
checksum, and installs to `~/.local/bin`:

```bash
curl -fsSL https://raw.githubusercontent.com/juanmackie/mnemosyne-hermes/main/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
mnemosyne --version
```

For a pinned release:

```bash
curl -fsSL https://raw.githubusercontent.com/juanmackie/mnemosyne-hermes/main/install.sh \
  | bash -s -- --version 2.3.3
```

A source checkout remains available when developing the project:

```bash
./install.sh --from-source
# equivalent: ./scripts/install/install.sh --skip-api-key --no-mcp
```

## 2. Connect Hermes

Two integrations are supported. They are independent, and you can enable both:
one captures and injects memory automatically, the other lets the agent call
memory tools explicitly.

| Mode | What you get | Requires |
|---|---|---|
| **Native provider** (recommended) | Automatic capture of user turns, plus automatic context injection before each call. The agent never has to ask for memory. | The `mnemosyne` binary **and** the Python adapter package. |
| **MCP-only** | The agent can call memory tools explicitly when it chooses. Nothing is captured or injected automatically. | Only the `mnemosyne` binary. |

Both modes drive the same local store, so anything written by one is visible to
the other.

### 2a. Native provider mode (automatic memory)

The adapter ships in this repository at
[`integrations/hermes-memory-provider/`](../integrations/hermes-memory-provider/).
It is a pure-standard-library Python package that drives the `mnemosyne` binary
over a persistent MCP stdio session, and it registers the provider id
`mnemosyne-rust`:

```bash
pip install -e integrations/hermes-memory-provider
hermes config set memory.provider mnemosyne-rust
hermes memory status
```

It implements the Hermes `MemoryProvider` contract: `initialize`, `prefetch` before each
call, non-blocking capture of every completed turn, `on_session_end` flush, a
fail-closed `on_pre_compress` checkpoint, and an idempotent `shutdown`. Prefetch returns
**unfenced** text: Hermes applies its own `<memory-context>` wrapper and streaming
scrubber, so the provider must not add those tags itself.

Optional environment variables: `MNEMOSYNE_BIN` (binary path),
`MNEMOSYNE_MCP_ARGS` (default `mcp`), `MNEMOSYNE_DB_PATH`, `MNEMOSYNE_NAMESPACE` (default
`agent:hermes`), `MNEMOSYNE_POLICY_OWNER` (default `mnemosyne-rust`),
`MNEMOSYNE_PREFETCH_TIMEOUT`, and `MNEMOSYNE_REQUEST_TIMEOUT`.

Automatic capture applies to **user-originated turns only**, and only while this
provider owns capture. Exactly one component owns automatic capture at a time, so
a run never double-writes. Cron, flush, subagent, background, and skill-loop
executions are skipped for both capture and injection.

> `memory.provider: mnemosyne` names a *different*, separately installed Python
> provider. This repository does not package it. The provider documented here is
> `mnemosyne-rust`.

### 2b. MCP-only mode (explicit tool calls)

Add the server to `~/.hermes/config.yaml` under the `mcp_servers` key:

```yaml
mcp_servers:
  mnemosyne:
    command: /home/you/.local/bin/mnemosyne
    args: ["mcp"]
    env:
      MNEMOSYNE_DB_PATH: /home/you/.local/share/mnemosyne/mnemosyne.db
```

Use an **absolute** `command` path. Hermes may start the server from a
different working directory than your shell, in which case a bare `mnemosyne`
will not resolve. `MNEMOSYNE_DB_PATH` accepts home-relative, relative, or absolute
paths (a leading `~` is expanded), and absolute paths are recommended for shared
or scripted configuration. The CLI, the importer, and the MCP server all resolve
to the same database.

`mnemosyne mcp` and the legacy `mnemosyne serve` command are equivalent. MCP
stdout is reserved for JSON-RPC; diagnostics go to stderr, so the process is safe
for stdio clients.

### 2c. Docker: keep the store and the model cache on volumes

No image is published from this repository. Build and tag one yourself, then
mount the two paths that must outlive the container. The container filesystem is
disposable; the volumes are not.

```yaml
services:
  mnemosyne:
    image: mnemosyne-hermes:2.3.3        # your own build/tag
    command: ["mcp"]
    stdin_open: true                     # stdio MCP needs the streams attached
    volumes:
      - mnemosyne-db:/data/mnemosyne
      - mnemosyne-models:/home/mnemosyne/.cache/mnemosyne
    environment:
      MNEMOSYNE_DB_PATH: /data/mnemosyne/mnemosyne.db

volumes:
  mnemosyne-db:
  mnemosyne-models:
```

Paths are absolute, and each one means something different on each side of a
mapping: the MCP `env` block points at `/data/mnemosyne/mnemosyne.db` *inside*
the container, while the `mnemosyne-db` volume is what actually persists it. Mount
the embedding model cache as well, or a model-backed image re-downloads the ONNX
weights on every start.

If Hermes runs on the host, point `mcp_servers.command` at a wrapper that keeps
stdio attached, for example `docker run -i --rm -v mnemosyne-db:/data/mnemosyne
mnemosyne-hermes:2.3.3 mcp`. If Hermes runs in the same Compose project, both
services share the `mnemosyne-db` volume instead.
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
| `mnemosyne_bootstrap` | `mnemosyne.bootstrap` | Build bounded project startup context |
| `mnemosyne_update` | `mnemosyne.update` | Amend a memory |
| `mnemosyne_consolidate` | `mnemosyne.consolidate` | Find/consolidate candidates |
| `mnemosyne_used` | `mnemosyne.used` | Report useful recalls |

### Lifecycle tools

These two exist for provider-style use: a memory provider calls them on the
agent's behalf, one before a turn and one after it. Any MCP client can call them
directly as well.

| Tool | Compatibility alias | Purpose |
|---|---|---|
| `mnemosyne.prefetch` | `mnemosyne_prefetch` | Recall context to inject before a call |
| `mnemosyne.sync_turn` | `mnemosyne_sync_turn` | Capture one completed turn |

`mnemosyne.prefetch` takes `query` (required) plus optional `namespace`,
`budget_tokens` (default 1024), `limit` (default 5), and `execution_context`. It returns
`{text, count, diagnostics}`. `text` is **unfenced** - Hermes adds its own
`<memory-context>` wrapper - and `diagnostics` carries the resolved namespace, the
embedding mode, model name, dimensions and fallback reason, per-section counts,
the candidate and selected memory ids, dedup exclusions, and the token budget
accounting. Diagnostics never echo raw memory text.

`mnemosyne.sync_turn` takes `user_text` and `assistant_text` (both required) plus
optional `namespace`, `session_id`, `turn_id`, `execution_context`, `speaker`, and
`policy_owner`. It returns
`{status: "captured", source_memory_id, derived_ids, policy_proposal_ids,
extraction_status}` or `{status: "skipped", reason}`.

Both honour `execution_context`. For `cron`, `flush`, `subagent`,
`background`, and `skill_loop` they skip: prefetch returns empty text with
`skipped: true`, and sync_turn returns `status: "skipped"`. Replaying the same
(`session_id`, `turn_id`) pair reuses the existing turn memory instead of
duplicating it, so retries are safe.

The provider surfaces also include `mnemosyne_persona`,`mnemosyne_canonical`, and `mnemosyne_triples`. Bootstrap is the shared,
read-only startup assembly path: it returns separate approved constraints,
facts, failure guardrails, policies, relevant project-local skills, provenance,
and abstentions under a token budget. Constraint proposals are reviewed with
`mnemosyne constraint` and are only visible to bootstrap after owner approval.
Persona reads durable preference/constraint memories; canonical facts provide one current value per
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
runtime; it uses a deterministic hash embedding for local remember/recall. On
stores larger than 1,000 active memories, import and recall report a warning
because fallback vectors can materially reduce semantic recall. For higher
retrieval quality, upgrade to the model-backed path:

```bash
# 1. Build the model-backed binary (ONNX runtime via fastembed).
cargo build --release --features local-embeddings
#    (or `--features full` for model-backed embeddings, full ICS syntax
#    grammars, and companion TUI/dashboard binaries)

# 2. Install or invoke the rebuilt executable so `mnemosyne` on your PATH is
#    the feature-enabled build, not the older release binary.
cp target/release/mnemosyne ~/.local/bin/mnemosyne

# 3. Verify the provider mode: the loaded provider should no longer report
#    fallback/deterministic embeddings.
mnemosyne status

# 4. Complete the backfill so existing memories get model-backed vectors.
mnemosyne embed --all
```

If you run the model-backed build, pin the bank to a known model. The
`bge-small-en-v1.5` bank (384 dimensions) is a sensible small local default:
select the embedding preset, rebuild with `--features local-embeddings`, reinstall
the rebuilt binary, then confirm the effective configuration before backfilling.

`mnemosyne status` reports the effective embedding configuration (enabled,
model, dimensions, device, cache directory). If a store was written with a
different model than the one now configured, the vector space has changed and the
older vectors are no longer comparable; the configuration reports that conflict
instead of silently mixing the two. Run `mnemosyne embed --all` to re-embed the
whole bank so it matches the configured model again. Never mix models within one
database.
If you prefer not to replace the on-PATH binary, invoke the rebuilt executable
explicitly for each command, e.g. `./target/release/mnemosyne embed --all`.

## Configuration and namespaces

- `MNEMOSYNE_DB_PATH` selects the local SQLite/LibSQL database. The value may
  be absolute, relative, or home-relative; a leading `~` is expanded to your
  home directory so CLI, import, and the MCP server all resolve to the same
  database. Absolute paths are recommended for shared or scripted config.
- `global` stores personal facts shared across projects.
- `agent:hermes` isolates a Hermes identity.
- `project:<name>` isolates a workspace.
- `session:<project>:<id>` isolates temporary context.

For other MCP clients, use the same `mnemosyne mcp` stdio command and the
standard `mcpServers` configuration shape. The underscore aliases are safe for
clients that expose provider tools as native commands. The release is local-only
by default; distributed Iroh peer networking is an explicit source-build
feature (`cargo build --release --features distributed`). See
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
| No vector model is available | The release uses deterministic fallback embeddings; stores over 1,000 active memories emit a retrieval-quality warning. Build with `--features local-embeddings`, then run `mnemosyne embed --all` for model-backed vectors. |

For protocol details, see [MCP_SERVER.md](../MCP_SERVER.md). For retrieval
quality methodology, see [benchmark/retrieval/README.md](../benchmark/retrieval/README.md).
