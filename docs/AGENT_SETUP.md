# Mnemosyne setup runbook (agent-executed)

You are an AI agent installing Mnemosyne — a local-first memory system — as the
Hermes memory provider. Execute steps in order.

**Contract**
- Run pre-checks and verification gates; proceed only when they pass.
- Never delete/overwrite existing config, DB, or binaries. Merge/skip and report.
- No cloud API keys required. If a step demands one, stop — that's a bug.
- A step fails twice → stop and report exact output.
- Finish with a checklist: step, pass/fail, one-line evidence.

---

## 0. Pre-check
```bash
hermes --version            # supported range: >=0.18,<0.20
hermes memory status        # note the current provider before changing it
```
If a memory DB already exists, keep it — do not delete or re-initialize.

## 1. Clone and install the provider
```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes
./install.sh --dry-run      # inspect dependency owner, paths and provider selection
./install.sh               # install the provider with its audited engine pin
```
There is **no curl one-liner** — the installer needs a checkout — and there is
no native release binary to download. `./install.sh --dry-run` prints the
resolved venv, DB path and symlink target without writing anything.

On native Windows, execute `.\install.ps1 -DryRun`, then
`.\install.ps1 -Yes`; pass `-Venv` or `-Python` when discovery needs a path.
The native installer uses a verified copy and supports PowerShell 5.1/7.
Both installers use Hermes PM admission when that environment is managed.

**Verify:** the installer exits zero after activation, engine import,
registration and actual Hermes-loader selection checks. A same-name bundled
provider that blocks the deployed snapshot is a refusal, not a successful
install. Resolve that supported host configuration before continuing.

## 2. Verify
```bash
hermes mnemosyne doctor --no-fix   # must exit 0
hermes memory status               # mnemosyne installed / available / active
```
`doctor` prints where memory actually lives (resolved DB path, provider package,
engine version) and warns if the DB sits outside `$HERMES_HOME`.

## 3. Database location
The provider resolves its DB in this order:
`memory.mnemosyne.db_path` > `MNEMOSYNE_DB_PATH` > engine default
(`MNEMOSYNE_DATA_DIR` > `$HERMES_HOME` > `~/.hermes`).

Prefer the default (`$HERMES_HOME/mnemosyne/data/mnemosyne.db`). If you set
`MNEMOSYNE_DATA_DIR` or `MNEMOSYNE_DB_PATH` outside `$HERMES_HOME`, `doctor`
will warn that logs and memory are split. Do not set both `db_path` and
`profile_isolation`.

## 4. Smoke test (keyless — keyword recall works)
```bash
hermes mnemosyne stats                 # counts from the live store
hermes mnemosyne inspect "test"        # search the live store
```
For a full write/read round-trip, ask the agent to remember a fact and recall
it, or run the CI smoke test: `bash scripts/smoke-hermes-onboarding.sh`.

For automatic recall tuning, read [HERMES_CONFIGURATION.md](HERMES_CONFIGURATION.md).
Keep the default session scope and four tools unless the task needs another
setting. Evaluate caching against real session hits before enabling it; the
offline exact-repeat benchmark alone does not establish next-turn reuse.

## 5. The lite MCP surface (optional, independent of the provider)

This is the **lite** store's MCP server, not the provider. Install it in its
own virtualenv — never in the Hermes venv, where the engine owns the
`mnemosyne` name.

```bash
pip install -e .                      # installs the mnemosyne-lite CLI
mnemosyne-lite init                   # creates ~/.mnemosyne-lite/mnemosyne.db
```

MCP client configuration:

```json
{"mcpServers": {"mnemosyne-lite": {"command": "mnemosyne-lite", "args": ["mcp"]}}}
```

**Verify:** `mnemosyne-lite diagnostics` prints the resolved DB path, then call
`mnemosyne_memory_remember` and `mnemosyne_memory_search` through the client.
(`mnemosyne-lite mcp` writes pure JSON-RPC to stdout; diagnostics go to stderr.)
See `MCP_SERVER.md` and `docs/MCP_CLIENT_CONFIGS.md`.

---
References: `integrations/hermes-provider/README.md`,
`docs/HERMES_INTEGRATION.md`, `README.md`.
Source: `https://github.com/juanmackie/mnemosyne-hermes`
