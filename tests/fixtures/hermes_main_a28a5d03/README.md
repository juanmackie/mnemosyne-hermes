# Pinned Hermes host contract extracts

Source commit: `a28a5d03a9fa60418db5f44f3436fa2aa029c8f2` (2026-10-08).
The source URLs and full upstream SHA256 values are recorded in each module.
These MIT-licensed extracts retain the checkpoint method/helper bodies and
SQLite snapshot helper; imports and a small manager constructor make them
runnable without installing Hermes or using the network. Formatting may differ
from upstream. Local artifact hashes are in `manifest.json`.

The checkpoint fixture stubs redaction at its boundary because inputs are
already normalized, synthetic evidence. It verifies evidence handoff and host
failure propagation; it does not exercise production redaction, the complete
compression pipeline, gateway scheduling, or a model request. The backup
fixture executes the host SQLite snapshot helper against a real WAL database
and verifies a restored committed row.

This compatibility fixture does not expand the provider's published supported
release range. Retain the source ledger and upstream license when updating it.
