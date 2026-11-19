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
4. **Misses and fixes.** A fit miss (source memory not in the top 5) with an
   `alias` field proposes an alias rule: the alias maps to the memory's canonical
   label. The rule applies to every memory in the same label group whose content
   contains the alias, held-out memories included. Each application is a derived
   restated row, `<canonical>: <content>`. It is tagged with `rehearsal_batch`
   and `trust_tier: DERIVED`, and it keeps the memory's id.
5. **Decision.** A batch is kept only if all three hold on the held-out probes:
   hits rise, over-returns do not rise (distractor rows in the top 5, the plan's
   definition), and fabricated-query rows do not rise. A batch with no fixes is
   never kept.

The first real-engine run on the alias population used wrong top-1 as the
over-return proxy. That choice was made before any data, and it is not the
plan's definition. The decision was switched to over-returns after that run.
Both runs are kept in this record.

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

**PrecisionMemBench population** (35 memories, 18 fit, 17 held). Zero fit misses,
so there were no fixes to test. All 35 controls missed, which shows the detector
can see misses. This population has no headroom for rehearsal.

**Alias stress population** (`--population alias`: 4 label groups × 12 memories,
content uses an alias and shared attribute words). This is a constructed case,
not a sample of real use. It has 4 fit misses and 12 proposed alias applications.
Held-out hits rose from 20 to 27 of 27. Held-out over-returns rose from 62 to 88,
and fabricated rows stayed at 0. The batch is rejected. Restating a memory under
its group label adds distractor rows for the other memories in the group, because
the probes share the label and attribute words. A label-based fix cannot separate
them here.

So the Phase 4 gate is not met: no fix improved held-out recall without raising
over-returns, on either population. The machinery runs on the real engine, and
the rejection is a measured result.

Run: `python -m bench.rehearsal.rehearse --output <report.json> [--batch-file <fixes.json>]`
