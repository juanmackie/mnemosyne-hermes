//! Session commit → long-term memory extraction pipeline (design borrowed
//! from OpenViking's session/memory concept).
//!
//! Lifecycle: **Create → Interact → Commit**
//!
//! `commit()` runs in two phases, mirroring the upstream design:
//!
//! 1. **Synchronous**: archive the session messages and return immediately.
//! 2. **Asynchronous** (call [`extract_and_decide`]): generate candidate
//!    memories from the conversation, vector pre-filter similar existing
//!    memories, make per-item dedup decisions (`skip / create / merge /
//!    delete`), and emit a [`MemoryDiff`] audit record of every mutation for
//!    rollback. The write-time turn path uses [`distill_turn`] as a cheap,
//!    deterministic gate and keeps its raw transcript in a separate tier.
//!
//! Candidate generation here is heuristic (preference/decision statement
//! detection) so the pipeline works offline; an LLM extractor can replace
//! [`extract_candidates`] without changing downstream types.

use crate::error::{MnemosyneError, Result};
use crate::types::MemoryId;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::path::Path;

/// One message in a session transcript
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionMessage {
    /// "user" | "assistant"
    pub role: String,
    pub text: String,
}

impl SessionMessage {
    pub fn new(role: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            role: role.into(),
            text: text.into(),
        }
    }
}

/// A candidate memory extracted from a session
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateMemory {
    pub content: String,
    /// Why this was extracted (e.g. "user preference", "decision")
    pub reason: &'static str,
}

/// Per-existing-item dedup decisions (mirrors OpenViking's decision levels)
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "decision", rename_all = "snake_case")]
pub enum DedupDecision {
    /// Candidate is a duplicate of an existing memory — do nothing
    Skip { existing_id: MemoryId },
    /// Merge candidate content into an existing memory
    Merge { existing_id: MemoryId },
    /// Conflicting existing memory should be deleted in favor of candidate
    Delete { existing_id: MemoryId },
}

/// Outcome of resolving one candidate against existing memories
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum CandidateResolution {
    /// Create a brand-new memory
    Create,
    /// Do not create; apply per-item decisions to existing memories only
    ResolveExisting(Vec<DedupDecision>),
    /// Skip entirely (exact duplicate)
    SkipAll(DedupDecision),
}

// ---------------------------------------------------------------------------
// Similarity pre-filter (lexical Jaccard over word sets — no embeddings needed;
// callers with an embedding service can substitute cosine similarity)
// ---------------------------------------------------------------------------

/// Lexical similarity between two texts: Jaccard over lowercased word sets.
pub fn lexical_similarity(a: &str, b: &str) -> f32 {
    let words = |s: &str| -> std::collections::HashSet<String> {
        s.to_lowercase()
            .split(|c: char| !c.is_alphanumeric())
            .filter(|w| w.len() > 2)
            .map(|w| w.to_string())
            .collect()
    };
    let sa = words(a);
    let sb = words(b);
    if sa.is_empty() && sb.is_empty() {
        // Empty token sets are not automatically identical: `Rust` and `Go`
        // would otherwise look like duplicates because both are short. Keep
        // exact normalized text at 1.0 and unrelated short text at 0.0.
        return if normalize_for_dedup(a) == normalize_for_dedup(b) {
            1.0
        } else {
            0.0
        };
    }
    if sa.is_empty() || sb.is_empty() {
        return 0.0;
    }
    let intersection = sa.intersection(&sb).count() as f32;
    let union = (sa.len() + sb.len()) as f32 - intersection;
    intersection / union
}

// ---------------------------------------------------------------------------
// Typed single-pass turn extraction
// ---------------------------------------------------------------------------

/// Version of the structured extraction contract. Bump when fields change.
pub const EXTRACTION_SCHEMA_VERSION: &str = "turn-extraction.v1";
const MAX_CANDIDATES: usize = 32;

