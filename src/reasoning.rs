//! Outcome-aware reasoning memories.
//!
//! Reasoning memories are deliberately separate from ordinary facts at the
//! metadata level.  They record a small, evidence-backed strategy learned
//! from a completed task, or a guardrail distilled from a failed task.  The
//! source trajectory remains a normal memory/provenance record; hidden model
//! reasoning is never required or stored.

use crate::error::{MnemosyneError, Result};
use crate::types::{MemoryId, MemoryNote, SearchResult};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Version of the structured reasoning-memory extraction contract.
pub const REASONING_EXTRACTION_SCHEMA_VERSION: &str = "reasoning-experience.v1";
/// ReasoningBank found that a small number of distilled items is preferable
/// to copying an entire trajectory into long-term memory.
pub const MAX_REASONING_ITEMS: usize = 3;

/// Outcome supplied by an objective verifier, reviewer, or explicit caller.
/// `Uncertain` is intentional: an unverified task must not become a trusted
/// success recipe merely because an LLM produced a confident summary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskOutcome {
    Success,
    Failure,
    Uncertain,
}

impl TaskOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Failure => "failure",
            Self::Uncertain => "uncertain",
        }
    }
}

/// Whether an item describes a reusable successful approach or a pitfall to
/// avoid.  Failure items are rendered as guardrails and are never treated as
/// verified facts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasoningLessonKind {
    Strategy,
    Guardrail,
}

impl ReasoningLessonKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Strategy => "strategy",
            Self::Guardrail => "guardrail",
        }
    }
}

/// One item returned by the reasoning extractor.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtractedReasoningItem {
    pub title: String,
    pub description: String,
    pub content: String,
    pub lesson_kind: ReasoningLessonKind,
    pub applicability: String,
    pub confidence: f32,
    pub evidence_quote: String,
    pub source_role: String,
}

/// Strict LLM output contract for reasoning-memory extraction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReasoningExtraction {
    pub schema_version: String,
    pub items: Vec<ExtractedReasoningItem>,
}

impl ReasoningExtraction {
    /// Validate bounds, outcome polarity, and verbatim role-bound evidence.
    pub fn validate(
        &self,
        messages: &[crate::session_extract::SessionMessage],
        outcome: TaskOutcome,
    ) -> Result<()> {
        if self.schema_version != REASONING_EXTRACTION_SCHEMA_VERSION {
            return Err(MnemosyneError::ValidationError(format!(
                "unsupported reasoning extraction schema version: {}",
                self.schema_version
            )));
        }
        if self.items.len() > MAX_REASONING_ITEMS {
            return Err(MnemosyneError::ValidationError(format!(
                "too many reasoning items; maximum is {}",
                MAX_REASONING_ITEMS
            )));
        }
        let source = messages
            .iter()
            .map(|message| message.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        for item in &self.items {
            validate_text(&item.title, "reasoning title", 200)?;
            validate_text(&item.description, "reasoning description", 600)?;
            validate_text(&item.content, "reasoning content", 2_000)?;
            validate_text(&item.applicability, "reasoning applicability", 500)?;
            validate_text(&item.evidence_quote, "reasoning evidence", 2_000)?;
            validate_role(&item.source_role)?;
            validate_confidence(item.confidence)?;
            if !source.contains(&item.evidence_quote) {
                return Err(MnemosyneError::ValidationError(
                    "reasoning evidence quote is not present in source trajectory".into(),
                ));
            }
            if !messages.iter().any(|message| {
                message.role.eq_ignore_ascii_case(&item.source_role)
                    && message.text.contains(&item.evidence_quote)
            }) {
                return Err(MnemosyneError::ValidationError(
                    "reasoning evidence quote does not belong to declared source role".into(),
                ));
            }
            match (outcome, item.lesson_kind) {
                (TaskOutcome::Success, ReasoningLessonKind::Guardrail) => {
                    return Err(MnemosyneError::ValidationError(
                        "successful tasks may only produce strategy items".into(),
                    ));
                }
                (TaskOutcome::Failure, ReasoningLessonKind::Strategy) => {
                    return Err(MnemosyneError::ValidationError(
                        "failed tasks may only produce guardrail items".into(),
                    ));
                }
                _ => {}
            }
        }
        Ok(())
    }
}

/// Durable metadata for one completed trajectory.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReasoningExperience {
    pub id: String,
    pub namespace: crate::types::Namespace,
    pub source_memory_id: MemoryId,
    pub task_summary: String,
    pub outcome: TaskOutcome,
    pub verifier: String,
    pub confidence: f32,
    pub outcome_evidence: String,
    pub created_at: DateTime<Utc>,
}

impl ReasoningExperience {
    pub fn validate(&self) -> Result<()> {
        validate_text(&self.id, "reasoning experience id", 128)?;
        validate_text(&self.task_summary, "reasoning task summary", 2_000)?;
        validate_text(&self.verifier, "reasoning verifier", 128)?;
        validate_text(&self.outcome_evidence, "reasoning outcome evidence", 2_000)?;
        if !self.created_at.timestamp_millis().is_positive() {
            return Err(MnemosyneError::ValidationError(
                "reasoning experience timestamp must be positive".into(),
            ));
        }
        validate_confidence(self.confidence)
    }
}

/// A fully materialized reasoning item plus its lesson polarity.
#[derive(Debug, Clone)]
pub struct ReasoningMemory {
    pub memory: MemoryNote,
    pub lesson_kind: ReasoningLessonKind,
    pub title: String,
    pub description: String,
    pub applicability: String,
}

impl ReasoningMemory {
    pub fn validate(&self) -> Result<()> {
        validate_text(&self.title, "reasoning title", 200)?;
        validate_text(&self.description, "reasoning description", 600)?;
        validate_text(&self.applicability, "reasoning applicability", 500)?;
        validate_text(&self.memory.content, "reasoning memory content", 2_000)?;
        if !self
            .memory
            .tags
            .iter()
            .any(|tag| tag == "reasoning_strategy")
        {
            return Err(MnemosyneError::ValidationError(
                "reasoning memory must carry the reasoning_strategy tag".into(),
            ));
        }
        Ok(())
    }
}

/// Search result with reasoning-specific metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReasoningSearchHit {
    pub result: SearchResult,
    pub experience_id: String,
    pub outcome: TaskOutcome,
    pub lesson_kind: ReasoningLessonKind,
    pub title: String,
    pub description: String,
    pub applicability: String,
}

/// Result of the LLM-backed reasoning learning operation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReasoningLearningResult {
    pub source_memory_id: MemoryId,
    pub experience_id: String,
    pub item_ids: Vec<MemoryId>,
    pub extraction_status: ReasoningExtractionStatus,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "status")]
pub enum ReasoningExtractionStatus {
    Succeeded,
    FailedRetryable { error: String },
}

fn validate_text(value: &str, name: &str, max: usize) -> Result<()> {
    if value.trim().is_empty() || value.chars().count() > max {
        return Err(MnemosyneError::ValidationError(format!(
            "{} must contain 1..={} characters",
            name, max
        )));
    }
    Ok(())
}

fn validate_confidence(value: f32) -> Result<()> {
    if !value.is_finite() || !(0.0..=1.0).contains(&value) {
        return Err(MnemosyneError::ValidationError(
            "confidence must be between 0 and 1".into(),
        ));
    }
    Ok(())
}

fn validate_role(role: &str) -> Result<()> {
    if matches!(role, "user" | "assistant" | "system" | "tool") {
        Ok(())
    } else {
        Err(MnemosyneError::ValidationError(
            "reasoning source_role must be user, assistant, system, or tool".into(),
        ))
    }
}
