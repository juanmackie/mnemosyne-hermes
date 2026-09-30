# Contributing

Thanks for taking a look. This repository is small and has a narrow job, so the
rules below are mostly about not breaking the two things that ship.

## What ships

1. **The Hermes provider** — `integrations/hermes-provider/`. A vendored,
   engine-backed memory provider, provider id `mnemosyne`, installed by
   `./install.sh`. This is the product.
2. **The lite surface** — `src/mnemosyne_lite/`. A standalone SQLite keyword
   store with a CLI (`mnemosyne-lite`) and an MCP stdio server. Not a Hermes
   provider.

[README.md](README.md) explains both; [AGENTS.md](AGENTS.md) is the contract for
what must not drift.

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- A Hermes install only if you are working on the provider end to end
  (`hermes-agent >=0.18,<0.20`).
- No API key. Memory works keyless; if a step demands a key, that is a bug.

## Dev setup

```bash
git clone https://github.com/juanmackie/mnemosyne-hermes.git
cd mnemosyne-hermes

uv tool install ruff==0.16.0
uv tool install mypy
uv tool install shellcheck-py
uv tool install pre-commit
pre-commit install
```

The hooks use tools on `PATH` rather than pinned remote hook repos, so a local
run and a CI run use the same executables and nothing is fetched at hook time.

For the lite surface, `pip install -e .` into its own virtualenv. Do **not**
install it into a Hermes venv: the engine owns the `mnemosyne` distribution
name, import package and console script, and the two must not merge.

## Checks

Run these before you open a pull request:

```bash
./test-all.sh                    # provider contract gates + unit tests
bash scripts/checks.sh           # repo gates (version drift)
pre-commit run --all-files       # ruff, ruff format, mypy, shellcheck
```

`./test-all.sh` runs the three provider gates first (they need no engine, no
Hermes and no network), then pytest:

```bash
python tests/test_vendored_provider.py   # snapshot hashes + declared patches
python tests/test_provider_loader.py     # loader contract, engine absent and present
python tests/test_provider_db_path.py    # DB path precedence + doctor checks
```

The clean-user acceptance lane needs real symlinks, so it runs on Linux/macOS
(CI runs it there; on Windows it skips):

```bash
bash scripts/smoke-hermes-onboarding.sh
```

## The vendored snapshot rule

`integrations/hermes-provider/hermes_memory_provider/` is a byte-hashed snapshot
of the provider shipped by `mnemosyne-memory 3.15.1`. Upstream is the source of
truth for that code; this repository owns the plumbing around it.

An edit to a vendored file is a deliberate, reviewable act. The same change must
contain all three of:

1. a `# LOCAL PATCH:` marker at the site, saying why;
2. an entry in `integrations/hermes-provider/PATCHES.md` (file, date, reason,
   behaviour, upstream status);
3. an updated hash, byte count and line count for that file in
   `integrations/hermes-provider/VENDORED_FROM.json`.

`python tests/test_vendored_provider.py` fails if any of those is missing, or if
a file drifted without any of them. Compute the hash the way the wheel does:

```bash
python -c "import base64,hashlib,pathlib; b=pathlib.Path('PATH').read_bytes(); \
print(base64.urlsafe_b64encode(hashlib.sha256(b).digest()).decode().rstrip('='))"
```

The vendored tree is excluded from ruff, mypy and pyright by configuration in
`pyproject.toml` and `integrations/hermes-provider/pyproject.toml`. That is
because it is upstream's code with optional imports that are absent in a bare
venv — not because the checks were inconvenient. Do not remove the exclusions
to make a new file pass; fix the file or add a patch.

Re-vendoring procedure: `scripts/vendor-provider-sync.sh`, then triage per
`PATCHES.md`.

## One version source

`pyproject.toml`, `src/mnemosyne_lite/__init__.py` and the README's
`**Current Version**` line must agree, and the version literal must not appear
anywhere else under `src/mnemosyne_lite/`. `scripts/check_version_drift.sh`
enforces all of that, plus the engine pin in `install.sh`,
`integrations/hermes-provider/pyproject.toml` and `VENDORED_FROM.json`.

## Adding a gate

`scripts/checks.sh` is the registry. A gate that is not listed there is
invisible, so add it in the same change that introduces it.

## Code style

- `ruff` for linting and formatting (line length 100, `E F W I UP B SIM`).
- `mypy` over `src/mnemosyne_lite` and `tests`.
- `shellcheck -S warning` over `install.sh`, `scripts/` and `bench/`.
- Comments explain reasons, invariants and hazards. A `ponytail:` comment marks
  a deliberate shortcut with a known ceiling — name the ceiling and the upgrade
  condition.

## Tests

Add tests where the behaviour lives:

| What you changed | Where the test goes |
| --- | --- |
| Lite CLI behaviour | `tests/test_lite_cli.py` |
| Lite MCP protocol or tools | `tests/test_lite_mcp.py` |
| Storage safety, schema, recall semantics | `tests/test_python_hardening.py`, `tests/test_recall_freshness.py` |
| Provider loader / registration | `tests/test_provider_loader.py` |
| Provider DB path and doctor checks | `tests/test_provider_db_path.py` |
| The vendored snapshot | `tests/test_vendored_provider.py` |

Prefer a test that drives the real path (the CLI through `cli.main(argv)`, the
server through `serve()` with real streams) over one that re-implements it.

## Commits and pull requests

- Describe the work, not the tool. Do not attribute commits to an AI unless you
  were asked to.
- Prefer a branch for non-trivial work. CI runs on pushes to `main` and on pull
  requests.
- Do not commit scratch state: `.dream-rsi/`, `.pi/`, `bench/data/`,
  `bench/log.jsonl`, `bench/last_measure.txt`, `*.egg-info/`.
- If you change a documented contract (a provider id, a tool name, a DB path
  default, a namespace default), update the docs and the CHANGELOG in the same
  change, and say in the PR what a user has to do about it.

## Code of conduct

Be straightforward and kind. Assume the other person is competent and busy.
Disagree about the code, not the person. Harassment or personal attacks are not
welcome here; maintainers may remove comments, commits or contributors that
cross that line.
