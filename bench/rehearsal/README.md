# Offline rehearsal (Phase 4)

`rehearse.py` finds recall misses in stored labelled memories and keeps only
alias fixes that generalise to held-out probes. It is deterministic and keyless:
no LLM is called.

## What it does

1. **Probes.** For each memory with a `canonical_name` label, one probe:
   `<canonical_name> <attribute>`. The attribute is the first key token of the
   memory (digit tokens first, then capitalised tokens), excluding the label.
2. **Controls.** Each probe is paired with a probe aimed at a different memory.
   Controls must miss. If they do not, the instrument cannot see misses and the
   result is not valid.
3. **Split.** Memories are assigned to `fit` or `held` by a fixed hash of their
   id.
4. **Misses and fixes.** A fit miss (source memory not in the top 5) proposes one
   derived alias row. The row is tagged with `rehearsal_batch` and
   `trust_tier: DERIVED`, and only single-token attributes qualify.
5. **Decision.** A batch is kept only if all three hold on the held-out probes:
   hits rise, wrong top-1 results do not rise, and fabricated-query rows do not
   rise. A batch with no fixes is never kept.

## What it does not do

- It does not write to any live store. `--batch-file` writes the kept fixes as
  JSON for the owner to review and apply. Applying a batch to a live store needs
  explicit approval.
- It does not run on a schedule. No cron job is installed.
- It does not use an LLM paraphrase. The plan's optional Hermes-proxy paraphrase
  is not implemented.
- Fix types are alias rows only. Importance bumps and graph links from the plan
  are not implemented.

## Measured result

On the PrecisionMemBench labelled population (35 memories, 18 fit, 17 held), the
real engine had zero fit misses. Both held-out hits and wrong top-1 were
unchanged, so there were no fixes to test. All 35 controls missed, which shows
the detector can see misses. This population therefore has no measured headroom
for rehearsal. Acceptance is covered by unit tests, not by a real-engine run
that improved recall.

Run: `python -m bench.rehearsal.rehearse --output <report.json> [--batch-file <fixes.json>]`
