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
hermes --version            # any version; there is no required Hermes version
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

## 6. Keep it updated

Updates are commits on `main` of the checkout from step 1 — there are no GitHub
releases. `scripts/update.sh` (native Windows: `scripts\update.ps1`) is the one
tool for both noticing and applying them:

| Command | Effect | Exit |
| --- | --- | --- |
| `scripts/update.sh --check` | Fetch, print new commits. Changes nothing. | `0` current, `10` update available |
| `scripts/update.sh --apply [-- INSTALL_ARGS]` | Fast-forward, `./install.sh --yes INSTALL_ARGS`, `hermes mnemosyne doctor --no-fix`. Restores the old commit and install if either fails. | `0` ok, `1` failed |

`--quiet` prints nothing when already current. `--apply` refuses a dirty tree,
a diverged history or another branch, and never touches the memory database.
Pass the same `--venv` / `--hermes-home` you gave the installer after `--`.

**Notify only (default).** Hermes can run a script on a schedule and deliver its
output; empty output is silent. Check `hermes cron create --help` on your
version, then:

```bash
mkdir -p ~/.hermes/scripts
cat > ~/.hermes/scripts/mnemosyne-update-check.sh <<'EOF'
#!/usr/bin/env bash
# Prints the new commits only when an update exists; exit 10 means "update available".
"$HOME/mnemosyne-hermes/scripts/update.sh" --check --quiet || [ "$?" -eq 10 ]
EOF
chmod +x ~/.hermes/scripts/mnemosyne-update-check.sh
hermes cron create "0 9 * * *" --no-agent --script mnemosyne-update-check.sh \
  --name mnemosyne-update-check
hermes cron list          # confirm the job exists; add --deliver <target> to choose where notices go
```

**Apply automatically (only if the user asked for it).** `--apply` runs the
installer from the fetched commits, so enable it only for a remote you trust:

```bash
cat > ~/.hermes/scripts/mnemosyne-update-apply.sh <<'EOF'
#!/usr/bin/env bash
"$HOME/mnemosyne-hermes/scripts/update.sh" --apply --quiet -- --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
EOF
chmod +x ~/.hermes/scripts/mnemosyne-update-apply.sh
hermes cron create "0 4 * * *" --no-agent --script mnemosyne-update-apply.sh \
  --name mnemosyne-update-apply
```

A successful apply prints `Updated to <sha>`. Restart the Hermes gateway
afterwards: Hermes caches the loaded provider module until the process ends.
If `hermes` is not on the scheduler's `PATH`, add `--venv <hermes venv>` after
the `--`.

Without Hermes cron, use any scheduler. Linux/macOS `crontab -e`:
`0 9 * * * $HOME/mnemosyne-hermes/scripts/update.sh --check --quiet` (cron mails
the output when `MAILTO` is set). Windows Task Scheduler: run
`powershell -NoProfile -ExecutionPolicy Bypass -File <checkout>\scripts\update.ps1 -Check -Quiet`
daily. Anything that reads Atom feeds can follow
`https://github.com/juanmackie/mnemosyne-hermes/commits/main.atom`.

---
References: `integrations/hermes-provider/README.md`,
`docs/HERMES_INTEGRATION.md`, `README.md`.
Source: `https://github.com/juanmackie/mnemosyne-hermes`
