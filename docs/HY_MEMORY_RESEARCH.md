# Hy-Memory research: memory-writer evaluation

Research date: 2026-10-17. This note records transferable ideas from
Tencent's Hy-Memory project. It does not claim that the ideas improve
mnemosyne-hermes, and it does not make Hy-Memory a dependency.

## Pinned sources

- Model card: <https://huggingface.co/tencent/Hy-Memory-30B-A3B>
- Framework repository at
  [`f79770cdb098534d2444178347ad5adc7c473135`](https://github.com/Tencent-Hunyuan/Hy-Memory/tree/f79770cdb098534d2444178347ad5adc7c473135)
- Chat extractor:
  [`hy_memory/agent/extractor.py`](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/f79770cdb098534d2444178347ad5adc7c473135/hy_memory/agent/extractor.py)
- Coding extractor:
  [`hy_memory/coding/extractor.py`](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/f79770cdb098534d2444178347ad5adc7c473135/hy_memory/coding/extractor.py)
- Reconciler:
  [`hy_memory/agent/reconciler.py`](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/f79770cdb098534d2444178347ad5adc7c473135/hy_memory/agent/reconciler.py)
- Framework license:
  [`LICENSE`](https://github.com/Tencent-Hunyuan/Hy-Memory/blob/f79770cdb098534d2444178347ad5adc7c473135/LICENSE)
- Model license:
  [`LICENSE`](https://huggingface.co/tencent/Hy-Memory-30B-A3B/blob/main/LICENSE)

Hy-Memory is a memory-writer model and framework. Its published comparison
changes the writer while keeping retrieval, answer model and judge fixed. The
published scores use fixed 20% benchmark subsamples. Those are vendor-reported
results, not results for this repository.

## Transferable principles

1. **Write only supported information.** Keep exact quantities, dates, paths,
   symbols and verification state. Distinguish advice, attempted actions,
   observed results and verified outcomes.
2. **Do not re-ingest context.** Earlier messages can resolve a reference. They
   are not new user testimony. Retrieved or injected context must not become a
   memory by itself.
3. **Separate the session outcome from a reusable lesson.** A coding trace can
   produce a task-specific record and, only when the evidence supports reuse, a
   procedure with its environmental limits. Zero lessons is valid.
4. **Use conservative reconciliation.** Added detail, a changed current state
   and a separate dated occurrence are different cases. Never merge different
   people, owners or banks. Ambiguous chronology must not retire a current
   memory.
5. **Evaluate the writer alone.** Compare writer artifacts from the same traces
   with retrieval and answer settings fixed. Keep the starting bank, model,
   decoding and fixture hashes stable.

## Contradictions found upstream

The source is not internally consistent, so its prompts must not be copied
verbatim:

- `hy_memory/coding/extractor.py` defines a value threshold for reusable
  experiences, but its output instructions also say
  `MANDATORY: You MUST output at least ONE experience`. The local contract
  permits an empty result.
- `hy_memory/agent/reconciler.py` says separate dated occurrences stay separate,
  but a few-shot example merges repeated injuries into one count/timeline. The
  local contract keeps independent dated events separate unless the trace proves
  they are the same occurrence.

The README architecture is not evidence of runtime behavior. The pinned source
must be audited before any production writer change.

## Local fit

The provider already has bounded turn capture, explicit remember/update/forget
tools, approval staging, owner and scope checks, provenance metadata, canonical
history, and a single-flight background consolidation worker. Do not add a
second extractor, reconciler, store or worker.

The first useful seam is evaluation. Existing benchmarks measure prefetch
context and retrieval cutoffs; they do not isolate memory-writing quality. See
`bench/memory_writer_eval.py` and `bench/README.md`.

## Licensing and data

The framework code and the model's own license separately declare Apache-2.0.
If source text or code is copied, keep the license, attribution and modification
notices. The rights to upstream benchmark data and examples are not established
by those licenses. This repository uses authored synthetic fixtures only.

## Limits

- The pinned engine and Hermes are not installed in the development environment
  used for this note. Their current writer prompts and semantic conflict
  decisions were not audited here.
- Lexical scoring can miss a correct paraphrase and can pass a sentence with a
  wrong conclusion. Review disputed cases and use wrong-conclusion controls.
- Character counts are not billed tokens. Latency and token usage are reported
  only when measured.
- Replay through temporary stores checks persistence and visibility. It does
  not prove model quality or live-gateway behavior.
