//! Owner-reviewed project constraint proposals.
//!
//! Constraints are scoped guidance, not ordinary factual memories. A proposal
//! remains inactive until its routed owner approves it. The bootstrap layer
//! reads only approved proposals (plus backwards-compatible, explicit legacy
//! constraint memories).

use crate::error::MnemosyneError;
use crate::storage::libsql::{ConstraintProposalRecord, LibsqlStorage};
use crate::types::{MemoryId, Namespace};
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConstraintStatus {
    Proposed,
    Approved,
    Rejected,
    Superseded,
}

impl ConstraintStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Proposed => "proposed",
            Self::Approved => "approved",
            Self::Rejected => "rejected",
            Self::Superseded => "superseded",
        }
    }

    pub fn parse(value: &str) -> Result<Self, ConstraintError> {
        match value {
            "proposed" => Ok(Self::Proposed),
            "approved" => Ok(Self::Approved),
            "rejected" => Ok(Self::Rejected),
            "superseded" => Ok(Self::Superseded),
            other => Err(ConstraintError::Invalid(format!(
                "unknown constraint status: {other}"
            ))),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConstraintProposal {
    pub id: String,
    pub namespace: Namespace,
    pub text: String,
    pub scope: String,
    pub priority: u8,
    pub valid_until: Option<String>,
    pub source_memory_ids: Vec<MemoryId>,
    pub evidence_quotes: Vec<String>,
    pub proposer: String,
    pub owner: String,
    pub status: ConstraintStatus,
    pub created_at: String,
    pub approved_by: Option<String>,
    pub decided_at: Option<String>,
    pub decision_note: Option<String>,
}

#[derive(Debug, Error)]
pub enum ConstraintError {
    #[error("constraint storage error: {0}")]
    Storage(#[from] MnemosyneError),
    #[error("invalid constraint proposal: {0}")]
    Invalid(String),
}

#[derive(Clone)]
pub struct ConstraintProposalService {
    storage: std::sync::Arc<LibsqlStorage>,
}

impl ConstraintProposalService {
    pub fn new(storage: std::sync::Arc<LibsqlStorage>) -> Self {
        Self { storage }
    }

    pub async fn propose(
        &self,
        namespace: &Namespace,
        text: &str,
        scope: &str,
        priority: u8,
        valid_until: Option<&str>,
        source_memory_ids: Vec<MemoryId>,
        evidence_quotes: Vec<String>,
        proposer: &str,
        owner: &str,
    ) -> Result<ConstraintProposal, ConstraintError> {
        if text.trim().is_empty() || scope.trim().is_empty() {
            return Err(ConstraintError::Invalid(
                "text and scope must not be empty".into(),
            ));
        }
        if proposer.trim().is_empty() || owner.trim().is_empty() || owner.trim() == "*" {
            return Err(ConstraintError::Invalid(
                "proposer and owner must be explicit; wildcard owners are not allowed".into(),
            ));
        }
        let id = uuid::Uuid::new_v4().to_string();
        let record = self
            .storage
            .create_constraint_proposal(
                &id,
                namespace,
                text,
                scope,
                priority,
                valid_until,
                &source_memory_ids,
                &evidence_quotes,
                proposer,
                owner,
            )
            .await?;
        Self::from_record(record)
    }

    pub async fn get(&self, id: &str) -> Result<Option<ConstraintProposal>, ConstraintError> {
        self.storage
            .get_constraint_proposal(id)
            .await?
            .map(Self::from_record)
            .transpose()
    }

    pub async fn list(
        &self,
        namespace: Option<&Namespace>,
        status: Option<ConstraintStatus>,
        limit: usize,
    ) -> Result<Vec<ConstraintProposal>, ConstraintError> {
        let records = self
            .storage
            .list_constraint_proposals(namespace, status.map(ConstraintStatus::as_str), limit)
            .await?;
        records.into_iter().map(Self::from_record).collect()
    }

    pub async fn approve(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<ConstraintProposal, ConstraintError> {
        Self::from_record(
            self.storage
                .decide_constraint_proposal(id, reviewer, "approved", note)
                .await?,
        )
    }

    pub async fn reject(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<ConstraintProposal, ConstraintError> {
        Self::from_record(
            self.storage
                .decide_constraint_proposal(id, reviewer, "rejected", note)
                .await?,
        )
    }

    pub async fn supersede(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<ConstraintProposal, ConstraintError> {
        Self::from_record(
            self.storage
                .supersede_constraint_proposal(id, reviewer, note)
                .await?,
        )
    }

    fn from_record(
        record: ConstraintProposalRecord,
    ) -> Result<ConstraintProposal, ConstraintError> {
        let namespace = serde_json::from_str(&record.namespace).map_err(|error| {
            ConstraintError::Invalid(format!("invalid constraint namespace: {error}"))
        })?;
        let source_ids: Vec<String> =
            serde_json::from_str(&record.source_memory_ids).map_err(|error| {
                ConstraintError::Invalid(format!("invalid constraint sources: {error}"))
            })?;
        let source_memory_ids = source_ids
            .iter()
            .map(|id| {
                MemoryId::from_string(id).map_err(|error| {
                    ConstraintError::Invalid(format!("invalid constraint source ID: {error}"))
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let evidence_quotes = serde_json::from_str(&record.evidence_quotes).map_err(|error| {
            ConstraintError::Invalid(format!("invalid constraint evidence: {error}"))
        })?;
        Ok(ConstraintProposal {
            id: record.id,
            namespace,
            text: record.text,
            scope: record.scope,
            priority: record.priority,
            valid_until: record.valid_until,
            source_memory_ids,
            evidence_quotes,
            proposer: record.proposer,
            owner: record.owner,
            status: ConstraintStatus::parse(&record.status)?,
            created_at: record.created_at,
            approved_by: record.approved_by,
            decided_at: record.decided_at,
            decision_note: record.decision_note,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_round_trips() {
        for status in [
            ConstraintStatus::Proposed,
            ConstraintStatus::Approved,
            ConstraintStatus::Rejected,
            ConstraintStatus::Superseded,
        ] {
            assert_eq!(ConstraintStatus::parse(status.as_str()).unwrap(), status);
        }
    }
}
