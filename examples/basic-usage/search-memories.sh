#!/usr/bin/env bash
#
# Example: Search Memories
#
# `mnemosyne-lite recall` runs a full-text search (SQLite FTS5, ranked by
# BM25) and filters by namespace and importance. It matches tokens, not
# substrings: `al` does not find `alpha`. A query with no letters or digits
# ("%", "_") is searched literally instead. It is keyword search, not
# semantic search.
#
# Usage:
#   ./search-memories.sh [query]

set -e

QUERY="${1:-architecture decision}"

echo "🔍 Searching for: '$QUERY'"
echo ""

echo "=== Basic search ==="
mnemosyne-lite recall --query "$QUERY" --max-results 5

echo ""
echo "=== Importance 7 and up ==="
mnemosyne-lite recall --query "$QUERY" --min-importance 7 --max-results 3

echo ""
echo "=== Search tips ==="
echo "  - Matching is full text: whole words, not substrings"
echo "  - --max-results caps the output (default 10)"
echo "  - --namespace searches one namespace only"
echo "  - 'mnemosyne-lite diagnostics' lists namespaces and counts"
echo "  - --format json prints one JSON array instead of one dict per line"
echo ""
echo "Examples:"
echo "  mnemosyne-lite recall --query \"race condition\" --namespace project:myapp"
echo "  mnemosyne-lite recall --query \"authentication\" --min-importance 8"
