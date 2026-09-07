# Claude Development Guidelines — mnemosyne-hermes

Operational facts for working in this repository. For deep architectural and
workflow context, read [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[AGENT_GUIDE.md](AGENT_GUIDE.md), and [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md)
rather than duplicating their content here.

## Project

Mnemosyne is a memory/storage system exposed as a Hermes server (`mnemosyne
serve`) and an MCP server (`mnemosyne mcp`). The core is Rust; a Python/PyO3
feature is optional and off by default.

## Build & Install

```bash
# Development rebuild (incremental, installs to ~/.local/bin)
./scripts/rebuild-and-update-install.sh

# Production build
./scripts/rebuild-and-update-install.sh --full-release

# First-time source install
./scripts/build-and-install.sh

# Manual source build (pure Rust, no Python toolchain required)
cargo build --release
```

## Test

```bash
# Fast unit tests (no external services)
cargo test --lib

# Full suite
make test            # cargo test --all
./test-all.sh        # comprehensive runner; ./test-all.sh --skip-llm skips LLM tests
cargo test --test ics_integration_test   # ICS integration tests
```

## Check, Lint, Format

```bash
make check     # cargo check --all
make lint      # cargo clippy --all-targets --all-features
make format    # cargo fmt --all
make doctor    # mnemosyne doctor health check
```

## Run

```bash
mnemosyne serve      # Hermes server
mnemosyne mcp        # MCP server
mnemosyne --version
```

## Secrets

API keys are never stored in the repo. Use the built-in secret manager or the
environment:

```bash
mnemosyne secrets init          # first-time interactive setup (age-encrypted file)
mnemosyne secrets set ANTHROPIC_API_KEY   # prompts for the value
mnemosyne secrets list          # names only, never values
export ANTHROPIC_API_KEY=...    # session-scoped alternative
```

Secrets are stored age-encrypted at `~/.config/mnemosyne/secrets.age` by
default. Do not commit `.env*`, connection configs, keys, or tokens.

## Git

- Work on feature/fix branches; do not commit directly to `main`.
- Use descriptive commit messages that describe the work, not the tool.
- Do not attribute commits to AI unless explicitly requested.
