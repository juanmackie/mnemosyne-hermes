# Plan: LLM/model inheritance from active Hermes instance

## Context
User referenced `https://hermes-agent.nousresearch.com/docs` (Hermes agent docs). Request: "all LLM uses should be inherited by the active chosen model currently in use within the users hermes instance / not a separate api key". This relates to how the Python code/configures LLM calls (`ANTHROPIC_API_KEY`, model selection, DSPy, `mnemosyne` CLI surfaces).

## User clarifications received
- Surfaces: docs/contracts, Python code, build/config (all 3).
- Meaning: "Read hermes config" — LLM/model settings read from hermes instance config (`~/.hermes/config.yaml` / `.env`) rather than a separate `ANTHROPIC_API_KEY` env/file. Keyless/degraded path preserved when neither present.
- Source reference: `https://hermes-agent.nousresearch.com/docs/user-guide/configuration` — settings in `~/.hermes/config.yaml` (model, provider, timeout), secrets in `.env`, precedence: CLI > `config.yaml` > `.env` > defaults.

## Potential files to inspect/change (pending clarification)
- `SECRETS_MANAGEMENT.md` (priority order, env vs encrypted file)
- `src/orchestration/dspy_modules/test_semantic_metrics.py` (hardcoded `ANTHROPIC_API_KEY` check, `dspy.configure`)
- `.github/workflows/ci.yml` (retired rust; could update for model inheritance references)
- `docs/` (architecture docs referencing LLM/model usage)
- `AGENTS.md` (constraints about secrets and LLM keys)

## Approach (confirmed)
1. Inspect `~/.hermes/config.yaml` and `.env` structure from docs; read current repo references (`SECRETS_MANAGEMENT.md`, `AGENTS.md`, test/code files that reference `ANTHROPIC_API_KEY`).
2. Update docs/contracts (`SECRETS_MANAGEMENT.md`, `AGENTS.md`) to state inheritance: if hermes instance config (`~/.hermes/config.yaml`) defines model/provider, use it; secrets from `.env` or hermes instance take priority over separate repo-level keys; preserve graceful degradation.
3. Update Python surfaces (tests like `test_semantic_metrics.py`, DSPy modules, CLI/config paths) to read from hermes instance config first, then fall back to env/file, then graceful degrade — instead of requiring a separate hardcoded key.
4. Update build/config references (`.github/workflows/ci.yml`, `Makefile.archive`) if any rust-era model references remain, aligning with pure-Python + hermes-config inheritance.
5. Verify: confirm `ANTHROPIC_API_KEY` no longer required when hermes config present; confirm graceful path when neither present; confirm no `.rs` artifacts reintroduced.

## Files to inspect/modify
- `SECRETS_MANAGEMENT.md` (inheritance docs, priority order update)
- `AGENTS.md` (constraints: model/config inheritance, no separate key requirement)
- `src/orchestration/dspy_modules/test_semantic_metrics.py` (hardcoded `ANTHROPIC_API_KEY` / `dspy.configure`)
- `.github/workflows/ci.yml` (retired rust; add model-inheritance reference)
- `Makefile.archive` (doctor/reference lines)
- `docs/archive/RUST_ARCHIVE_REF.md` (optional retirement note if needed)

## Reuse / existing patterns
- `mnemosyne secrets` priority: env > `~/.config/mnemosyne/secrets.age` > OS keychain (`SECRETS_MANAGEMENT.md`).
- Hermes docs: `~/.hermes/config.yaml` defines `model`, `.env` defines keys (`https://hermes-agent.nousresearch.com/docs/user-guide/configuration`).
- Existing graceful-degradation note in `AGENTS.md`: "Local-first and keyless by design: memory must work without cloud API keys or OS keyrings; graceful degradation is required."
- `python.toml` / `pyproject.toml` (optional Python feature; off by default) — no rust dependency.

## Steps
- [x] Read `SECRETS_MANAGEMENT.md` fully; read `test_semantic_metrics.py` and `dspy_modules/` for LLM/config references.
- [ ] Update `SECRETS_MANAGEMENT.md`: add hermes instance config (`~/.hermes/config.yaml`, `.env`) as inheritance layer above/en place of separate `ANTHROPIC_API_KEY`.
- [ ] Update `AGENTS.md`: document that LLM/model usage inherits from active hermes instance; separate API key not required; graceful degradation preserved.
- [ ] Update `test_semantic_metrics.py`: replace unconditional `exit(1)` on missing `ANTHROPIC_API_KEY` with graceful check (read hermes config / `.env` first, degrade gracefully).
- [ ] Update `.github/workflows/ci.yml` / `Makefile.archive`: add comments/model refs aligning with inheritance.
- [ ] Verify: no `.rs` artifacts; python import OK; docs consistent.

## Verification
- Confirm `ANTHROPIC_API_KEY` or hermes instance config resolves correctly without a separate key.
- Confirm `mnemosyne secrets list` / docs reflect inheritance behavior.
