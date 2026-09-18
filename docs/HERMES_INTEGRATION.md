# Mnemosyne + Hermes Agent

This is the canonical setup guide for using Mnemosyne as a local memory layer
for Hermes. It covers the shortest path from zero installation to a verified
memory, then shows how to migrate an existing Python `mnemosyne-memory` store.

## Adapter Status (Updated)

The Python adapter (`mnemosyne_rust_hermes`) is **retired as a provider**: it drove a
Rust binary over MCP stdio and its registration path was never read by the Hermes
plugin loader. It is preserved for reference and its tests still run. The canonical
provider is `mnemosyne` — see [section 3a](#3a-native-provider-mode-automatic-memory).

```bash
# Retired — kept only so old links resolve. Do not install it as a provider:
#   python -m pip install integrations/hermes-memory-provider/
```

Historically it communicated with a `mnemosyne` binary over a persistent stdio
JSON-RPC session (`MNEMOSYNE_BIN`). No current install path registers it.

## 1. Install the provider and the engine

`./install.sh` installs the vendored provider plus its pinned engine with `uv`,
links `$HERMES_HOME/plugins/mnemosyne`, and selects `memory.provider: mnemosyne`.
The release-binary installer described in older revisions of this file no longer
exists in this repository.

```bash
./install.sh --dry-run     # plan only: venv, symlink target, resolved DB path
./install.sh               # writes nothing until you confirm
hermes mnemosyne doctor --no-fix
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

## Python provider path (zero-config setup)

For a Python-native memory provider (no Rust binary required), run the Python provider install script. It creates the plugin symlink, writes the Hermes config, verifies the binary/link, and prepares the DB:

```bash
./install.sh --repo-url https://github.com/juanmackie/mnemosyne-hermes
# or for a quick setup with a given repo:
./install.sh --setup "https://github.com/juanmackie/mnemosyne-hermes"
```

The script creates `~/.hermes/plugins/mnemosyne` as a symlink to the repository source and writes `~/.hermes/config.yaml` with `memory.provider: mnemosyne` and the default DB path (`~/.mnemosyne/mnemosyne.db`). It does not perform destructive operations (no DB migration, no binary release, no publish).

After setup, initialize the database explicitly:

```bash
mnemosyne init
```

Verify the provider is registered and the link is healthy:

```bash
ls -l ~/.hermes/plugins/mnemosyne
cat ~/.hermes/config.yaml
mnemosyne diagnostics
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

### 2. LLM inheritance (no second API key)

Mnemosyne's optional orchestration and DSPy features inherit the active Hermes
model from `$HERMES_HOME/config.yaml` (default `~/.hermes/config.yaml`). The
configured `model.default` is sent through Hermes' local OpenAI-compatible
subscription proxy when using Portal/OAuth. For a configured custom provider,
its endpoint and provider credential are inherited from the Hermes profile
(`.env`) rather than a second Mnemosyne key; secrets are never logged or
persisted by Mnemosyne.

Start the proxy once in the Hermes environment:

```bash
hermes setup --portal
hermes proxy start
```

The default endpoint is `http://127.0.0.1:8645/v1`. Set
`HERMES_PROXY_BASE_URL` only when the proxy uses another address. If no Hermes
instance or proxy is configured, local memory still works and standalone
`ANTHROPIC_API_KEY` remains a legacy fallback.

### 3a. Native provider mode (automatic memory)

**The canonical provider in this repository is the vendored, engine-backed one
at [`integrations/hermes-provider/`](../integrations/hermes-provider/README.md).**
It is a byte-identical snapshot of `mnemosyne-memory 3.15.1`'s
`hermes_memory_provider` plus a small, declared local patch layer, it registers
the provider id **`mnemosyne`**, and it is what `./install.sh` installs:

```bash
./install.sh --dry-run     # show venv, symlink target and DB path; write nothing
./install.sh               # uv install + plugin symlink + memory.provider=mnemosyne
hermes mnemosyne doctor --no-fix
```

It implements the Hermes `MemoryProvider` contract: `initialize`, prefetch before each
call, non-blocking capture of every completed turn, `on_session_end` flush, and an
idempotent `shutdown`. Tool results are JSON strings, and a failed engine import
reports `unavailable_reason()` instead of pretending to be healthy — see
`integrations/hermes-provider/CONTRACT_AUDIT.md` for the line-by-line audit
(including the hooks it deliberately does **not** override). Prefetch returns
**unfenced** text: Hermes applies its own `<memory-context>` wrapper and streaming
scrubber, so the provider must not add those tags itself.

Automatic capture applies to **user-originated turns only**, and only while this
provider owns capture. Exactly one component owns automatic capture at a time, so
a run never double-writes. Cron, flush, subagent, background, and skill-loop
executions are skipped for both capture and injection.

> The Rust-era `mnemosyne-rust` adapter in
> [`integrations/hermes-memory-provider/`](../integrations/hermes-memory-provider/)
> is **retired as a provider**: it drove a binary over MCP stdio and its
> `hermes_agent.memory_providers` entry point was never read by the Hermes plugin
> loader. Its tests still run; nothing registers it.
>
> The slim `mnemosyne_hermes` provider that used to live in
> `integrations/hermes/` is also retired — see that directory's README.

### 3b. MCP-only mode (explicit tool calls)

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

### 3c. Docker: keep the store and the model cache on volumes

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
## 4. Tool surface

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
# 1. Install the Python-native package (no Rust compiler needed).
pip install -e .

# 2. Verify the provider mode: the adapter uses PythonMemoryStorage
#    with synchronous=NORMAL durability.
mnemosyne diagnostics

# 3. Embedding rebuilds require upstream mnemosyne-memory 3.15.1 source
#    (blocked until upstream source is fetched).
mnemosyne embed --all  # blocked: upstream source MISSING
```

The `bge-small-en-v1.5` embedding identity (384 dimensions) is preserved
in `.mnemosyne_notes` and adapter contracts. Do not mix embedding models
within one database. Run `mnemosyne embed --all` to re-embed the
whole bank once upstream source is available.

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
by default. Distributed Iroh peer networking
is an explicit source-build feature (blocked until upstream source is fetched).
See [MCP client configuration examples](MCP_CLIENT_CONFIGS.md) for Claude Code,
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
