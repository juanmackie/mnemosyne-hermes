# Source-grounded memory review

[Anthropic's components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)
(November 6, 2026) motivates a small addition to the existing provider: retain
useful lessons with evidence, distinguish hypotheses from tested results, and
review notes against originals. Search remains available alongside summaries;
always-loaded context stays small. Salience is left to the agent rather than
restricted to recording failures. The article's wiki and dreaming experiments
do not establish that every memory system needs either feature.

## Run a bounded review

After installing this revision, use the existing Hermes CLI registration:

```bash
hermes mnemosyne review --session-id YOUR_SESSION --limit 20
```

Without `--id`/`--evidence`, this prints JSON and performs no model calls. It opens the resolved provider
store read-only, using the same config/environment precedence as inspection.
It refuses missing, foreign or corrupt stores instead of creating one.
The command does not construct the engine or register an LLM backend.

The default selection includes the requested session's session-scoped records
and global records. The default session ID is `hermes_default`; pass the actual
session ID when reviewing a different session. A local operator can explicitly
select `--all-sessions`. That option may expose private material from other
sessions; it is never implied by a source or lineage reference.

The scan covers at most the newest 200 visible working/episodic records, using
the existing evidence projection. It does not cover BEAM banks or fetch Hermes
transcripts. `scanned` and `scan_truncated` describe that scan;
`candidate_count` counts candidates within it, and `shown` and
`candidates_truncated` describe the output cap (`--limit` accepts 1–100).
An empty queue means no mechanical signals in that selection, not that every
memory is correct or that the entire store was checked.

| Signal | What to investigate |
| --- | --- |
| `source_unavailable` | No usable stored transcript reference or visible original ID. Locate evidence before treating the note as established. |
| `metadata_unreadable` | Metadata cannot be projected as a bounded JSON object. |
| `originals_unavailable` | Some summary originals cannot be read in this selection, have been removed, or exceed projection limits. |
| `content_requires_full_read` | Content exceeds the 4,000-character projection; retrieve the original before judging it. |
| `exact_duplicate` | Another scanned record has equal complete text after case/whitespace normalization. Inspect both scopes and histories before proposing a merge. |

Candidates contain IDs and evidence references rather than another copy of the
memory text. `duplicate_of` includes both ID and store to disambiguate rows.
`original_ids` contains at most 20 visible IDs and has its own truncation flag.
Truncated content is never compared for duplicates. References are stored,
unauthenticated claims; their presence does not establish accuracy. Records
without signals are omitted even if their claims might be false or stale.

## Check one note against explicit source evidence

The semantic mode makes **one optional Hermes model call** and returns a
correction proposal. It never changes the note:

```bash
hermes mnemosyne review --session-id YOUR_SESSION --id MEMORY_ID --evidence /path/outside-repo/excerpt.json
```

Use your host's session search/export to obtain the original messages. Supply a
UTF-8 JSON excerpt in chronological order; preserve speaker roles and include
relevant corrections and qualifications, rather than selecting only a quote
that agrees with the note. Keep real transcripts outside this repository.
For example (synthetic data):

```json
{
  "session_id": "YOUR_SESSION",
  "message_id_namespace": "internal",
  "messages": [
    {"id": "7", "role": "user", "content": "The cave exit is east."}
  ]
}
```

This mode requires both `--id` and `--evidence`. It accepts one complete memory
of at most 4,000 characters, with the same session/global visibility rules as
inspection. `--limit` belongs to the mechanical queue, not source checking.
The excerpt accepts at most 100,000 UTF-8 bytes, 20 messages, 4,000 characters per
message and 24,000 content characters overall. Invalid, oversized or incomplete
inputs are rejected, not silently truncated. IDs must be unique strings and
roles must be `user`, `assistant` or `tool`.

If the note has a usable stored source reference, its session, message ID and
namespace must match the excerpt; include that original message even when later
messages correct it. Legacy references without a namespace are refused rather
than assigned one. With no usable reference, the source association is explicitly
`operator_selected`. Files and stored references are not authenticated; matching
identifiers does not prove that a supplied excerpt is an original transcript.
The command does not open or discover Hermes transcript databases.

The existing `HermesAuxLLMBackend` routes the request through Hermes' configured
auxiliary compression model and existing authentication. No second key or new
model registry is introduced. This optional mode fails loudly if Hermes or its
model cannot return usable output; ordinary keyless memory and mechanical
review remain available. It does not fall back to an unrelated model service.

The JSON result contains `original`, `original_sha256`, `evidence_sha256`, source
association, citations, and one of three verdicts:

- `supported`: no replacement.
- `contradicted`: a proposed replacement, with exact source quotes.
- `insufficient`: no replacement; missing evidence is not treated as a conflict.

Only a contradiction may include a replacement. Each citation must refer to a
supplied message and contain an exact substring from it. Invalid schemas,
unknown IDs, invented quotes and empty required citations fail without a
proposal. The prompt treats memory and transcript contents as untrusted data
and asks the model to distinguish assertions, guesses, conditions and chronology.
That guidance is not a guarantee of resistance to adversarial content.

`applied` is always false. Exact-quote checking proves a quote occurs in the
excerpt; it cannot establish entailment, source authenticity, completeness or
factual truth. A model can cite a real quote and still interpret it incorrectly.
Inspect the evidence and the full proposed note before deciding to edit through
existing provider tools. Re-read the current memory before applying any change:
the original hash identifies the input, not a lock or an automatic conflict check.
No automated apply command or recurring job is added.

## Close the loop on an explicit user correction

Mark the check as a correction trigger when the user has explicitly corrected a
remembered fact:

```bash
hermes mnemosyne review --session-id YOUR_SESSION --id MEMORY_ID \
  --evidence /path/outside-repo/excerpt.json --correction \
  --correction-message-id 7
```

`--correction` requires both `--id` and `--evidence`, and the excerpt must
contain at least one `user` message. `--correction-message-id` is optional and
must name a user message; the ledger records that ID, not its text. This flag
does not fetch a transcript, invent a correction message or infer that an
ordinary assistant turn was a correction. The excerpt still has to contain the
relevant original and correction messages.

The result is the same proposal-only JSON, plus `trigger`, `review_id`,
`proposal_id` and `ledger` fields. The command appends one review event to
`<db_path>.review-ledger.jsonl` beside the provider store. The event contains
IDs, the session/visibility selector, SHA-256 hashes of the original memory,
supplied evidence and proposed replacement, the verdict, the trigger and a
timestamp. It never stores memory text, transcript text, citation quotes, the
model's reason or the replacement. It is a local diagnostic record, not a second
memory store and not an authority on truth.

A `supported` or `insufficient` verdict is recorded too, with no proposal. That
is how an unsupported correction trigger becomes visible instead of disappearing.

Inspect the proposal and the cited evidence, then record what actually happened:

```bash
hermes mnemosyne review-outcome --proposal PROPOSAL_ID --outcome accepted
hermes mnemosyne review-outcome --proposal PROPOSAL_ID --outcome rejected
hermes mnemosyne review-outcome --proposal PROPOSAL_ID --outcome corrected_again
```

- `accepted`: the change was applied and is still believed correct.
- `rejected`: the proposal was not applied because it was wrong or not useful.
- `corrected_again`: the change was applied, but the result was later corrected.

The latest recorded outcome wins. Applying a proposal remains a separate action
through the existing provider tools; recording an outcome does not edit memory.

`review-outcome` also reads the current memory with the selector recorded at
review time and compares its content hash with the original and replacement
hashes. It reports one observed state:

| Observed | Meaning |
| --- | --- |
| `matches_original` | The note is unchanged; an `accepted` outcome is unverified. |
| `matches_proposal` | The current text exactly matches the proposed replacement. |
| `changed_otherwise` | The note differs from both recorded hashes. |
| `missing` | The note is not found in the recorded selection. |

The outcome event stores that observation and a `source` (`explicit`, or
`observed_rerun` for the automatic supersession below). Warnings are returned
when an outcome conflicts with the observation, but they do not block the
record: the operator can still be right about a state the read-only projection
cannot see.

A new explicit correction also checks the ledger. If its current memory hash
equals a prior proposal's exact replacement hash, that prior proposal was
applied, and if the new review proposes a different replacement, the prior
proposal is marked `corrected_again` with source `observed_rerun`. This is the
only automatic inference: an exact hash match plus a new explicit correction.
It does not edit memory and does not infer acceptance from a silent rerun.

To see what still needs a decision:

```bash
hermes mnemosyne review-pending
```

lists proposals without a recorded outcome, oldest first. The IDs are the ones
`review-outcome` accepts, so losing the original command output does not lose
the feedback loop.

```bash
hermes mnemosyne review-stats
```

prints review, trigger and verdict counts, pending proposals, observed states,
outcome sources, and rates:

- `acceptance_rate = accepted / decided`
- `verified_acceptance_rate = accepted_verified / decided`
- `false_correction_rate = (rejected + corrected_again) / decided`
- `decided = accepted + rejected + corrected_again`

`accepted_verified` counts accepted outcomes whose current memory exactly
matched the proposed replacement; `accepted_unverified` counts the rest. Rates
are `null` until at least one proposal has a recorded outcome. They measure
explicit and evaluated outcomes, not an assumption that an unrecorded proposal
was accepted, and they do not prove the memory is true.

The ledger stays local and is never exported automatically. There is no export
command in this milestone. Deliberately not added: automatic application,
background scanning, transcript discovery, a new tool registration, a schema
change, a recurring model job, or code self-modification. Those need measured
proposal accuracy and a reviewed regression case first.

## Agent workflow

1. Start with a concrete question or task. Recall relevant notes; read injected
   context first. Use live tools for cheaply available current state.
2. Run a review when there is a reason to suspect stale or conflicting notes,
   or when the user requests one. Do not continuously review the entire store.
3. Read a candidate using `hermes mnemosyne inspect --id ID --session-id YOUR_SESSION`.
   Use `hermes mnemosyne history --id ID --session-id YOUR_SESSION` for partial
   recorded history. Look up the original transcript with the host's session
   search, using the reference's session and message namespace, or inspect the
   visible original IDs. Missing evidence remains missing; do not invent it.
4. Test a proposed general rule against available evidence. Save useful lessons
   with the rule, supporting observation/test and limitations in the note text.
   Keep untested explanations explicitly labelled as hypotheses.
5. Correct only evidence-supported errors through the configured provider
   tools. Default exposure includes remember/recall/stats/forget; update and
   validation tools require explicit configuration. Review itself never applies
   changes. A duplicate signal alone is insufficient grounds for deletion. For
   an explicit user correction, run the review with `--correction`, then record
   the outcome after applying or declining the proposal. `review-pending` lists
   proposals that still need a decision.
6. Keep native `MEMORY.md`/`USER.md` anchors short. Use searchable provider
   records for detail and skills for procedures. A read-only Markdown export
   can aid navigation; it remains a projection rather than a second authority.

For example, a useful lesson describes the failed operation, the tested fix,
the command/result that supports it, and where it applies. Do not convert a
single unexplained failure into a universal rule. A caller-supplied
`metadata.source_ref` can aid lookup but cannot authenticate the source.

## YAGNI and the five-step process

1. **Question requirements.** The provider already captures turns, consolidates
   tiers, exposes evidence and budgets injected context. Reimplementing these
   would create competing behavior. Lite remains a separate keyword store.
2. **Delete unnecessary work.** No new wiki database, graph service, tool
   registration, scheduler, API key, schema or model dependency. The outcome
   ledger is an append-only sidecar, not a second memory store.
3. **Simplify.** Reuse the existing read-only evidence reader, source checker,
   Hermes model adapter and configured-tool prompt. Mechanical signals request
   investigation, not automatic decisions. One `--correction` flag is the whole
   new trigger; it does not infer corrections from ordinary turns.
4. **Shorten feedback cycles.** A capped JSON queue supports immediate manual
   inspection. `review-outcome` compares the current memory against the
   recorded hashes, `review-pending` lists decisions that are still open, and
   `review-stats` turns those observations into acceptance and false-correction
   rates without another model call. Local/keyless regression tests cover
   ownership and safety.
5. **Automate last.** No new recurring model jobs. The exact-hash supersession
   observation is automated because it follows from two explicitly recorded
   inputs; the decision to apply or reject a proposal is not. Consider broader
   automation only after a task-specific evaluation shows improved answers,
   acceptable cost and a measured proposal false-correction rate. Existing
   token-free tier consolidation is unchanged and is not advertised as semantic
   verification of notes.

A full wiki, recurring source checking, automatic correction or memory promotion
would need a measured retrieval problem and an evaluation against the simpler
search-plus-review path. This change makes no benchmark improvement claim and
performs no live gateway verification. The ledger measures decisions people and
agents record; it does not establish model accuracy by itself.

## Evaluate before scheduling

`bench/source_check_cases.json` contains eight authored synthetic cases:
incorrect notes, an explicit correction, correct/conditional/negated notes,
missing evidence, an assistant guess and an embedded instruction attempt.
The evaluator records supported-note false proposals, insufficient-evidence
false proposals, successful corrections, judgment errors and per-case output.
It also reports `false_proposal_rate` over the supported and insufficient cases
and `correction_pass_rate` over the contradicted cases, so a review-logic change
can be compared on the same authored set. Expected verdicts and replacement
terms are authored independently of the model; replacement scoring is
intentionally simple and still needs human inspection. These are synthetic
rates, not the local acceptance/false-correction rates that `review-stats`
computes from recorded outcomes.

In an environment where the Hermes auxiliary adapter is usable, run:

```bash
python bench/source_check_eval.py --run-model
```

This makes up to eight calls (one per case, 3,000 output tokens and a 60-second
timeout requested per call). Output is JSON; any failed case exits nonzero.
Use the same Hermes model configuration for comparisons. The production command
and evaluator use the same prompt and citation validator. No live memory store
or private transcript is used by this evaluation.

Unit tests use scripted judgments to validate boundaries and the scorer,
including a deliberately wrong judgment with a real quote. Those tests are not
measurements of model performance. No live model result is claimed for this
revision: Hermes is absent from the development interpreter. A successful run
on this small corpus would be a smoke test, not evidence that unattended
correction is safe. Expand the corpus with independently reviewed, nonprivate
cases before considering automation.
