# Item 7 — Deployment Reproducible and Recoverable (Planning Deliverable)

Status: PLANNING ONLY. No package built; no image redeployed; no TrueNAS operation executed. Contracts preserved: plugin package `mnemosyne-rust` (or future `mnemosyne`), pinned dependencies (`pyproject.toml` + `requirements.txt` + `Cargo.toml` + `migrations/`), DB location (`MNEMOSYNE_DB_PATH`), namespace (`agent:hermes`), provider identity (`mnemosyne-rust` / `mnemosyne`).

## Package design (plugin + patched engine)

Based on `.auto/deliverables/item_02_python_baseline.md` and `pyproject.toml`:

### Plugin package structure (reproducible)

```
mnemosyne-hermes-python/
  pyproject.toml              # pinned: maturin>=1.0,<2.0; setuptools>=61; Python>=3.11
  requirements.txt            # pinned: claude-agent-sdk>=0.1.0, asyncio>=3.4.3, rich>=13.0.0 (optional Python feature)
  integrations/
    hermes-memory-provider/
      mnemosyne_rust_hermes/    # adapter source (preserved exactly)
      tests/                    # contract tests (preserved exactly)
      README.md                 # contract (preserved exactly)
  patches/
    001_link_type_fix.diff     # from .auto/evidence/link_type_fix.patch (reproducible patch for upstream or engine)
  deploy/
    .env.example               # variables: MNEMOSYNE_BIN, MNEMOSYNE_DB_PATH, MNEMOSYNE_NAMESPACE, MNEMOSYNE_PROVIDER_ID
    provider.json.example        # config structure (no real paths or secrets)
    deploy_config.md            # this file
  scripts/
    verify_install.sh           # clean-install test (design from item 2)
    redeploy_trueNAS.sh         # proposed redeploy procedure (not executed)
    rollback.sh                 # proposed rollback (not executed)
  docs/
    INSTALL.md                  # updated for Python-only runtime (see item 8)
```

Note: `mnemosyne-memory 3.15.1` upstream source is MISSING; the package design assumes it will be fetched or referenced before final package creation. This is explicitly labeled `unknown`.

## Pinned dependencies (current evidence)

- Python: `pyproject.toml` (build-system `maturin>=1.0,<2.0`; requires-python `>=3.11`); no exact Python micro version pinned (`python==3.11.9` not specified).
- Dependencies: `claude-agent-sdk>=0.1.0`, `asyncio>=3.4.3`, `rich>=13.0.0` (`requirements.txt`); `anthropic>=0.72.0`, `dspy-ai>=3.0.3` (`pyproject.toml` optional/development).
- Rust binary dependency: the adapter (`mnemosyne_rust_hermes`) depends on `mnemosyne` binary on `PATH` or `MNEMOSYNE_BIN`. The binary build is managed by `Makefile` (`cargo build --release`) and `scripts/rebuild-and-update-install.sh`. There is NO pinned binary digest (`sha256:` empty in `.auto/evidence/claim_001.json`).
- SQLite/LibSQL schema: `migrations/libsql/` and `migrations/sqlite/` contain schema versions (`001_...sql` through `027_...sql`); migration `027` creates fact relation and rebuilds maintenance constraint. Schema versions must be preserved in package.

## TrueNAS image redeploy procedure (design only)

Based on `docs/OPERATIONS.md` (not fully read; must verify) and plugin contract:

