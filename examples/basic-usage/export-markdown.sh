#!/usr/bin/env bash
#
# Example: Dump Memories to a File
#
# The lite CLI has no export command: `list` prints one Python dict per memory,
# so a dump is a redirect. For a copy you can restore, use `mnemosyne-lite
# backup` instead — that writes a real SQLite backup file.
#
# Usage:
#   ./export-markdown.sh [output-file] [namespace]

set -e

OUTPUT_FILE="${1:-memories-export-$(date +%Y%m%d).txt}"
NAMESPACE="${2:-}"

echo "📤 Dumping memories to: $OUTPUT_FILE"
echo ""

if [ -n "$NAMESPACE" ]; then
  echo "Namespace filter: $NAMESPACE"
  mnemosyne-lite list --limit 100 --namespace "$NAMESPACE" >"$OUTPUT_FILE"
else
  echo "No namespace filter (all namespaces)"
  mnemosyne-lite list --limit 100 >"$OUTPUT_FILE"
fi

echo ""
echo "✅ Dump complete!"
echo ""
echo "File: $OUTPUT_FILE"
echo "Bytes: $(wc -c <"$OUTPUT_FILE")"
echo "Memories: $(wc -l <"$OUTPUT_FILE")"
echo ""
echo "Preview:"
head -5 "$OUTPUT_FILE"
echo ""
echo "Use cases:"
echo "  - Keep a human-readable copy of a namespace"
echo "  - Diff two dumps after a cleanup"
echo ""
echo "For a restorable copy of the whole store:"
echo "  mnemosyne-lite backup --output memories.db"
echo "  mnemosyne-lite restore --backup memories.db --yes"