/// Deterministic write-time extraction. This gate deliberately uses no model,
/// network, or credential: captured turns are durable even when no candidate
/// is strong enough to become recallable knowledge.
pub fn distill_turn(messages: &[SessionMessage]) -> TurnExtraction {
    let mut candidates = Vec::new();
    for message in messages {
        let role = message.role.to_ascii_lowercase();
        if !matches!(role.as_str(), "user" | "assistant" | "system") {
            continue;
        }
        for sentence in split_source_sentences(&message.text) {
            let lower = sentence.to_ascii_lowercase();
            let kind = if role == "user"
                && PREFERENCE_MARKERS
                    .iter()
                    .any(|marker| lower.contains(marker))
            {
                Some("preference")
            } else if CONSTRAINT_MARKERS
                .iter()
                .any(|marker| lower.contains(marker))
            {
                Some("constraint")
            } else if DECISION_MARKERS.iter().any(|marker| lower.contains(marker)) {
                Some("decision")
            } else if FACT_MARKERS.iter().any(|marker| lower.contains(marker)) {
                Some("fact")
            } else {
                None
            };
            let Some(kind) = kind else { continue };
            candidates.push(ExtractedMemoryCandidate {
                content: sentence.clone(),
                kind: kind.into(),
                confidence: match kind {
                    "constraint" => 0.92,
                    "preference" => 0.90,
                    "decision" => 0.88,
                    _ => 0.82,
                },
                evidence_quote: sentence,
                source_role: role.clone(),
                entities: extract_entities(&message.text),
            });
            if candidates.len() == MAX_CANDIDATES {
                break;
            }
        }
        if candidates.len() == MAX_CANDIDATES {
            break;
        }
    }
    let mut seen = std::collections::HashSet::new();
    candidates.retain(|candidate| seen.insert(normalize_for_dedup(&candidate.content)));
    TurnExtraction {
        schema_version: EXTRACTION_SCHEMA_VERSION.into(),
        candidates,
        response_feedback: None,
    }
}

/// Extract an explicit date bound from a statement without guessing one.
pub fn temporal_valid_until(text: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    let words: Vec<&str> = text.split_whitespace().collect();
    for (index, word) in words.iter().enumerate() {
        if !matches!(word.to_ascii_lowercase().as_str(), "until" | "through") {
            continue;
        }
        let date = words
            .get(index + 1)?
            .trim_matches(|c: char| !c.is_ascii_digit() && c != '-');
        let date = chrono::NaiveDate::parse_from_str(date, "%Y-%m-%d").ok()?;
        return Some(date.and_hms_opt(23, 59, 59)?.and_utc());
    }
    None
}

fn split_source_sentences(text: &str) -> Vec<String> {
    text.split(|c: char| matches!(c, '.' | '\n' | '!' | '?'))
        .map(str::trim)
        .filter(|sentence| sentence.chars().count() > 8)
        .map(ToOwned::to_owned)
        .collect()
}

fn extract_entities(text: &str) -> Vec<ExtractedEntity> {
    let known = [
        "rust",
        "sqlite",
        "libsql",
        "turso",
        "python",
        "typescript",
        "javascript",
        "postgres",
        "postgresql",
        "docker",
        "claude",
        "mnemosyne",
    ];
    let mut entities = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (index, raw) in text.split_whitespace().enumerate() {
        let token = raw.trim_matches(|c: char| !c.is_alphanumeric() && c != '-' && c != '_');
        if token.chars().count() < 3
            || (index == 0 && !known.contains(&token.to_ascii_lowercase().as_str()))
        {
            continue;
        }
        let normalized = token.to_ascii_lowercase();
        let proper = token.chars().next().is_some_and(|c| c.is_ascii_uppercase());
        if !proper && !known.contains(&normalized.as_str()) {
            continue;
        }
        if seen.insert(normalized.clone()) {
            entities.push(ExtractedEntity {
                display_name: token.to_owned(),
                normalized_key: normalized,
                role: "mentioned".into(),
                confidence: 0.75,
            });
        }
        if entities.len() == 16 {
            break;
        }
    }
    entities
}

/// A bounded entity attached to an extracted candidate.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtractedEntity {
    pub display_name: String,
    pub normalized_key: String,
    pub role: String,
    pub confidence: f32,
}

/// A durable fact, preference, constraint, or decision from a completed turn.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtractedMemoryCandidate {
    pub content: String,
    pub kind: String,
    pub confidence: f32,
    pub evidence_quote: String,
    pub source_role: String,
    pub entities: Vec<ExtractedEntity>,
}

