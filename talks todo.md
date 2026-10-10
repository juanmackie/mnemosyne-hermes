# Agent-harness talk TODO — mnemosyne-hermes

Draft implementation list based on `C:/Users/juanm/Documents/GitHub/AGENT_HARNESS_PROJECT_REVIEW.md`. No item below is marked complete; phase 1 only created this plan. Preserve the provider/lite separation, keyless operation, fail-loud storage behavior, and the vendored upstream contract.

## Current state and scope

- Provider audit, bounded audit-health counters, prefetch diagnostics, consolidation lifecycle status and an offline prefetch evaluation already exist. Improve those surfaces instead of adding another telemetry store or benchmark runner.
- `integrations/hermes-provider/hermes_memory_provider/` is a byte-hashed upstream snapshot. Any edit inside it requires, in the same change, a `# LOCAL PATCH:` marker, `PATCHES.md` entry and updated `VENDORED_FROM.json` hash/bytes/lines. `tests/test_vendored_provider.py` is a required gate. Do not refactor the snapshot for file size alone.
- Do not add a separate model key, capture memory/query text in telemetry, use live gateway credentials, or change Hermes tool-name/config contracts without a specific compatibility need.

## 1. Correlate existing audit events for important provider operations

- [ ] Inspect current audit coverage for memory write/update/forget, explicit recall, automatic prefetch/injection, and consolidation completion/failure. Reuse `AuditLog`, its existing `event_id`/session/source metadata, and existing diagnostics counters; do not duplicate audit-health reporting.
- [ ] Add a bounded opaque `operation_id` only to paths that currently cannot be joined across their start/result audit events. Put it in existing `metadata_json` where possible; otherwise use the documented vendored patch process. Record action, outcome, duration and relevant memory ID/reference only. Never record query text, memory content, raw tool/delegation transcripts, secrets or model prompts.
- [ ] Add tests proving correlated success/failure events share the ID, metadata has a fixed size/allowlist, failed audit writes remain visible through existing health counters, and audit failure does not change memory mutation behavior.

**Acceptance criteria:** each selected operation can be followed by opaque ID from start to terminal event where both events are emitted; diagnostics still expose aggregate write/read failures; tests demonstrate that event records contain no query or memory body; existing calls without an operation ID remain compatible.

**Planned checks after implementation:** `python tests/test_provider_audit.py`; `python -m pytest tests/test_provider_inspection.py tests/test_provider_evidence.py -q`; `python tests/test_vendored_provider.py`; `bash scripts/checks.sh` (includes repository gates).

## 2. Evaluate downstream memory usefulness under context limits

- [ ] Extend the existing public-prefetch evaluation cases with a small held-out task set that includes a needed fact, a distractor, a topic switch, an explicit correction/retirement, and a no-match case. Score whether required evidence is surfaced, forbidden/stale facts are excluded, and output remains within configured context limits.
- [ ] Keep the current latency and synthetic-context grading. Report task-level recall/forbidden-fact rates next to latency; label fixtures as synthetic, record the corpus/eval version and provider source hashes, and state that this does not establish live gateway or semantic/paraphrase quality.
- [ ] Add a regression case showing memory correction/retirement prevents the stale fact from appearing in prefetch while historical audit evidence remains available.

**Acceptance criteria:** all held-out fixtures have declared required/forbidden labels before the run; report includes per-case outcome and aggregate recall/forbidden-fact/context-budget metrics; an intentional wrong/empty/stale fixture proves grader discrimination; no live gateway, private memory corpus, or API key is needed; existing cache remains off by default unless the new task metrics meet an explicitly documented threshold.

**Planned checks after implementation:** `python -m pytest tests/test_provider_eval.py tests/test_provider_prefetch.py tests/test_provider_lifecycle.py -q`; run the existing offline eval command and inspect its JSON/HTML output; `bash scripts/checks.sh`.

## 3. Defer provider extraction unless maintenance evidence warrants a seam

- [ ] **Deferred.** Do not split the large upstream provider module just because of its size. Existing hook/tool registration and prefetch-source seams already allow extension, while extraction would increase divergence and vendor-sync burden.
- [ ] Reopen only after a repeated maintenance defect or upstream sync conflict is documented and traced to a repository-owned adaptation. First prototype the seam outside the hash-controlled snapshot. If a snapshot edit is necessary, follow the complete local-patch/manifest/audit contract and explain why an external adapter cannot own it.

**Acceptance criteria if reopened:** one named maintenance problem and caller are documented; no second provider registration or tool-name/config behavior is introduced; engine-backed contract tests pass; provider snapshot hashes/patch inventory agree; upstream drift and update gates remain green. Otherwise keep deferred.

