# Live verification checklist (post-swap)

Status: **NOT RUN on the live gateway.** Everything below that touches the live
symlink, the Hermes venv or the gateway is a separately authorised action
(plan decision D4). This file is the checklist to execute when that authorisation
is given, plus the evidence already gathered without touching live state.

## Already proven (no live state touched)

| Check | Command | Result |
|---|---|---|
| Snapshot is the upstream artifact | `scripts/vendor-provider-sync.sh <site-packages>/hermes_memory_provider` | 7/7 `same` before the local patches; patched files listed in `PATCHES.md` |
| Drift gate works (positive) | `python tests/test_vendored_provider.py` | 3 checks passed |
| Drift gate works (negative) | append a byte to a vendored file | gate fails; `DIFFERS audit.py` from the sync script |
| One registration, correct id | `python tests/test_provider_loader.py` | exactly 1 provider, `name == "mnemosyne"` |
| Bare venv (no engine) | same test, engine import blocked | module imports, `is_available() is False`, reason names the pin, `doctor` exits 1 |
| Engine present | same test, engine importable | `is_available() is True` |
| Engine/installer never diverges silently | `scripts/engine-parity-check.sh <venv>` | 81/81 engine files match the wheel RECORD; stray file → exit 1; modified file → exit 1 |
| Installer never writes first | `./install.sh --dry-run --hermes-home "$(mktemp -d)"` | plan printed; `HERMES_HOME` left empty (no symlink, no config) |
| Installer refuses blind runs | `./install.sh --venv … </dev/null` | exit 1, "refusing to install without confirmation" |

Not covered by the above: anything that requires the real gateway process, the
real symlink or the real memory database.

## Checklist (requires explicit authorisation)

Run in order; stop at the first failure and record it rather than continuing.

0. **Engine parity (D6).** Before swapping anything, prove the installed engine
   is the pinned artifact — the review suspected local engine patches
   (`hardening.py`, `hotness.py`, `memory_diff_audit.py`) that the wheel does not
   ship. On the dev host there is exact parity and none of those files exist, so
   re-run this on the live host and reconcile any drift (upstream it, reinstall
   the pin, or record the divergence explicitly):
   ```bash
   scripts/engine-parity-check.sh "$HERMES_VENV"   # expect PARITY, exit 0
   ```

1. **Confirm the target and the rollback.** Record `$HERMES_HOME`, the resolved
   venv, the current symlink target and the current DB path:
   ```bash
   readlink -f "$HERMES_HOME/plugins/mnemosyne"
   grep -n "provider" "$HERMES_HOME/config.yaml"
   ```
   Rollback = re-point the symlink at its previous target and restore the config
   copy `install.sh`/`hermes config set` leaves behind.

2. **Install** (uv, no pip needed):
   ```bash
   ./install.sh --dry-run        # read the plan: venv, symlink, DB path
   ./install.sh --yes
   ```
   Expected: symlink → `integrations/hermes-provider/hermes_memory_provider`;
   `import mnemosyne.core.beam` succeeds **and** `mnemosyne.__file__` is the
   engine's, not this repo's `src/mnemosyne`.

3. **Doctor in the gateway venv** (not a fresh CLI shim):
   ```bash
   "$HERMES_VENV/bin/python" -m hermes_cli.main mnemosyne doctor --no-fix
   ```
   Expected: exit 0. If the engine is missing, it must exit non-zero and name the
   install command — never "available ✓" with silent no-ops.

4. **Restart the gateway.** Provider modules are cached in `sys.modules` for the
   life of the process; a running gateway keeps executing the old module, and a
   one-shot CLI probe will still look healthy. Record the restart in the log.

5. **Prefetch returns real rows on live data.** Start a session, send one turn,
   and confirm the injected `<memory-context>` block contains memories from the
   intended store — not an empty block.

6. **One `sync_turn` lands in the intended store.**
   ```bash
   hermes mnemosyne stats
   hermes mnemosyne inspect --query "<the phrase you just used>"
   ```
   Expected: exactly one new memory for that turn, in the store named by
   `MNEMOSYNE_DB_PATH` / `hermes mnemosyne doctor` output.

7. **No double-writes with the standalone surface.** If the lite CLI
   (`src/mnemosyne`) is also pointed at the same file, confirm it refuses
   (`not a Mnemosyne store`, exit 1) rather than adding `content_lower` to the
   engine's bank. This is the storage-safety contract; verify it on the live
   bank rather than trusting the unit tests.

8. **Compression checkpoint exercised.** Trigger a context compression in a long
   session and confirm it completes. Note the audit finding F3: this provider
   does **not** override `on_pre_compress`, so it contributes nothing to the
   summary prompt by design — the check is that nothing breaks, not that text is
   contributed.

9. **Tool count and tool calls.** Log the tool schemas the manager exposes
   (`get_tool_schemas()` length) and make one real tool call
   (`mnemosyne_memory_search`). Expected: a JSON string result; on an
   uninitialized provider, `{"status": "memory_unavailable", ...}` rather than an
   exception or an empty string.

10. **Keep the lite distribution OUT of the engine venv.** The lite surface is
    `mnemosyne-lite` / `mnemosyne_lite` now; if it (or any other `mnemosyne`
    distribution) is installed in the gateway venv, `import mnemosyne` may
    resolve to the wrong package. Confirm:
    ```bash
    "$HERMES_VENV/bin/python" -c "import mnemosyne, sys; print(mnemosyne.__file__)"
    ```
    Expected: the engine's path, not this repo's `src/`.

11. **Reconcile the review host's `text_learning.py`.** The 2026-09-18 review host
    had an extra `text_learning.py` next to the provider; it is absent from the
    wheel RECORD and unreferenced by the provider or the engine. Either delete it
    or add it to the patch layer (`PATCHES.md` + `VENDORED_FROM.json`).

11. **Record the outcome** in this file: date, host, commands, observed rows, and
    any deviation. A swap is not "verified" until steps 3-6 have real output.