```bash
#!/bin/bash
# redeploy_trueNAS.sh — proposed (NOT EXECUTED)
set -euo pipefail

# 1. Pre-update backup
DB_PATH="${MNEMOSYNE_DB_PATH:-$HOME/.hermes/mnemosyne/mnemosyne.db}"
BACKUP_DIR="/srv/backups/mnemosyne/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 "$DB_PATH" ".backup to '${BACKUP_DIR}/mnemosyne_pre_update.db'"
# Verify integrity
sqlite3 "${BACKUP_DIR}/mnemosyne_pre_update.db" "PRAGMA integrity_check;"
# Note: backup must include committed WAL data (TRUNCATE checkpoint ensures this).

# 2. Stop current services / processes
systemctl stop mnemosyne-hermes || true  # or container stop: docker stop mnemosyne-hermes
# Wait for shutdown timeout (MNEMOSYNE_SHUTDOWN_TIMEOUT = 5s default)
sleep 6

# 3. Deploy new image / binary
# For container:
# docker pull <new_digest>:mnemosyne-hermes
# docker rm -f mnemosyne-hermes || true
# docker run -d --name mnemosyne-hermes \
#   -e MNEMOSYNE_DB_PATH=/data/mnemosyne.db \
#   -e MNEMOSYNE_NAMESPACE=agent:hermes \
#   -v /srv/data/mnemosyne:/data \
#   <new_digest>:mnemosyne-hermes
# Note: digest must be verified; currently UNKNOWN (empty sha256 in claim_001.json).

# 4. Smoke checks after redeploy
# a. Verify binary responds to `MNEMOSYNE_BIN --version` (or `mnemosyne --version`)
# b. Verify adapter registers (`python -c "import importlib.metadata; ..."`)
# c. Verify DB connection (`sqlite3 $DB_PATH ".tables"` shows expected tables)
# d. Verify namespace (`python -c "from mnemosyne_rust_hermes.config import default_config; ... namespace ..."`)
# e. Run basic plugin test (`python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v` — basic contract only)
# f. Verify checkpoint directory exists and is writable (`ls -l <DB_PATH>/checkpoints/`)

# 5. Rollback path (if smoke fails)
# a. Stop new container/service.
# b. Restore DB from pre-update backup (`sqlite3 <DB> ".restore '${BACKUP_DIR}/mnemosyne_pre_update.db'"` or file copy).
# c. Restart previous image/service.
# d. Verify rollback integrity (`sqlite3 <DB> "PRAGMA integrity_check;"` and representative record counts).
# Note: rollback must NOT lose committed WAL data; backup before redeploy ensures this.
```

Note: The exact redeploy procedure requires a confirmed container digest, a confirmed `MNEMOSYNE_BIN` binary version, and a confirmed DB path. All three are currently labeled UNKNOWN (see item 2 gaps).

## Smoke checks (post-deploy verification)

Based on `.github/workflows/ci.yml` (Rust CI) and plugin contract (`provider.py`):

1. `MNEMOSYNE_BIN` responds to `--version` or `mcp --help` (binary available and executable).
2. Plugin entry point registered (`hermes_agent.memory_providers` includes `mnemosyne-rust` or `mnemosyne`).
3. Config resolves (`MNEMOSYNE_NAMESPACE == agent:hermes`; `MNEMOSYNE_DB_PATH` exists and is writable).
4. Adapter contracts preserved (`provider.name == provider_id`; `namespace == agent:hermes`; `db_path` resolves correctly).
5. Basic tool call succeeds (`mnemosyne_memory_remember` or `mnemosyne_memory_search` returns without exception; does not require real memory success, only transport response).
6. Checkpoint directory exists and is writable (`provider._checkpoint_dir()` creates directory; `os.access(dir, os.W_OK)` passes).
7. Audit check passes (after item 5 repair): `sqlite3 <DB> "SELECT count(*) FROM audit_events;"` returns non-zero (or at least shows table exists and grows after mutation).

Note: Smoke check 5 does NOT verify memory persistence (requires DB content verification); smoke check 4 verifies contracts; smoke check 7 verifies audit repair (requires item 5 implementation).

## Rollback to previous artifact

Proposed rollback procedure (design only; not tested against deployed host):

