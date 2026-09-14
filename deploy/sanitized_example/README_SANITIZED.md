# Sanitized Baseline README (Planning Only)

Status: Planning deliverable for item 2 (Python baseline capture).
Not a replacement for `README.md`. Full `README.md` rewrite is proposed in item 8.

## Install (proposed — not executed)

```bash
python -m venv /tmp/baseline_venv
source /tmp/baseline_venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
python -m unittest discover -s integrations/hermes-memory-provider/tests -t . -v
```

Expected: exit 0; entry-point `mnemosyne-rust` registered.

## Contracts verified

- `provider.id` = `mnemosyne-rust`
- `namespace` = `agent:hermes`
- `db_path` resolves
- `MNEMOSYNE_NAMESPACE` = `agent:hermes`
- `MNEMOSYNE_PROVIDER_ID` = `mnemosyne-rust`

Note: Clean-install verification requires deployed binary (`MNEMOSYNE_BIN`) on PATH. Without binary, only adapter contracts can be verified; stdio MCP integration requires binary.
