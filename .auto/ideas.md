# Deferred adapter/retrieval optimization ideas

- stdio transport: `StdioJsonRpcClient` needs robust Windows subprocess response handling (thread reader + queue works but timing-sensitive in `test_script_server_end_to_end`). Not a retrieval regression — adapter contracts preserved.
- retrieval optimization (if benchmark DB integrated): `.auto/data/template.db` exists (1.7MB, bge-small-en-v1.5 provenance). `.auto/evaluate_membench.py` requires binary integration (`target/release/mnemosyne.exe` needs full CLI/MCP server setup) before real MRR can be measured. No adapter code changes needed for retrieval quality — this is external runtime integration.
- adapter build/install verification: `python -m pip install -e integrations/hermes-memory-provider/` passes; adapter entry point `mnemosyne-rust` registered.