/// Explicit response feedback. Generic sentiment is intentionally not enough.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtractedResponseFeedback {
    pub polarity: String,
    pub guidance: String,
    pub applicability: String,
    pub signal: String,
    pub confidence: f32,
    pub evidence_quote: String,
    pub source_role: String,
    pub anchors: Vec<String>,
}

impl ExtractedResponseFeedback {
    /// Conservative promotion gate: generic reactions are not policies.
    pub fn is_actionable(&self) -> bool {
        if self.source_role != "user" {
            return false;
        }
        let guidance = self.guidance.trim().to_ascii_lowercase();
        let quote = self.evidence_quote.trim().to_ascii_lowercase();
        if guidance.is_empty()
            || guidance.contains("the user")
            || guidance.contains("user's personality")
            || guidance.contains("user emotion")
            || guidance.contains("the user feels")
        {
            return false;
        }
        // These are sentiment/task outcomes, not response characteristics.
        // Require the extractor to name a characteristic before promotion.
        let generic = [
            "thanks",
            "thank you",
            "wrong",
            "okay",
            "ok",
            "do better",
            "answer differently",
            "be more accurate",
            "be correct",
            "be helpful",
            "improve",
            "not good",
            "bad response",
            "i dislike this",
        ];
        if generic
            .iter()
            .any(|value| guidance == *value || quote == *value)
        {
            return false;
        }
        let characteristic = [
            "concise",
            "verbose",
            "brief",
            "detailed",
            "bullets",
            "bullet",
            "format",
            "structure",
            "code",
            "diff",
            "example",
            "explain",
            "heading",
            "step",
            "tone",
            "plain text",
            "markdown",
            "length",
        ];
        characteristic.iter().any(|value| guidance.contains(value)) && !self.anchors.is_empty()
    }
}

/// Structured result returned by the turn distiller or an optional extractor.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnExtraction {
    pub schema_version: String,
    pub candidates: Vec<ExtractedMemoryCandidate>,
    pub response_feedback: Option<ExtractedResponseFeedback>,
}

