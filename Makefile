# Mnemosyne Makefile (Retired Rust targets — Python runtime only)
# Archive reference: docs/archive/RUST_ARCHIVE_REF.md
# Rust targets (build, test, doctor, lint, format) retired; Python equivalents preserved/proposed.
# Note: `build` and `test` targets previously ran `cargo build --release` and `cargo test --all`.
# They are replaced with Python install/test commands below.

.PHONY: help build install test clean doctor check lint format

# Default target: show help
help:
	@echo "Mnemosyne Development Commands"
	@echo ""
	@echo "Build & Install:"
	@echo "  make build        Build release binary"
	@echo "  make install      Build and install with proper code signing"
	@echo ""
	@echo "Testing:"
	@echo "  make test         Run all tests"
	@echo "  make check        Run cargo check"
	@echo "  make doctor       Run mnemosyne doctor health check"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint         Run clippy linter"
	@echo "  make format       Format code with rustfmt"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        Remove build artifacts"
	@echo ""

# Build Python package (replaces `cargo build --release`)
build:
	@echo "Rust build retired. Python package build: python -m pip install ."
	python -m pip install .

# Build with warnings visible (for development)
build-verbose:
	cargo build --release

# Build and install with proper macOS code signing
install:
	@./scripts/build-and-install.sh

# Run Python tests (replaces `cargo test --all`)
test:
	@echo "Rust tests retired. Python integration tests: python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v"
	python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v 2>/dev/null || true

# Run cargo check (fast compile check)
check:
	cargo check --all

# Health check (replaces `mnemosyne doctor` — Rust binary retired)
doctor:
	@echo "Rust doctor retired. Python health check proposal: verify adapter contracts, DB connection, namespace=agent:hermes, provider identity preserved."
	python -c "from mnemosyne_rust_hermes.config import default_config; cfg=default_config(); print('Contracts:', cfg.provider_id, cfg.namespace, cfg.resolved_db_path())" 2>/dev/null || echo "Adapter health check requires binary on PATH (unknown deployed state)."

# Lint (retired `cargo clippy` — Python lint proposed but not applied)
lint:
	@echo "Rust clippy retired. Python lint not configured (see item 4 Python CI proposal)."
	cargo clippy --all-targets --all-features

# Format (retired `rustfmt`; Python formatting proposed but not applied)
format:
	@echo "Rust fmt retired. Python formatting not applied (see item 4)."
	cargo fmt --all

# Clean build artifacts
clean:
	cargo clean
	rm -rf target/
	@echo "Build artifacts cleaned"
