#!/usr/bin/env bash
# Install a released Mnemosyne binary without a Rust toolchain.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/juanmackie/mnemosyne-hermes/main/install.sh | bash
#   ./install.sh --version 2.3.2 --bin-dir "$HOME/.local/bin"
#
# Source builds remain available from a checkout with:
#   ./scripts/install/install.sh --skip-api-key --no-mcp
set -euo pipefail

REPOSITORY="${MNEMOSYNE_REPOSITORY:-juanmackie/mnemosyne-hermes}"
VERSION="${MNEMOSYNE_VERSION:-latest}"
BIN_DIR="${MNEMOSYNE_BIN_DIR:-${HOME}/.local/bin}"
FROM_SOURCE=0

usage() {
  cat <<'EOF'
Mnemosyne release installer

Options:
  --version VERSION  Install a release tag (default: latest)
  --bin-dir DIR      Install into DIR (default: ~/.local/bin)
  --from-source      Use scripts/install/install.sh from a local checkout
  --help             Show this help

Environment:
  MNEMOSYNE_REPOSITORY  GitHub owner/repository override
  MNEMOSYNE_VERSION      Release tag override
  MNEMOSYNE_BIN_DIR      Install directory override
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || { echo "--version requires a value" >&2; exit 2; }
      VERSION="$2"
      shift 2
      ;;
    --bin-dir)
      [[ $# -ge 2 ]] || { echo "--bin-dir requires a value" >&2; exit 2; }
      BIN_DIR="$2"
      shift 2
      ;;
    --from-source)
      FROM_SOURCE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$FROM_SOURCE" == "1" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -x "$SCRIPT_DIR/scripts/install/install.sh" ]]; then
    exec "$SCRIPT_DIR/scripts/install/install.sh" --skip-api-key --no-mcp --bin-dir "$BIN_DIR"
  fi
  echo "--from-source requires a Mnemosyne source checkout" >&2
  exit 1
fi

case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:amd64) ASSET="mnemosyne-linux-x86_64" ;;
  Linux:aarch64|Linux:arm64) ASSET="mnemosyne-linux-aarch64" ;;
  Darwin:x86_64|Darwin:amd64) ASSET="mnemosyne-macos-x86_64" ;;
  Darwin:arm64|Darwin:aarch64) ASSET="mnemosyne-macos-aarch64" ;;
  *)
    echo "Unsupported platform: $(uname -s) $(uname -m). Use --from-source on a supported Rust host." >&2
    exit 1
    ;;
esac

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "tar is required" >&2; exit 1; }

if [[ "$VERSION" == "latest" ]]; then
  RELEASE_URL="https://github.com/${REPOSITORY}/releases/latest/download"
else
  VERSION="${VERSION#v}"
  RELEASE_URL="https://github.com/${REPOSITORY}/releases/download/v${VERSION}"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ARCHIVE="${ASSET}.tar.gz"
curl --fail --silent --show-error --location \
  "${RELEASE_URL}/${ARCHIVE}" --output "$TMP_DIR/$ARCHIVE"
curl --fail --silent --show-error --location \
  "${RELEASE_URL}/checksums.txt" --output "$TMP_DIR/checksums.txt"

expected="$(awk -v name="$ARCHIVE" '$2 == name { print $1; exit }' "$TMP_DIR/checksums.txt")"
[[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || {
  echo "No valid checksum found for ${ARCHIVE}" >&2
  exit 1
}
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$TMP_DIR/$ARCHIVE" | awk '{print $1}')"
else
  actual="$(shasum -a 256 "$TMP_DIR/$ARCHIVE" | awk '{print $1}')"
fi
[[ "$actual" == "$expected" ]] || {
  echo "Checksum verification failed for ${ARCHIVE}" >&2
  exit 1
}

tar -xzf "$TMP_DIR/$ARCHIVE" -C "$TMP_DIR"
[[ -f "$TMP_DIR/mnemosyne" ]] || { echo "Release archive is missing mnemosyne" >&2; exit 1; }
mkdir -p "$BIN_DIR"
install -m 0755 "$TMP_DIR/mnemosyne" "$BIN_DIR/mnemosyne"

echo "Installed Mnemosyne ${VERSION} to ${BIN_DIR}/mnemosyne"
echo "Configure Hermes with: hermes config set memory.provider mnemosyne"