```bash
# rollback.sh — proposed (NOT EXECUTED)
set -euo pipefail
DB_PATH="${MNEMOSYNE_DB_PATH:-$HOME/.hermes/mnemosyne/mnemosyne.db}"
BACKUP_DIR="/srv/backups/mnemosyne/<pre_update_timestamp>"
# Verify backup exists and integrity passes
sqlite3 "${BACKUP_DIR}/mnemosyne_pre_update.db" "PRAGMA integrity_check;"
# Stop current service/container
systemctl stop mnemosyne-hermes || docker stop mnemosyne-hermes || true
# Restore DB (atomic replacement preferred; or copy)
cp "${BACKUP_DIR}/mnemosyne_pre_update.db" "$DB_PATH"
# Restart previous image/service (digest or binary reference must be preserved from pre-update state)
# Verify rollback integrity
sqlite3 "$DB_PATH" "PRAGMA integrity_check;"
# Verify representative records (memory count, namespace, provider identity)
sqlite3 "$DB_PATH" "SELECT namespace FROM memories LIMIT 1;"
```

Note: Rollback requires the previous binary/image reference to be preserved. The current repository does NOT store the deployed binary digest; this must be captured before any redeploy (see item 2 gaps).

## PID pressure and unrelated failing cron integrations (separate tracking)

Based on `.auto/checks.sh` and `.auto/checks_full.sh`:

- `.auto/checks.sh` runs `rustfmt` check, `cargo test --release --lib storage::`, `cargo test --release --lib mcp::`, `cargo check --release --locked --features full,distributed --bin mnemosyne`.
- `.auto/checks_full.sh` (not fully read) may include additional integration tests.
- No Python integration tests (plugin tests, MCP stdio tests, Python CI) are included in these checks.
- PID pressure (process identification / process limits) and unrelated failing cron integrations are NOT addressed by the Python pivot; they must be tracked separately.

Proposed separate tracking (design, not applied):

```json
{
  "separate_tracking": {
    "pid_pressure": {
      "description": "Process limit / PID exhaustion observed on deployed host",
      "evidence_path": ".auto/evidence/pid_pressure_YYYY-MM-DD.json (to be created if observed)",
      "investigation_required": true,
      "action_taken": "none (this deliverable excludes unrelated infrastructure overhaul)"
    },
    "failing_cron_integrations": {
      "description": "Unrelated cron jobs failing independently of memory provider",
      "evidence_path": ".auto/evidence/cron_failures_YYYY-MM-DD.json (to be created if observed)",
      "investigation_required": true,
      "action_taken": "none (this deliverable excludes unrelated infrastructure overhaul)"
    },
    "python_ci_coverage": {
      "status": "designed (see item 4); not executed",
      "evidence_path": ".auto/deliverables/item_07_deploy_repro.md"
    }
  }
}
```

Note: These are tracking entries only; no investigation or fix is performed in this deliverable.

## Gaps (explicitly labeled unknown; no manufacturing)

- Deployed container digest / binary sha256: UNKNOWN (empty `sha256` in `.auto/evidence/claim_001.json`).
- TrueNAS deployment details: UNKNOWN (`docs/OPERATIONS.md` not fully read; redeploy procedure proposed, not verified against deployed infrastructure).
- Whether rollback to previous artifact has been tested: UNKNOWN (only design proposed).
- Whether smoke checks currently pass on deployed host: UNKNOWN (requires binary and service availability; no deployed check executed).
- Whether `MNEMOSYNE_BIN` binary responds to `--version`: UNKNOWN (not executed in this session).
- Whether PID pressure or failing cron integrations have been observed: UNKNOWN; tracked separately.
- Whether backup before redeploy has been executed: UNKNOWN (item 1 backup verification is planning only; no fresh backup produced).

## Deliverable artifacts

- `.auto/deliverables/item_07_deploy_repro.md` (this file)
- `docs/plans/item_07_deploy_repro.md` (mirror)
- Proposed `redeploy_trueNAS.sh`, `rollback.sh`, `verify_install.sh` (design only, not applied, not executed).
- Separate tracking proposal (`separate_tracking` JSON design) for PID/crons.
