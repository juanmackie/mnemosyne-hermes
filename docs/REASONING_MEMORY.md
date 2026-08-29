# Outcome-aware reasoning memory

Mnemosyne can retain small, reusable lessons from completed agent tasks without
storing hidden chain-of-thought.

## Model

A caller supplies an outcome from the strongest available signal:

- `success`: tests, a reviewer, or an explicit completion signal passed;
- `failure`: tests, a reviewer, or a tool failure identified a problem;
- `uncertain`: no reliable verifier was available.

The trajectory is stored as an observable source record. An optional Anthropic
call then distills at most three items:

- successful tasks produce `strategy` items;
- failed tasks produce `guardrail` items;
- uncertain tasks may produce only cautious, low-confidence lessons.

Every item requires a verbatim quote tied to its source role. Hidden model
reasoning is not extracted or persisted.

## Rust API

```rust,no_run
use mnemosyne_core::{MemoryManager, SessionMessage, TaskOutcome};

# async fn example(manager: &MemoryManager) -> mnemosyne_core::Result<()> {
let result = manager.learn_reasoning_experience(
    "Find every matching record",
    &[
        SessionMessage::new("assistant", "The first page looked complete"),
        SessionMessage::new("tool", "Reviewer found an omitted second page"),
    ],
    TaskOutcome::Failure,
    0.95,
    "Reviewer found an omitted second page",
    "reviewer",
).await?;
println!("learned {} reasoning items", result.item_ids.len());
# Ok(())
# }
```

`learn_reasoning_experience` stores the source before contacting the LLM. If
the LLM is unavailable, it returns `FailedRetryable` while retaining the
source trajectory; callers can then use the returned source ID with a custom
retry path. For offline or custom extractors, use
`LibsqlStorage::store_reasoning_experience` with validated
`ReasoningMemoryRecord` values.

## Retrieval

`MemoryManager::recall_reasoning` searches only distilled reasoning items and
caps callers at three results. `recall_for_context` adds one sparse reasoning
channel alongside factual evidence and response guidance. Failure-derived
items are rendered as fallible guardrails and require an applicability check.

The companion tables are additive and registered for both LibSQL and standard
SQLite in migration `023_reasoning_experiences.sql`.
