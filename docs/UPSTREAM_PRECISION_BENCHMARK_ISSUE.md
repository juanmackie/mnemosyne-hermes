# Draft upstream issue: Qdrant retrieval adapter rejects the current filters argument

**Status:** verified against the current public `main` source on 2026-10-13; draft only. No issue has been submitted.

**Suggested title:** `Qdrant hybrid-search async_retrieve rejects the retrieval mode's filters keyword`

## Problem

The retrieval mode calls `memory.async_retrieve(..., filters=filters)` for every non-empty query. The current `HybridSearchMemoryProvider.async_retrieve()` override accepts `query`, `k`, `user_id`, and `query_timestamp`, but not `filters`. Python therefore raises `TypeError` before the bundled Qdrant retrieval baseline can return results.

The base provider interface already declares the optional `filters` keyword. Even when the Qdrant adapter does not advertise filter support and the mode computes `filters = None`, it still passes `filters=None` in the call. The override must accept the keyword to remain compatible with the interface and caller.

## Minimal reproduction

```python
import asyncio
from memory_bench.memory.hybrid_search import HybridSearchMemoryProvider

# Bypass client setup: argument binding fails before the method body runs.
provider = object.__new__(HybridSearchMemoryProvider)
asyncio.run(provider.async_retrieve("query", k=1, filters=None))
```

On the current implementation this fails with an unexpected-keyword-argument error for `filters` before Qdrant retrieval starts.

## Suggested fix

Add `filters: dict | None = None` to the override signature. If the adapter intentionally does not support provider-neutral filters, accept and ignore `None`, and either reject non-`None` filters clearly or implement their mapping. Keep `retrieve()` and `async_retrieve()` signatures aligned with `MemoryProvider`.

## Primary-source evidence

- [`RetrievalMode.async_answer()` passes `filters=filters`](https://github.com/vectorize-io/agent-memory-benchmark/blob/main/src/memory_bench/modes/retrieval.py#L27-L43), including when the value is `None`.
- [`MemoryProvider` defines the optional filter in its interface](https://github.com/vectorize-io/agent-memory-benchmark/blob/main/src/memory_bench/memory/base.py#L39-L52).
- [`HybridSearchMemoryProvider.async_retrieve()` omits the filter parameter](https://github.com/vectorize-io/agent-memory-benchmark/blob/main/src/memory_bench/memory/hybrid_search.py#L103-L105).

This is a directly verified interface mismatch in current source. It is independent of the Mnemosyne recall evaluation and does not imply that the public Qdrant benchmark row was successfully run in the supplied findings.
