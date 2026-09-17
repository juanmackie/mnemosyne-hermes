#!/usr/bin/env bash
# Python provider install script for mnemosyne-hermes.
# Usage:
#   ./install.sh --repo-url https://github.com/juanmackie/mnemosyne-hermes
#   ./install.sh --setup "https://github.com/juanmackie/mnemosyne-hermes"
# Creates symlink, writes config, verifies binary/link.
set -euo pipefail

REPO_URL="${MNEMOSYNE_REPO_URL:-https://github.com/juanmackie/mnemosyne-hermes}"
SETUP_URL=""
PLUGIN_DIR="${HOME}/.hermes/plugins"
CONFIG_FILE="${HOME}/.hermes/config.yaml"

usage() {
  cat <<'EOF'
Usage: install.sh [--repo-url URL] [--setup URL]
  --repo-url URL   Clone/link from repo URL (default: mnemosyne-hermes repo)
  --setup URL      Equivalent to --repo-url; supports "Setup {REPO URL}" workflow
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --setup) SETUP_URL="$2"; REPO_URL="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "$PLUGIN_DIR"
mkdir -p "$(dirname "$CONFIG_FILE")"

echo "Installing Python provider from: $REPO_URL"

# Symlink the repo source into plugin dir for Python provider access
PLUGIN_LINK="$PLUGIN_DIR/mnemosyne"
if [[ -L "$PLUGIN_LINK" ]]; then rm "$PLUGIN_LINK"; fi
if [[ -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]]; then
  echo "Error: $PLUGIN_LINK exists and is not a symlink. Remove it first." >&2
  exit 1
fi

# Link to current checkout when running locally; otherwise clone
if [[ -f "pyproject.toml" && -d "src" ]]; then
  ln -sf "$(pwd)" "$PLUGIN_LINK"
else
  TMP="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$TMP/repo" || {
    echo "Clone failed from $REPO_URL" >&2
    rm -rf "$TMP"; exit 1
  }
  ln -sf "$TMP/repo" "$PLUGIN_LINK"
fi

# Write basic config
if [[ ! -f "$CONFIG_FILE" ]]; then
  cat > "$CONFIG_FILE" <<EOF
memory:
  provider: mnemosyne
  db_path: ${HOME}/.mnemosyne/mnemosyne.db
EOF
fi

echo "Config written: $CONFIG_FILE"
echo "Plugin symlink: $PLUGIN_LINK -> $(readlink -f "$PLUGIN_LINK")"

# Verify binary/link
if command -v mnemosyne >/dev/null 2>&1; then
  echo "Verified: mnemosyne binary found ($(command -v mnemosyne))"
else
  echo "Note: mnemosyne binary not on PATH. Add ~/.local/bin or install binary."
fi

echo "Python provider setup complete. Run: mnemosyne init"
