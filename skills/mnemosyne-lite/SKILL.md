---
name: mnemosyne-lite
description: Search the local mnemosyne-lite SQLite store and get a few ranked, cited cards instead of reading raw tables. Use when the user asks about past memories ("have we seen this before", "what did we decide", "find notes about ..."), or before any recall call. Also use to create a store with init and summarise it with describe.
---

# mnemosyne-lite: search local memory without reading tables

The `mnemosyne-lite` CLI answers questions from a local SQLite store.
One `recall` returns a handful of ranked cards, each capped per card
(default 500 chars, marked with `…`), however many memories exist.
Prefer it to `list` without a query or dumping tables: those cost tokens
in proportion to the store.

## First: learn the store

```bash
mnemosyne-lite describe
```

This shows the memory count, per-namespace counts, the date range, and
example calls. Run it once per session before searching. `recall --query`
filters by words; `--namespace` scopes to one namespace (exact match).

## Search

```bash
mnemosyne-lite recall --query "<words as the user said them>"
mnemosyne-lite recall --query "<words>" --format cards
mnemosyne-lite recall --query "<words>" --namespace <ns> --max-results 5
mnemosyne-lite list --limit 20
mnemosyne-lite describe
```

Add `--format json` for structured output, `--max-results` for more cards
(max 100), and `--max-chars N` to cap card bodies. `list --format cards`
renders the same capped cards without a `match:` line.

## Reading the output

- The cards header states `shown N of M`. Zero means *none found*,
  not *none exist*: retry with fewer or different words.
- Each card has an id, its namespace, importance, the capped content,
  and a `match:` snippet from the text that matched.
- `…` marks truncation. A card never drops text silently.

## No store yet?

```bash
mnemosyne-lite init
mnemosyne-lite remember --content "<fact>" --namespace <ns>
mnemosyne-lite describe
```

`init` and `remember` may create the store. Every other command refuses
a missing path instead of fabricating an empty database. A foreign file
is refused byte-identically: a mistyped `--db-path` never becomes an
empty store that reports "0 memories".

## MCP

The stdio server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout,
logs on stderr. Four tools: `mnemosyne_memory_search`,
`mnemosyne_memory_remember`, `mnemosyne_prefetch`, `mnemosyne_sync_turn`
(`mnemosyne.recall` / `mnemosyne.remember` are accepted as aliases).
`tools/list` descriptions carry a one-line store summary; `search` and
`prefetch` return ranked `results` plus card `text` with `shown` / `total`.
A failed call returns `isError: true` rather than an empty result, so a
client can tell "no match" from "store broken".
