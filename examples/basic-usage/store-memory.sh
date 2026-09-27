#!/usr/bin/env bash
#
# Example: Store a Memory
#
# `mnemosyne-lite remember` writes straight to the local SQLite store. No LLM is
# involved: nothing generates a summary, tags or a classification, and no API
# key is needed.
#
# Usage:
#   ./store-memory.sh

set -e

echo "📝 Storing a memory"
echo ""

mnemosyne-lite remember \
  --content "Decided to use Redis for session storage instead of in-memory sessions.
             Rationale: sessions must survive restarts and scale across app servers.
             Trade-offs: an extra dependency (Redis) for a fast, shared store.
             Configuration: 24h TTL, 10-connection pool, reject requests if Redis is down." \
  --importance 8 \
  --namespace default

echo ""
echo "✅ Stored."
echo ""
echo "The command prints the stored memory as a Python dict:"
echo "  {'id': ..., 'content': <first 200 chars>, 'namespace': 'default',"
echo "   'importance': 8, 'success': True}"
echo ""
echo "There is no --format json flag; redirect stdout if you want a file."
echo ""
echo "Try searching for it:"
echo "  mnemosyne-lite recall --query \"session storage\""
echo "  mnemosyne-lite list --limit 5"
