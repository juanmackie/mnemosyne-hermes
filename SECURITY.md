# Security policy

## What this repository is

A Hermes memory-provider distribution. Two things ship:

- **The provider** — `integrations/hermes-provider/`. A vendored, engine-backed
  Hermes memory provider, provider id `mnemosyne`, installed by `./install.sh`.
- **The lite surface** — `src/mnemosyne_lite/`. A standalone SQLite store with a
  CLI and an MCP stdio server. Not a Hermes provider.

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting][report] on this repository.
Please do not open a public issue for anything exploitable.

Include: what you ran, what happened, the version (`mnemosyne-lite --version`,
or `hermes mnemosyne doctor --no-fix` output), your OS, and the smallest
reproduction you have. If the issue is in the memory engine or in the upstream
provider code rather than this repository's plumbing, say so — see below.

[report]: https://github.com/juanmackie/mnemosyne-hermes/security/advisories/new

## Scope

In scope:

- `src/mnemosyne_lite/` — the store, the CLI, the MCP stdio server.
- `install.sh` — the installer and its `--uninstall`/`--purge` paths.
- The gates in `scripts/` and `tests/`.
- The local patches in `integrations/hermes-provider/PATCHES.md`.

Out of scope here, but worth reporting upstream:

- The memory engine (`mnemosyne-memory`) and the upstream
  `hermes_memory_provider` — [AxDSan/mnemosyne][upstream]. This repository
  vendors a byte-hashed snapshot of the latter; a defect that exists in the
  upstream file as shipped belongs upstream. Local patches are listed in
  `PATCHES.md`, and a defect *introduced* by one of those is in scope here.

[upstream]: https://github.com/AxDSan/mnemosyne

## Properties this repository relies on

These are the invariants a report is most likely to break. Please say which one
you think you broke:

- **Keyless.** Memory must work with no cloud API key and no OS keyring. If a
  code path requires one, that is a bug.
- **No secrets in the repo.** This project needs none. A credential in the tree,
  in a log, or in a commit is a vulnerability.
- **Guard before mutate.** The store classifies a database file with reads only
  before any DDL, DML or persistent pragma, and refuses a file that is not a
  lite store while leaving it byte-identical. A path that writes before that
  classification, or that modifies a refused file, is a vulnerability.
- **Fail loud.** A store that cannot be read raises rather than returning an
  empty result. A silent empty result where an error was due is a bug worth
  reporting.
- **The vendored snapshot is hash-gated.** `tests/test_vendored_provider.py`
  must fail on any edit that is not declared in `PATCHES.md` with an updated
  `VENDORED_FROM.json` hash. A way to change the shipped provider without that
  gate firing is a vulnerability.
- **Local-first by default.** Storage and keyword retrieval are local. If a user
  invokes optional model-backed engine features, the engine may send the needed
  prompt/content through the Hermes proxy and model they configured. This repo
  must not add an unconfigured destination or bypass that consent boundary.

## Supported versions

The `main` branch is the supported version. The lite distribution's version is
in `pyproject.toml` (dynamic, read from `mnemosyne_lite.__version__`), and the
CHANGELOG records what changed. Entries up to `2.4.0` describe the retired Rust
product and are not supported.

The Hermes range this provider supports is `>=0.18,<0.20`, and the versions
actually tested are listed in `integrations/hermes-provider/README.md`. Reports
against a Hermes version outside that range are still welcome, but the range is
a contract and not a promise to expand.
