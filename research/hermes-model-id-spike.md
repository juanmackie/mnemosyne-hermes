# Spike: can the memory provider read the active model id?

2026-11-19 · answers step B0 of the Self-Improvement Memory adoption plan.

**Question.** `initialize()` receives `agent_context`, `platform`,
`gateway_session_key` and `hermes_home`, but not the active model. Can a memory
provider read the model id without a new key, a second provider configuration, or
a fragile hack?

**Answer: yes, on the newest published Hermes.** Use
`agent.auxiliary_client._read_main_model()`, guarded, and store `null` when it
fails. Patch P55 does this.

## What was inspected

The `hermes-agent` wheel the repo's own
`scripts/upstream-drift-check.py --latest-hermes` resolves: **0.19.0** (the newest
release on PyPI at the time of the spike, and what the onboarding smoke in
`.github/workflows/upstream-drift.yml` installs). `PATCHES.md` records P34 being
verified against a 0.22-era build, so re-confirm on that build before relying on
this outside CI.

The call path, in the wheel:

1. `agent/agent_init.py` builds `_init_kwargs = {session_id, platform,
   hermes_home, agent_context}` and adds `warning_callback`, `status_callback`,
   `session_title`, `user_id`/`user_id_alt`/`user_name`, `chat_id`/`chat_name`/
   `chat_type`, `thread_id`, `gateway_session_key`, `agent_identity`,
   `agent_workspace`.
2. `agent/memory_manager.py:initialize_all()` injects `hermes_home` if missing
   and calls `provider.initialize(session_id=..., **kwargs)`.
3. Nothing in that set is the model.

## The three options the plan named

| Option | Finding |
| --- | --- |
| A kwarg Hermes already passes | **No.** `agent_init.py` passes session, platform, home, context, callbacks and gateway identity only. |
| The Hermes config file | Works, but alone it is stale: `config.yaml` `model.default` is the persisted default, not the model of a session started with `hermes --model X`. |
| The auxiliary client the provider already uses | **Yes.** `agent.auxiliary_client._read_main_model()` returns the live per-turn runtime override recorded by `set_runtime_main()`, and falls back to `config.yaml` `model.default`; `""` when neither is set. |

The third option is not an extra integration: `agent/plugin_llm.py` already
calls `_read_main_model()` from a different module for exactly this purpose
(recording plugin LLM attribution), so the helper is a cross-module contract
inside Hermes, not a local accident. It needs no key, no OAuth, no network call
and no second provider config.

## What P55 does with it

- `hermes_llm_adapter.active_model_id()` imports the helper lazily, bounds and
  sanitises the value (≤100 characters, no control characters), and returns
  `None` on any exception. The host answer wins; an explicit
  `MNEMOSYNE_MODEL_ID` is the fallback for measurement arms and scripts that
  have no Hermes runtime to ask (the helper returns `""` there). That order
  means a stray export in the launching shell cannot relabel a real session.
- `mnemosyne_remember` stores `metadata.provenance = {model, hermes_version,
  engine_version, recorded_at}`. `hermes_version` and `engine_version` come from
  the provider's existing `cli.detect_hermes_version()` / `cli.engine_version()`,
  cached per initialization.
- Recall rows, `mnemosyne_get` and prefetch injection show one short line.

## Residual risk, stated plainly

- **Private helper.** `_read_main_model` is underscore-private in Hermes. A
  rename or removal makes provenance `model: null` — visible, not wrong. That is
  the property that makes this acceptable: the failure mode is absence.
- **Worker threads.** The runtime override lives in a `contextvars.ContextVar`,
  and Hermes' compatibility mirror for legacy readers only answers on the main
  thread. A write on a worker thread would therefore fall back to the persisted
  `config.yaml` default. Not reachable today: P55 stamps `mnemosyne_remember`
  only, which runs on the turn thread; turn capture (`sync_turn`) is not stamped.
  If stamping is ever extended to turn capture, the model id must be captured on
  the caller's thread and passed down, not re-read in the worker.
- **Live gateway unverified.** This is wheel-source analysis plus engine-free
  tests (`tests/test_provider_provenance.py`).
  `integrations/hermes-provider/LIVE_VERIFICATION.md` is still unexecuted in
  this repository's history; a live run under `hermes --model X` with a
  different `config.yaml` default is the check that would settle it.
