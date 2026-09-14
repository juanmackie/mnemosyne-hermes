# Deferred adapter/retrieval optimization ideas

- stdio transport: `StdioJsonRpcClient` uses thread-based stdout reader; timing-sensitive on Windows (`test_script_server_end_to_end`). Not a retrieval regression — adapter contracts preserved.
- benchmark integration: `.auto/data/template.db` exists (1.7MB, provenance `bge-small-en-v1.5`). `.auto/evaluate_membench.py` fails with `RuntimeError: unexpected argument '--scope' found` — binary `target/release/mnemosyne.exe` does not support `--scope` argument required by benchmark dataset (`queries_heldout.jsonl`). No adapter code change fixes this; requires binary/runtime integration or benchmark script alignment.
- adapter build/install verification: `python -m pip install -e integrations/hermes-memory-provider/` passes; adapter entry point `mnemosyne-rust` registered.