/// Structured status for enhanced turn learning. Raw synchronization succeeds
/// even when the optional derived extraction is retryable.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "status")]
pub enum ExtractionStatus {
    Succeeded,
    FailedRetryable { error: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnLearningResult {
    pub source_memory_id: MemoryId,
    pub derived_ids: Vec<MemoryId>,
    /// Canonical policy memory IDs, populated only for legacy/materialized
    /// policy rows. New extraction returns pending proposal IDs instead.
    pub policy_ids: Vec<MemoryId>,
    pub policy_proposal_ids: Vec<String>,
    pub extraction_status: ExtractionStatus,
}

impl TurnExtraction {
    /// Validate the complete batch before any derived memory is written.
    pub fn validate(&self, messages: &[SessionMessage]) -> Result<()> {
        if self.schema_version != EXTRACTION_SCHEMA_VERSION {
            return Err(MnemosyneError::ValidationError(format!(
                "unsupported extraction schema version: {}",
                self.schema_version
            )));
        }
        if self.candidates.len() > MAX_CANDIDATES {
            return Err(MnemosyneError::ValidationError(
                "too many extracted candidates".into(),
            ));
        }
        let source = messages
            .iter()
            .map(|m| m.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        for candidate in &self.candidates {
            validate_extracted_text(&candidate.content, "candidate content", 2_000)?;
            validate_extracted_text(&candidate.kind, "candidate kind", 64)?;
            if !matches!(
                candidate.kind.as_str(),
                "fact" | "preference" | "constraint" | "decision"
            ) {
                return Err(MnemosyneError::ValidationError(
                    "candidate kind is not supported".into(),
                ));
            }
            validate_extracted_text(&candidate.evidence_quote, "candidate evidence", 2_000)?;
            validate_role(&candidate.source_role)?;
            validate_confidence(candidate.confidence)?;
            ensure_evidence(&source, &candidate.evidence_quote)?;
            ensure_role_evidence(messages, &candidate.source_role, &candidate.evidence_quote)?;
            if candidate.entities.len() > 16 {
                return Err(MnemosyneError::ValidationError(
                    "too many entities for candidate".into(),
                ));
            }
            for entity in &candidate.entities {
                validate_extracted_text(&entity.display_name, "entity display name", 256)?;
                validate_extracted_text(&entity.normalized_key, "entity normalized key", 256)?;
                validate_extracted_text(&entity.role, "entity role", 64)?;
                validate_confidence(entity.confidence)?;
            }
        }
        if let Some(feedback) = &self.response_feedback {
            validate_extracted_text(&feedback.polarity, "feedback polarity", 16)?;
            validate_extracted_text(&feedback.signal, "feedback signal", 32)?;
            validate_extracted_text(&feedback.guidance, "feedback guidance", 1_000)?;
            validate_extracted_text(&feedback.applicability, "feedback applicability", 500)?;
            validate_extracted_text(&feedback.evidence_quote, "feedback evidence", 2_000)?;
            validate_role(&feedback.source_role)?;
            if feedback.anchors.len() > 16 {
                return Err(MnemosyneError::ValidationError(
                    "too many feedback anchors".into(),
                ));
            }
            for anchor in &feedback.anchors {
                validate_extracted_text(anchor, "feedback anchor", 256)?;
            }
            validate_confidence(feedback.confidence)?;
            ensure_evidence(&source, &feedback.evidence_quote)?;
            ensure_role_evidence(messages, &feedback.source_role, &feedback.evidence_quote)?;
            if !matches!(feedback.polarity.as_str(), "prefer" | "avoid") {
                return Err(MnemosyneError::ValidationError(
                    "feedback polarity must be prefer or avoid".into(),
                ));
            }
            if !matches!(
                feedback.signal.as_str(),
                "direct_preference" | "correction" | "dissatisfaction" | "approval"
            ) {
                return Err(MnemosyneError::ValidationError(
                    "feedback signal is not explicit".into(),
                ));
            }
        }
        Ok(())
    }
}

fn validate_extracted_text(value: &str, name: &str, max: usize) -> Result<()> {
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
    if matches!(role, "user" | "assistant" | "system") {
        Ok(())
    } else {
        Err(MnemosyneError::ValidationError(
            "source_role must be user, assistant, or system".into(),
        ))
    }
}
fn ensure_role_evidence(messages: &[SessionMessage], role: &str, quote: &str) -> Result<()> {
    if messages
        .iter()
        .any(|message| message.role.eq_ignore_ascii_case(role) && message.text.contains(quote))
    {
        Ok(())
    } else {
        Err(MnemosyneError::ValidationError(
            "evidence quote does not belong to declared source role".into(),
        ))
    }
}

fn ensure_evidence(source: &str, quote: &str) -> Result<()> {
    if source.contains(quote) {
        Ok(())
    } else {
        Err(MnemosyneError::ValidationError(
            "evidence quote is not present in source turn".into(),
        ))
    }
}

// ---------------------------------------------------------------------------
// Candidate extraction (heuristic, offline-safe)
// ---------------------------------------------------------------------------

/// Sentence-level markers that a user message contains a durable preference
const PREFERENCE_MARKERS: &[&str] = &[
    "i prefer",
    "i like",
    "i always",
    "i never",
    "we prefer",
    "we use",
    "please remember",
    "remember that",
    "keep in mind",
    "from now on",
    "my name is",
];

/// Markers that a user/assistant exchange records a decision or insight
const DECISION_MARKERS: &[&str] = &[
    "we decided",
    "decided to",
    "the approach is",
    "chose ",
    "choosing ",
    "going with ",
    "it turns out",
    "root cause",
    "the fix was",
    "lesson learned",
];

const CONSTRAINT_MARKERS: &[&str] = &[
    "must ",
    "required",
    "cannot ",
    "can't ",
    "do not ",
    "don't ",
    "should not ",
    "avoid ",
    "constraint",
];

const FACT_MARKERS: &[&str] = &[
    " is ",
    " are ",
    " has ",
    " have ",
    " uses ",
    " runs on ",
    "located in ",
    "version ",
];

/// Extract candidate memories from a session transcript.
///
/// Heuristics: scan user messages for preference statements and either role
/// for decision/insight sentences. Each matching sentence becomes a candidate.
pub fn extract_candidates(messages: &[SessionMessage]) -> Vec<CandidateMemory> {
    let mut out = Vec::new();
    for msg in messages {
        let is_user = msg.role.eq_ignore_ascii_case("user");
        for sentence in split_sentences(&msg.text) {
            let lower = sentence.to_lowercase();
            if is_user && PREFERENCE_MARKERS.iter().any(|m| lower.contains(m)) {
                out.push(CandidateMemory {
                    content: sentence.clone(),
                    reason: "user preference",
                });
            } else if DECISION_MARKERS.iter().any(|m| lower.contains(m)) {
                out.push(CandidateMemory {
                    content: sentence.clone(),
                    reason: "decision/insight",
                });
            }
        }
    }
    // Dedup identical candidates, preserving order
    let mut seen = std::collections::HashSet::new();
    out.retain(|c| seen.insert(normalize_for_dedup(&c.content)));
    out
}

fn split_sentences(text: &str) -> Vec<String> {
    text.split(|c: char| c == '.' || c == '\n' || c == '!' || c == '?')
        .map(|s| s.trim())
        .filter(|s| s.len() > 8)
        .map(|s| format!("{}.", s))
        .collect()
}

fn normalize_for_dedup(s: &str) -> String {
    s.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

// ---------------------------------------------------------------------------
// Dedup decisions
// ---------------------------------------------------------------------------

/// Similarity above which a candidate is considered an exact duplicate
pub const SKIP_THRESHOLD: f32 = 0.92;
/// Similarity above which a candidate merges into the best existing match
pub const MERGE_THRESHOLD: f32 = 0.72;

/// Decide how to resolve a candidate against pre-filtered existing memories.
///
/// `existing` is a list of `(id, similarity)` pairs from vector pre-filtering
/// (or lexical fallback), sorted arbitrarily. Decision rules:
///
/// - any similarity ≥ `SKIP_THRESHOLD` → skip (duplicate exists)
/// - best similarity ≥ `MERGE_THRESHOLD` → merge into best match
/// - otherwise → create new
///
/// Deletion is intentionally conservative: only triggered by explicit caller
/// policy via [`resolve_with_policy`] when the existing memory is fully
/// subsumed (similarity ≥ SKIP_THRESHOLD but *older* content is shorter).
pub fn resolve_candidate(
    candidate_content: &str,
    existing: &[(MemoryId, f32)],
) -> CandidateResolution {
    if existing.is_empty() {
        return CandidateResolution::Create;
    }
    // Exact duplicate check first (normalized text equality)
    for (id, sim) in existing {
        if *sim >= SKIP_THRESHOLD {
            return CandidateResolution::SkipAll(DedupDecision::Skip { existing_id: *id });
        }
    }
    let best = existing
        .iter()
        .max_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
    if let Some((id, sim)) = best {
        if *sim >= MERGE_THRESHOLD {
            return CandidateResolution::ResolveExisting(vec![DedupDecision::Merge {
                existing_id: *id,
            }]);
        }
    }
    let _ = lexical_similarity(candidate_content, ""); // keep fn referenced for API stability
    CandidateResolution::Create
}

/// Policy-aware resolution allowing delete decisions for subsumed memories.
///
/// `existing_with_lengths` provides `(id, similarity, existing_content_len)`.
/// If the candidate fully covers an existing memory (similarity ≥
/// `SKIP_THRESHOLD`) *and* the candidate is substantially richer (≥25% longer),
/// the old memory is deleted in favor of creating the new one.
pub fn resolve_with_policy(
    candidate_content: &str,
    existing_with_lengths: &[(MemoryId, f32, usize)],
) -> CandidateResolution {
    let cand_len = candidate_content.len();
    for (id, sim, old_len) in existing_with_lengths {
        if *sim >= SKIP_THRESHOLD {
            if cand_len > *old_len * 5 / 4 {
                return CandidateResolution::ResolveExisting(vec![DedupDecision::Delete {
                    existing_id: *id,
                }]);
            }
            return CandidateResolution::SkipAll(DedupDecision::Skip { existing_id: *id });
        }
    }
    let pairs: Vec<(MemoryId, f32)> = existing_with_lengths
        .iter()
        .map(|(id, s, _)| (*id, *s))
        .collect();
    resolve_candidate(candidate_content, &pairs)
}

// ---------------------------------------------------------------------------
// Memory diff audit log
// ---------------------------------------------------------------------------

/// One recorded operation in a memory diff
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DiffOperation {
    Add {
        id: MemoryId,
        content: String,
        reason: String,
    },
    Update {
        id: MemoryId,
        before: String,
        after: String,
    },
    #[serde(rename = "delete")]
    Delete {
        id: MemoryId,
        deleted_content: String,
    },
}

/// Audit record of all memory changes from one session commit — written to
/// disk so extractions can be reviewed and rolled back.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryDiff {
    /// ISO-8601 timestamp of extraction
    pub extracted_at: String,
    pub operations: Vec<DiffOperation>,
}

impl MemoryDiff {
    pub fn new() -> Self {
        Self {
            extracted_at: Utc::now().to_rfc3339(),
            operations: Vec::new(),
        }
    }

    pub fn summary(&self) -> (usize, usize, usize) {
        let adds = self
            .operations
            .iter()
            .filter(|o| matches!(o, DiffOperation::Add { .. }))
            .count();
        let updates = self
            .operations
            .iter()
            .filter(|o| matches!(o, DiffOperation::Update { .. }))
            .count();
        let deletes = self
            .operations
            .iter()
            .filter(|o| matches!(o, DiffOperation::Delete { .. }))
            .count();
        (adds, updates, deletes)
    }

    /// Persist to `<dir>/memory_diff_<timestamp>.json`
    pub fn write_to_dir(&self, dir: &Path) -> std::io::Result<std::path::PathBuf> {
        std::fs::create_dir_all(dir)?;
        let filename = format!("memory_diff_{}.json", Utc::now().format("%Y%m%d_%H%M%S%3f"));
        let path = dir.join(filename);
        let json = serde_json::to_string_pretty(self)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
        std::fs::write(&path, json)?;
        Ok(path)
    }
}

impl Default for MemoryDiff {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Full pipeline
// ---------------------------------------------------------------------------

/// Result of running the full extraction pipeline for one commit
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractionResult {
    pub candidates_extracted: usize,
    pub created: Vec<MemoryId>,
    pub merged_into: Vec<MemoryId>,
    pub skipped: usize,
    pub deleted: Vec<MemoryId>,
}

/// Run the sync phase of a commit: archive messages to a JSONL file.
///
/// Returns the archive path. Mirrors OpenViking's "archive (sync)" step.
pub fn archive_messages(
    session_id: &str,
    messages: &[SessionMessage],
    archive_dir: &Path,
) -> std::io::Result<std::path::PathBuf> {
    std::fs::create_dir_all(archive_dir)?;
    let path = archive_dir.join(format!("{}_messages.jsonl", sanitize(session_id)));
    let mut out = String::new();
    for m in messages {
        let line =
            serde_json::json!({"role": m.role, "text": m.text, "ts": Utc::now().to_rfc3339()});
        out.push_str(&line.to_string());
        out.push('\n');
    }
    std::fs::write(&path, out)?;
    Ok(path)
}

fn sanitize(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lexical_similarity_identical_and_disjoint() {
        assert!(
            (lexical_similarity("use redis for caching", "use redis for caching") - 1.0).abs()
                < 1e-6
        );
        assert_eq!(
            lexical_similarity("redis caching layer", "quantum entanglement physics"),
            0.0
        );
    }

    #[test]
    fn test_lexical_similarity_partial_overlap() {
        let s = lexical_similarity(
            "we decided to use postgres for storage",
            "we chose postgres storage engine today",
        );
        assert!(s > 0.2 && s < 0.9);
    }

    #[test]
    fn deterministic_distiller_emits_typed_evidence_without_a_model() {
        let messages = vec![
            SessionMessage::new("user", "I prefer Rust for this service until 2030-01-02."),
            SessionMessage::new(
                "assistant",
                "We decided to keep SQLite for the durable log.",
            ),
        ];
        let extraction = distill_turn(&messages);
        assert!(extraction.validate(&messages).is_ok());
        assert_eq!(extraction.candidates.len(), 2);
        assert!(extraction
            .candidates
            .iter()
            .any(|candidate| candidate.kind == "preference"));
        assert!(extraction
            .candidates
            .iter()
            .any(|candidate| candidate.kind == "decision"));
        assert!(extraction
            .candidates
            .iter()
            .all(|candidate| !candidate.entities.is_empty()));
        assert_eq!(
            temporal_valid_until(&messages[0].text)
                .unwrap()
                .date_naive()
                .to_string(),
            "2030-01-02"
        );
    }

    #[test]
    fn test_extract_preferences_from_user_messages() {
        let msgs = vec![
            SessionMessage::new(
                "user",
                "I prefer tabs over spaces. Also I like dark themes.",
            ),
            SessionMessage::new("assistant", "Sure thing."),
        ];
        let cands = extract_candidates(&msgs);
        assert!(cands.iter().all(|c| c.reason == "user preference"));
        assert!(cands.len() >= 2);
    }

    #[test]
    fn test_extract_decisions_from_any_role() {
        let msgs = vec![
            SessionMessage::new(
                "assistant",
                "The fix was to bump the timeout. We decided to retry twice.",
            ),
            SessionMessage::new("user", "great"),
        ];
        let cands = extract_candidates(&msgs);
        assert!(cands.iter().any(|c| c.reason == "decision/insight"));
    }

    #[test]
    fn test_no_candidates_from_smalltalk() {
        let msgs = vec![
            SessionMessage::new("user", "hi there!"),
            SessionMessage::new("assistant", "Hello! How can I help?"),
        ];
        assert!(extract_candidates(&msgs).is_empty());
    }

    #[test]
    fn test_duplicate_candidates_removed() {
        let msgs = vec![
            SessionMessage::new("user", "I prefer vim keybindings."),
            SessionMessage::new("user", "I prefer vim keybindings."),
        ];
        let cands = extract_candidates(&msgs);
        assert_eq!(cands.len(), 1);
    }

    #[test]
    fn test_resolve_skip_on_high_similarity() {
        let id = MemoryId(uuid::Uuid::new_v4());
        let res = resolve_candidate("use redis caching layer", &[(id, 0.95)]);
        assert_eq!(
            res,
            CandidateResolution::SkipAll(DedupDecision::Skip { existing_id: id })
        );
    }

    #[test]
    fn test_resolve_merge_on_medium_similarity() {
        let id = MemoryId(uuid::Uuid::new_v4());
        let res = resolve_candidate("postgres storage choice", &[(id, 0.8)]);
        assert_eq!(
            res,
            CandidateResolution::ResolveExisting(vec![DedupDecision::Merge { existing_id: id }])
        );
    }

    #[test]
    fn test_resolve_create_when_no_similar() {
        let id = MemoryId(uuid::Uuid::new_v4());
        let res = resolve_candidate("totally unrelated topic about kernels", &[(id, 0.1)]);
        assert_eq!(res, CandidateResolution::Create);
        assert_eq!(
            resolve_candidate("anything", &[]),
            CandidateResolution::Create
        );
    }

    #[test]
    fn test_policy_delete_when_subsumed_and_richer() {
        let id = MemoryId(uuid::Uuid::new_v4());
        let short_old = "x".repeat(40);
        let rich_new = format!("{} plus much more detail here", "x".repeat(80));
        let res = resolve_with_policy(&rich_new, &[(id, 0.95, short_old.len())]);
        match res {
            CandidateResolution::ResolveExisting(decisions) => {
                assert_eq!(decisions, vec![DedupDecision::Delete { existing_id: id }]);
            }
            other => panic!("expected delete decision, got {:?}", other),
        }
    }

    #[test]
    fn test_memory_diff_summary_and_serialization_roundtrip() {
        let mut diff = MemoryDiff::new();
        diff.operations.push(DiffOperation::Add {
            id: MemoryId(uuid::Uuid::new_v4()),
            content: "new".into(),
            reason: "user preference".into(),
        });
        diff.operations.push(DiffOperation::Update {
            id: MemoryId(uuid::Uuid::new_v4()),
            before: "old".into(),
            after: "newer".into(),
        });
        diff.operations.push(DiffOperation::Delete {
            id: MemoryId(uuid::Uuid::new_v4()),
            deleted_content: "gone".into(),
        });
        assert_eq!(diff.summary(), (1, 1, 1));
        let json = serde_json::to_string(&diff).unwrap();
        let back: MemoryDiff = serde_json::from_str(&json).unwrap();
        assert_eq!(back.summary(), (1, 1, 1));
    }

    #[test]
    fn test_memory_diff_write_to_disk() {
        let dir = std::env::temp_dir().join(format!("mnemo_diff_test_{}", uuid::Uuid::new_v4()));
        let mut diff = MemoryDiff::default();
        diff.operations.push(DiffOperation::Add {
            id: MemoryId(uuid::Uuid::new_v4()),
            content: "c".into(),
            reason: "test".into(),
        });
        let path = diff.write_to_dir(&dir).unwrap();
        assert!(path.exists());
        let contents = std::fs::read_to_string(path).unwrap();
        assert!(contents.contains("operations"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_archive_messages_writes_jsonl() {
        let dir = std::env::temp_dir().join(format!("mnemo_arch_test_{}", uuid::Uuid::new_v4()));
        let msgs = vec![
            SessionMessage::new("user", "hello"),
            SessionMessage::new("assistant", "hi"),
        ];
        let path = archive_messages("sess/1", &msgs, &dir).unwrap();
        assert!(path.exists());
        let contents = std::fs::read_to_string(path).unwrap();
        assert_eq!(contents.lines().count(), 2);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_turn_extraction_rejects_fabricated_evidence() {
        let extraction = TurnExtraction {
            schema_version: EXTRACTION_SCHEMA_VERSION.into(),
            candidates: vec![ExtractedMemoryCandidate {
                content: "User likes Rust".into(),
                kind: "preference".into(),
                confidence: 0.9,
                evidence_quote: "not in transcript".into(),
                source_role: "user".into(),
                entities: vec![],
            }],
            response_feedback: None,
        };
        let messages = vec![SessionMessage::new("user", "I prefer Rust.")];
        assert!(extraction.validate(&messages).is_err());
    }

    #[test]
    fn test_turn_extraction_accepts_verbatim_role_evidence() {
        let extraction = TurnExtraction {
            schema_version: EXTRACTION_SCHEMA_VERSION.into(),
            candidates: vec![ExtractedMemoryCandidate {
                content: "The project uses Rust".into(),
                kind: "fact".into(),
                confidence: 1.0,
                evidence_quote: "The project uses Rust".into(),
                source_role: "assistant".into(),
                entities: vec![],
            }],
            response_feedback: None,
        };
        let messages = vec![SessionMessage::new("assistant", "The project uses Rust")];
        assert!(extraction.validate(&messages).is_ok());
    }

    #[test]
    fn test_turn_extraction_rejects_out_of_range_confidence() {
        let extraction = TurnExtraction {
            schema_version: EXTRACTION_SCHEMA_VERSION.into(),
            candidates: vec![ExtractedMemoryCandidate {
                content: "Fact".into(),
                kind: "fact".into(),
                confidence: 1.1,
                evidence_quote: "Fact".into(),
                source_role: "user".into(),
                entities: vec![],
            }],
            response_feedback: None,
        };
        assert!(extraction
            .validate(&[SessionMessage::new("user", "Fact")])
            .is_err());
    }

    #[test]
    fn test_extraction_pipeline_counts() {
        // End-to-end heuristic flow without touching storage:
        let msgs = vec![SessionMessage::new("user", "I prefer async APIs.")];
        let candidates = extract_candidates(&msgs);
        assert_eq!(candidates.len(), 1);

        // Existing similar memory => merge decision recorded in result shape
        let existing_id = MemoryId(uuid::Uuid::new_v4());
        let resolution = resolve_candidate(&candidates[0].content, &[(existing_id, 0.85)]);
        let mut result = ExtractionResult {
            candidates_extracted: candidates.len(),
            created: vec![],
            merged_into: vec![],
            skipped: 0,
            deleted: vec![],
        };
        match resolution {
            CandidateResolution::ResolveExisting(decisions) => {
                for d in decisions {
                    if let DedupDecision::Merge { existing_id } = d {
                        result.merged_into.push(existing_id);
                    }
                }
            }
            _ => {}
        }
        assert_eq!(result.merged_into, vec![existing_id]);
    }
}
