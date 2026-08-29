//! Durable, owner-routed memory change proposals.
//!
//! This workflow is intentionally separate from the process-local ICS
//! presentation queue. A proposal is a review artifact; only an explicit
//! owner decision followed by a base-revision-checked apply can change a
//! canonical memory.

use crate::error::MnemosyneError;
use crate::storage::libsql::{LibsqlStorage, MemoryProposalRecord};
use crate::storage::StorageBackend;
use crate::types::{
    InteractionPolicy, MemoryClass, MemoryId, Namespace, PolicyPolarity, PolicySignalKind,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryProposalStatus {
    Pending,
    Accepted,
    Dismissed,
    Applied,
    Failed,
}

impl MemoryProposalStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Accepted => "accepted",
            Self::Dismissed => "dismissed",
            Self::Applied => "applied",
            Self::Failed => "failed",
        }
    }

    fn parse(value: &str) -> Result<Self, ProposalError> {
        match value {
            "pending" => Ok(Self::Pending),
            "accepted" => Ok(Self::Accepted),
            "dismissed" => Ok(Self::Dismissed),
            "applied" => Ok(Self::Applied),
            "failed" => Ok(Self::Failed),
            other => Err(ProposalError::Invalid(format!(
                "unknown proposal status: {other}"
            ))),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProposalProvenance {
    pub source_memory_ids: Vec<MemoryId>,
    pub evidence_quotes: Vec<String>,
}

impl ProposalProvenance {
    fn validate(&self) -> Result<(), ProposalError> {
        if self.source_memory_ids.is_empty() {
            return Err(ProposalError::Invalid(
                "a proposal requires at least one source memory".into(),
            ));
        }
        if self.evidence_quotes.is_empty()
            || self
                .evidence_quotes
                .iter()
                .any(|quote| quote.trim().is_empty() || quote.chars().count() > 2000)
        {
            return Err(ProposalError::Invalid(
                "a proposal requires non-empty evidence quotes of at most 2000 characters".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryProposal {
    pub id: String,
    pub namespace: Namespace,
    pub target_memory_id: MemoryId,
    pub base_updated_at: String,
    pub before_content: String,
    pub proposed_content: String,
    pub diff_text: String,
    pub source_revisions: Vec<String>,
    pub provenance: ProposalProvenance,
    pub proposer: String,
    pub owner: String,
    pub status: MemoryProposalStatus,
    pub created_at: String,
    pub reviewed_by: Option<String>,
    pub decided_at: Option<String>,
    pub decision_note: Option<String>,
    pub applied_at: Option<String>,
    pub error_message: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InteractionPolicyProposal {
    pub id: String,
    pub namespace: Namespace,
    pub source_memory_id: MemoryId,
    pub source_revision: String,
    pub polarity: PolicyPolarity,
    pub guidance: String,
    pub applicability: String,
    pub signal: PolicySignalKind,
    pub confidence: f32,
    pub anchors: Vec<String>,
    pub evidence_quote: String,
    pub proposer: String,
    pub owner: String,
    pub status: MemoryProposalStatus,
    pub created_at: String,
    pub reviewed_by: Option<String>,
    pub decided_at: Option<String>,
    pub decision_note: Option<String>,
    pub applied_at: Option<String>,
    pub error_message: Option<String>,
}

#[derive(Debug, Error)]
pub enum ProposalError {
    #[error("proposal storage error: {0}")]
    Storage(#[from] MnemosyneError),
    #[error("invalid proposal: {0}")]
    Invalid(String),
}

/// API for creating, reviewing, and applying durable proposals.
#[derive(Clone)]
pub struct ProposalService {
    storage: std::sync::Arc<LibsqlStorage>,
}

impl ProposalService {
    pub fn new(storage: std::sync::Arc<LibsqlStorage>) -> Self {
        Self { storage }
    }

    pub async fn propose_update(
        &self,
        target_memory_id: MemoryId,
        proposed_content: &str,
        proposer: &str,
        owner: &str,
        provenance: ProposalProvenance,
    ) -> Result<MemoryProposal, ProposalError> {
        if proposed_content.trim().is_empty() {
            return Err(ProposalError::Invalid(
                "proposed_content must not be empty".into(),
            ));
        }
        if proposer.trim().is_empty() || owner.trim().is_empty() || owner.trim() == "*" {
            return Err(ProposalError::Invalid(
                "proposer and owner must be non-empty; wildcard owners are not allowed".into(),
            ));
        }
        provenance.validate()?;
        let target = self.storage.get_memory(target_memory_id).await?;
        if target.is_archived {
            return Err(ProposalError::Invalid(
                "archived memories cannot receive proposals".into(),
            ));
        }
        if target.memory_class == MemoryClass::InteractionPolicy {
            return Err(ProposalError::Invalid(
                "interaction policies require their typed, explicit-signal workflow".into(),
            ));
        }
        let mut sources = Vec::with_capacity(provenance.source_memory_ids.len());
        for source_id in &provenance.source_memory_ids {
            let source = self.storage.get_memory(*source_id).await.map_err(|_| {
                ProposalError::Invalid(format!("source memory {source_id} was not found"))
            })?;
            if source.memory_class == MemoryClass::InteractionPolicy {
                return Err(ProposalError::Invalid(
                    "interaction policy rows cannot be proposal evidence".into(),
                ));
            }
            if source.namespace != target.namespace && source.namespace != Namespace::Global {
                return Err(ProposalError::Invalid(format!(
                    "source memory {source_id} is outside the target namespace"
                )));
            }
            sources.push(source);
        }
        for quote in &provenance.evidence_quotes {
            if !sources
                .iter()
                .any(|source| source.content.contains(quote) || source.summary.contains(quote))
            {
                return Err(ProposalError::Invalid(format!(
                    "evidence quote is not present in the supplied source memories: {quote}"
                )));
            }
        }
        let diff_text = line_diff(&target.content, proposed_content);
        let id = uuid::Uuid::new_v4().to_string();
        let record = self
            .storage
            .create_memory_proposal(
                &id,
                &target.namespace,
                &target_memory_id,
                &target.updated_at.to_rfc3339(),
                &target.content,
                proposed_content,
                &diff_text,
                &provenance.source_memory_ids,
                &provenance.evidence_quotes,
                proposer,
                owner,
            )
            .await?;
        Self::from_record(record)
    }

    pub async fn get(&self, id: &str) -> Result<Option<MemoryProposal>, ProposalError> {
        self.storage
            .get_memory_proposal(id)
            .await?
            .map(Self::from_record)
            .transpose()
    }

    pub async fn list(
        &self,
        namespace: Option<&Namespace>,
        status: Option<MemoryProposalStatus>,
        limit: usize,
    ) -> Result<Vec<MemoryProposal>, ProposalError> {
        let status = status.map(MemoryProposalStatus::as_str);
        let records = self
            .storage
            .list_memory_proposals(namespace, status, limit)
            .await?;
        records.into_iter().map(Self::from_record).collect()
    }

    pub async fn accept(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<MemoryProposal, ProposalError> {
        let record = self
            .storage
            .decide_memory_proposal(id, reviewer, "accepted", note)
            .await?;
        Self::from_record(record)
    }

    pub async fn dismiss(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<MemoryProposal, ProposalError> {
        let record = self
            .storage
            .decide_memory_proposal(id, reviewer, "dismissed", note)
            .await?;
        Self::from_record(record)
    }

    pub async fn apply(&self, id: &str, reviewer: &str) -> Result<MemoryProposal, ProposalError> {
        let record = self.storage.apply_memory_proposal(id, reviewer).await?;
        Self::from_record(record)
    }

    fn from_record(record: MemoryProposalRecord) -> Result<MemoryProposal, ProposalError> {
        let namespace = serde_json::from_str(&record.namespace)
            .map_err(|e| ProposalError::Invalid(format!("invalid proposal namespace: {e}")))?;
        let target_memory_id = MemoryId::from_string(&record.target_memory_id)
            .map_err(|e| ProposalError::Invalid(format!("invalid proposal target: {e}")))?;
        let source_ids: Vec<String> = serde_json::from_str(&record.source_memory_ids)
            .map_err(|e| ProposalError::Invalid(format!("invalid proposal sources: {e}")))?;
        let source_memory_ids = source_ids
            .iter()
            .map(|id| {
                MemoryId::from_string(id)
                    .map_err(|e| ProposalError::Invalid(format!("invalid source memory: {e}")))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let source_revisions: Vec<String> = serde_json::from_str(&record.source_revisions)
            .map_err(|e| ProposalError::Invalid(format!("invalid source revisions: {e}")))?;
        if source_revisions.len() != source_memory_ids.len() {
            return Err(ProposalError::Invalid(
                "proposal source revision count does not match source count".into(),
            ));
        }
        let evidence_quotes = serde_json::from_str(&record.evidence_quotes)
            .map_err(|e| ProposalError::Invalid(format!("invalid proposal evidence: {e}")))?;
        Ok(MemoryProposal {
            id: record.id,
            namespace,
            target_memory_id,
            base_updated_at: record.base_updated_at,
            before_content: record.before_content,
            proposed_content: record.proposed_content,
            diff_text: record.diff_text,
            source_revisions,
            provenance: ProposalProvenance {
                source_memory_ids,
                evidence_quotes,
            },
            proposer: record.proposer,
            owner: record.owner,
            status: MemoryProposalStatus::parse(&record.status)?,
            created_at: record.created_at,
            reviewed_by: record.reviewed_by,
            decided_at: record.decided_at,
            decision_note: record.decision_note,
            applied_at: record.applied_at,
            error_message: record.error_message,
        })
    }
}

/// Durable owner-review workflow for extracted interaction policies.
///
/// Policies use their own proposal table and remain separate from factual
/// memory proposals and factual recall. Applying one is the only operation
/// that materializes the policy memory.
#[derive(Clone)]
pub struct PolicyProposalService {
    storage: std::sync::Arc<LibsqlStorage>,
}

impl PolicyProposalService {
    pub fn new(storage: std::sync::Arc<LibsqlStorage>) -> Self {
        Self { storage }
    }

    pub async fn propose(
        &self,
        namespace: &Namespace,
        source_memory_id: MemoryId,
        policy: InteractionPolicy,
        proposer: &str,
        owner: &str,
    ) -> Result<InteractionPolicyProposal, ProposalError> {
        let id = uuid::Uuid::new_v4().to_string();
        let record = self
            .storage
            .create_interaction_policy_proposal(
                &id,
                namespace,
                &source_memory_id,
                &policy,
                proposer,
                owner,
            )
            .await?;
        Self::from_record(record)
    }

    pub async fn get(&self, id: &str) -> Result<Option<InteractionPolicyProposal>, ProposalError> {
        self.storage
            .get_interaction_policy_proposal(id)
            .await?
            .map(Self::from_record)
            .transpose()
    }

    pub async fn list(
        &self,
        namespace: Option<&Namespace>,
        status: Option<MemoryProposalStatus>,
        limit: usize,
    ) -> Result<Vec<InteractionPolicyProposal>, ProposalError> {
        let records = self
            .storage
            .list_interaction_policy_proposals(
                namespace,
                status.map(MemoryProposalStatus::as_str),
                limit,
            )
            .await?;
        records.into_iter().map(Self::from_record).collect()
    }

    pub async fn accept(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<InteractionPolicyProposal, ProposalError> {
        Self::from_record(
            self.storage
                .decide_interaction_policy_proposal(id, reviewer, "accepted", note)
                .await?,
        )
    }

    pub async fn dismiss(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<InteractionPolicyProposal, ProposalError> {
        Self::from_record(
            self.storage
                .decide_interaction_policy_proposal(id, reviewer, "dismissed", note)
                .await?,
        )
    }

    pub async fn apply(
        &self,
        id: &str,
        reviewer: &str,
    ) -> Result<InteractionPolicyProposal, ProposalError> {
        Self::from_record(
            self.storage
                .apply_interaction_policy_proposal(id, reviewer)
                .await?,
        )
    }

    fn from_record(
        record: crate::storage::libsql::InteractionPolicyProposalRecord,
    ) -> Result<InteractionPolicyProposal, ProposalError> {
        let namespace = serde_json::from_str(&record.namespace).map_err(|error| {
            ProposalError::Invalid(format!("invalid policy proposal namespace: {error}"))
        })?;
        let source_memory_id =
            MemoryId::from_string(&record.source_memory_id).map_err(|error| {
                ProposalError::Invalid(format!("invalid policy proposal source: {error}"))
            })?;
        let polarity = match record.polarity.as_str() {
            "prefer" => PolicyPolarity::Prefer,
            "avoid" => PolicyPolarity::Avoid,
            value => {
                return Err(ProposalError::Invalid(format!(
                    "invalid policy polarity: {value}"
                )))
            }
        };
        let signal = match record.signal.as_str() {
            "direct_preference" => PolicySignalKind::DirectPreference,
            "correction" => PolicySignalKind::Correction,
            "dissatisfaction" => PolicySignalKind::Dissatisfaction,
            "approval" => PolicySignalKind::Approval,
            value => {
                return Err(ProposalError::Invalid(format!(
                    "invalid policy signal: {value}"
                )))
            }
        };
        let anchors = serde_json::from_str(&record.anchors)
            .map_err(|error| ProposalError::Invalid(format!("invalid policy anchors: {error}")))?;
        Ok(InteractionPolicyProposal {
            id: record.id,
            namespace,
            source_memory_id,
            source_revision: record.source_revision,
            polarity,
            guidance: record.guidance,
            applicability: record.applicability,
            signal,
            confidence: record.confidence,
            anchors,
            evidence_quote: record.evidence_quote,
            proposer: record.proposer,
            owner: record.owner,
            status: MemoryProposalStatus::parse(&record.status)?,
            created_at: record.created_at,
            reviewed_by: record.reviewed_by,
            decided_at: record.decided_at,
            decision_note: record.decision_note,
            applied_at: record.applied_at,
            error_message: record.error_message,
        })
    }
}

fn line_diff(before: &str, after: &str) -> String {
    let mut diff = String::from("--- current\n+++ proposed\n");
    for line in before.lines() {
        diff.push('-');
        diff.push_str(line);
        diff.push('\n');
    }
    for line in after.lines() {
        diff.push('+');
        diff.push_str(line);
        diff.push('\n');
    }
    diff
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_diff_is_scoped_to_before_and_after_content() {
        let diff = line_diff("old", "new");
        assert!(diff.contains("--- current"));
        assert!(diff.contains("-old"));
        assert!(diff.contains("+new"));
    }

    #[test]
    fn provenance_requires_evidence() {
        let result = ProposalProvenance {
            source_memory_ids: Vec::new(),
            evidence_quotes: Vec::new(),
        }
        .validate();
        assert!(result.is_err());
    }
}
