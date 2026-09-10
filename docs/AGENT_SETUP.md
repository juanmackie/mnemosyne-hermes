# Mnemosyne setup runbook (agent-executed)

You are an AI agent installing Mnemosyne — a local-first memory system — for
your user. Execute steps in order.

**Contract**
- Run pre-checks and verification gates; proceed only when they pass.
- Never delete/overwrite existing config, DB, or binaries. Merge/skip and report.
- No cloud API keys required. If a step demands one, stop — that's a bug.
- A step fails twice → stop and report exact output.
- Finish with a checklist: step, pass/fail, one-line evidence.

---

## 0. Pre-check
```bash
command -v mnemosyne && mnemosyne --version
ls ~/.local/share/mnemosyne/mnemosyne.db 2>/dev/null
```
Installed → keep; DB exists → keep. Report findings.

## 1. Install release binary
```bash
curl -fsSL https://raw.githubusercontent.com/juanmackie/mnemosyne-hermes/main/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
```
Checksum failure → stop and show output. Skip nothing unless user asks.

**Verify:** `mnemosyne --version`  
Not found → append `export PATH="$HOME/.local/bin:$PATH"` to shell rc, re-source, retry.  
macOS `killed` error → `xattr -d com.apple.provenance ~/.local/bin/mnemosyne`; `codesign --force --sign - ~/.local/bin/mnemosyne`

## 2. Pin the DB location
```bash
mkdir -p ~/.local/share/mnemosyne
echo 'export MNEMOSYNE_DB_PATH=~/.local/share/mnemosyne/mnemosyne.db' >> ~/.bashrc
export MNEMOSYNE_DB_PATH=~/.local/share/mnemosyne/mnemosyne.db
```
Keep existing DB. Use user's actual shell rc (`~/.zshrc`/etc. as needed).

**Verify:** `echo $MNEMOSYNE_DB_PATH` is set; directory exists.

## 3. Health check
```bash
mnemosyne doctor --json     # add --fix for auto-fixable
```
**Verify:** doctor reports no blocking issues. "No embedding backend" is acceptable — search degrades gracefully to keyword + graph. Note it.

## 4. Smoke test (keyless — keyword recall works)
```bash
mnemosyne remember "The staging deploy target is host kraken-01" --importance 8
mnemosyne recall --query "kraken-01" --limit 5
```
**Verify:** recall result contains the stored memory.  
Then test the always-on profile slice (independent of matching):
```bash
mnemosyne remember "User prefers terse answers with copy-pasteable code" \
  --namespace "profile:identity" --importance 9
mnemosyne recall --query "kraken-01" --limit 5
```
**Verify:** response carries profile / profile-dynamic block beside results — Mnemosyne's standing context, independent of the keyword hit.

## 5. Connect agent runtime (MCP stdio)
Merge into runtime's MCP config (Hermes: `~/.hermes/config.yaml`; Claude Code / Cursor / Codex: client's own `mcpServers`). Preserve existing entries:
```yaml
mcp:
  servers:
    mnemosyne:
      command: mnemosyne
      args: ["mcp"]
      env:
        MNEMOSYNE_DB_PATH: "~/.local/share/mnemosyne/mnemosyne.db"
        MNEMOSYNE_NAMESPACE: "agent:hermes"   # pin scope; omit for auto-detect
```
Hermes users: `hermes config set memory.provider mnemosyne`; `hermes memory status`.

**Verify:** restart runtime; store + recall through mnemosyne tools. (`mnemosyne mcp` stdout is pure JSON-RPC; diagnostics go to stderr — safe for stdio clients.)

## 6. Optional — migrate old Python store
Only if previous `mnemosyne-memory` DB exists (e.g. `~/.hermes/mnemosyne/data/mnemosyne.db`). Dry-run first; show user before importing:
```bash
mnemosyne import --from <path> --dry-run --format json
mnemosyne import --from <path> --namespace "agent:hermes" --format json   # after OK
```

## 7. Optional — prove typed graph edges
Requires embedding-capable build (`cargo build --release --features local-embeddings`). If doctor reports no embeddings, skip; say so.  
Store the same sentence twice: second write reports duplicate and creates no new row — Mnemosyne writes an `extends` edge from newer memory to reaffirmed fact instead.

**Verify:** recall returns the fact exactly once.

---
References: `docs/HERMES_INTEGRATION.md` (canonical), `docs/BOOTSTRAP.md`, `README.md`.  
Source: `https://github.com/juanmackie/mnemosyne-hermes`
