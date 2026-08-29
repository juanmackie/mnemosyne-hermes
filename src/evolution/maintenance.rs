//! Bounded, advisory memory-maintenance reports.
//!
//! Maintenance is deliberately separate from factual mutation. It scans the
//! existing store, records a bounded report, and leaves repairs to explicit
//! proposal or evolution operations.

use crate::error::MnemosyneError;
use crate::storage::libsql::{LibsqlStorage, MaintenanceRunRecord};
use crate::storage::{MemorySortOrder, StorageBackend};
use crate::types::{MemoryId, Namespace};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::Duration;
use thiserror::Error;
use tokio::time::{sleep, timeout};

pub const MAX_ITEMS_PER_RUN: usize = 10_000;
pub const MAX_RETRIES_PER_RUN: usize = 5;
pub const MAX_RUN_DURATION: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MaintenanceKind {
    StaleLinks,
    MissingCitations,
    HealthSummary,
}

impl MaintenanceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::StaleLinks => "stale_links",
            Self::MissingCitations => "missing_citations",
            Self::HealthSummary => "health_summary",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MaintenanceStatus {
    Running,
    Success,
    Failed,
    Timeout,
}

impl MaintenanceStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Success => "success",
            Self::Failed => "failed",
            Self::Timeout => "timeout",
        }
    }

    fn parse(value: &str) -> Self {
        match value {
            "success" => Self::Success,
            "failed" => Self::Failed,
            "timeout" => Self::Timeout,
            _ => Self::Running,
        }
    }
}

#[derive(Debug, Clone)]
pub struct MaintenanceConfig {
    pub kind: MaintenanceKind,
    pub namespace: Option<Namespace>,
    pub item_limit: usize,
    pub retry_limit: usize,
    pub max_duration: Duration,
    pub stale_after_days: i64,
    pub idempotency_key: String,
}

impl MaintenanceConfig {
    pub fn bounded(mut self) -> Result<Self, MaintenanceError> {
        if self.idempotency_key.trim().is_empty() {
            return Err(MaintenanceError::InvalidConfig(
                "idempotency_key must not be empty".into(),
            ));
        }
        if self.stale_after_days < 1 {
            return Err(MaintenanceError::InvalidConfig(
                "stale_after_days must be at least 1".into(),
            ));
        }
        self.item_limit = self.item_limit.clamp(1, MAX_ITEMS_PER_RUN);
        self.retry_limit = self.retry_limit.min(MAX_RETRIES_PER_RUN);
        self.max_duration = self
            .max_duration
            .clamp(Duration::from_millis(1), MAX_RUN_DURATION);
        Ok(self)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MaintenanceFinding {
    pub code: String,
    pub memory_id: Option<MemoryId>,
    pub related_memory_id: Option<MemoryId>,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MaintenanceReport {
    pub run_id: String,
    pub idempotency_key: String,
    pub kind: MaintenanceKind,
    pub namespace: Option<Namespace>,
    pub status: MaintenanceStatus,
    pub attempts: usize,
    pub items_processed: usize,
    pub findings_count: usize,
    pub errors_count: usize,
    pub findings: Vec<MaintenanceFinding>,
    pub error_message: Option<String>,
    pub started_at: String,
    pub completed_at: String,
}

#[derive(Debug, Error)]
pub enum MaintenanceError {
    #[error("maintenance storage error: {0}")]
    Storage(#[from] MnemosyneError),
    #[error("maintenance configuration invalid: {0}")]
    InvalidConfig(String),
    #[error("maintenance run is already in progress: {0}")]
    AlreadyRunning(String),
    #[error("maintenance worker lost its lease: {0}")]
    LeaseLost(String),
}

struct ScanOutput {
    items_processed: usize,
    errors_count: usize,
    findings: Vec<MaintenanceFinding>,
}

/// Executes one maintenance report asynchronously against an existing store.
#[derive(Clone)]
pub struct MaintenanceRunner {
    storage: Arc<LibsqlStorage>,
}

impl MaintenanceRunner {
    pub fn new(storage: Arc<LibsqlStorage>) -> Self {
        Self { storage }
    }

    /// Run in the caller's async task. The scan is bounded by item count and
    /// timeout; a caller can use `spawn` when it must not await the report.
    pub async fn run(
        &self,
        config: MaintenanceConfig,
    ) -> Result<MaintenanceReport, MaintenanceError> {
        let config = config.bounded()?;
        if let Some(existing) = self
            .storage
            .get_maintenance_run(&config.idempotency_key)
            .await?
        {
            self.validate_existing(&existing, &config)?;
            // Terminal records are immutable/idempotent. A running record is
            // handed back to storage so an expired lease can be reclaimed
            // after a process crash; a fresh lease still returns
            // AlreadyRunning below.
            if existing.status != "running" {
                return self.reuse_existing(existing);
            }
        }

        let run_id = uuid::Uuid::new_v4().to_string();
        if !self
            .storage
            .start_maintenance_run(
                &run_id,
                &config.idempotency_key,
                config.kind.as_str(),
                config.namespace.as_ref(),
                config.item_limit,
                config.retry_limit,
                config.max_duration,
                config.stale_after_days,
            )
            .await?
        {
            let existing = self
                .storage
                .get_maintenance_run(&config.idempotency_key)
                .await?
                .ok_or_else(|| {
                    MaintenanceError::InvalidConfig(
                        "maintenance run was not inserted and cannot be recovered".into(),
                    )
                })?;
            self.validate_existing(&existing, &config)?;
            return self.reuse_existing(existing);
        }

        let started_at = Utc::now().to_rfc3339();
        let total_attempts = config.retry_limit + 1;
        let run_deadline = std::time::Instant::now() + config.max_duration;
        let mut attempts_made = 0;
        let mut last_error = None;
        let mut last_status = MaintenanceStatus::Failed;

        for attempt in 1..=total_attempts {
            self.require_lease(&run_id).await?;
            let remaining = run_deadline.saturating_duration_since(std::time::Instant::now());
            if remaining == Duration::ZERO {
                last_status = MaintenanceStatus::Timeout;
                last_error = Some("maintenance run exceeded its total deadline".into());
                break;
            }
            attempts_made = attempt;
            match timeout(remaining, self.scan(&config)).await {
                Ok(Ok(output)) => {
                    // A stale worker may finish a read-only scan after its
                    // lease was reclaimed.  Fence the terminal write so it
                    // cannot publish findings under the new owner.
                    self.require_lease(&run_id).await?;
                    let report = MaintenanceReport {
                        run_id: run_id.clone(),
                        idempotency_key: config.idempotency_key.clone(),
                        kind: config.kind,
                        namespace: config.namespace.clone(),
                        status: MaintenanceStatus::Success,
                        attempts: attempt,
                        items_processed: output.items_processed,
                        findings_count: output.findings.len(),
                        errors_count: output.errors_count,
                        findings: output.findings,
                        error_message: None,
                        started_at: started_at.clone(),
                        completed_at: Utc::now().to_rfc3339(),
                    };
                    let report_json = serde_json::to_string(&report)?;
                    if !self
                        .storage
                        .finish_maintenance_run(
                            &run_id,
                            report.status.as_str(),
                            attempt,
                            report.items_processed,
                            report.findings_count,
                            report.errors_count,
                            Some(&report_json),
                            None,
                        )
                        .await?
                    {
                        return Err(MaintenanceError::LeaseLost(run_id));
                    }
                    return Ok(report);
                }
                Ok(Err(error)) => {
                    self.require_lease(&run_id).await?;
                    last_status = MaintenanceStatus::Failed;
                    last_error = Some(error.to_string());
                }
                Err(_) => {
                    self.require_lease(&run_id).await?;
                    last_status = MaintenanceStatus::Timeout;
                    last_error = Some(format!(
                        "maintenance attempt timed out after {:?}",
                        config.max_duration
                    ));
                }
            }
            if attempt < total_attempts {
                let backoff = Duration::from_millis((attempt as u64) * 25);
                let remaining = run_deadline.saturating_duration_since(std::time::Instant::now());
                if remaining == Duration::ZERO {
                    break;
                }
                sleep(backoff.min(remaining)).await;
            }
        }

        let report = MaintenanceReport {
            run_id: run_id.clone(),
            idempotency_key: config.idempotency_key.clone(),
            kind: config.kind,
            namespace: config.namespace,
            status: last_status,
            attempts: attempts_made,
            items_processed: 0,
            findings_count: 0,
            errors_count: 1,
            findings: Vec::new(),
            error_message: last_error.clone(),
            started_at,
            completed_at: Utc::now().to_rfc3339(),
        };
        let report_json = serde_json::to_string(&report)?;
        if !self
            .storage
            .finish_maintenance_run(
                &run_id,
                report.status.as_str(),
                report.attempts,
                report.items_processed,
                report.findings_count,
                report.errors_count,
                Some(&report_json),
                report.error_message.as_deref(),
            )
            .await?
        {
            return Err(MaintenanceError::LeaseLost(run_id));
        }
        Ok(report)
    }

    /// Detach the bounded report task from an interactive caller.
    pub fn spawn(
        self: Arc<Self>,
        config: MaintenanceConfig,
    ) -> tokio::task::JoinHandle<Result<MaintenanceReport, MaintenanceError>> {
        tokio::spawn(async move { self.run(config).await })
    }

    async fn require_lease(&self, run_id: &str) -> Result<(), MaintenanceError> {
        if self.storage.maintenance_run_lease_active(run_id).await? {
            Ok(())
        } else {
            Err(MaintenanceError::LeaseLost(run_id.to_owned()))
        }
    }

    async fn scan(&self, config: &MaintenanceConfig) -> Result<ScanOutput, MnemosyneError> {
        match config.kind {
            MaintenanceKind::StaleLinks => self.scan_stale_links(config).await,
            MaintenanceKind::MissingCitations => self.scan_missing_citations(config).await,
            MaintenanceKind::HealthSummary => self.scan_health_summary(config).await,
        }
    }

    async fn scan_stale_links(
        &self,
        config: &MaintenanceConfig,
    ) -> Result<ScanOutput, MnemosyneError> {
        let candidates = self
            .storage
            .find_link_decay_candidates(
                config.stale_after_days,
                config.item_limit.saturating_mul(4).min(MAX_ITEMS_PER_RUN),
            )
            .await?;
        let mut output = ScanOutput {
            items_processed: 0,
            errors_count: 0,
            findings: Vec::new(),
        };

        for (source_id, link) in candidates {
            if output.items_processed >= config.item_limit {
                break;
            }
            let source = self.storage.get_memory(source_id).await;
            if let Some(namespace) = &config.namespace {
                if let Ok(source_memory) = &source {
                    if &source_memory.namespace != namespace {
                        continue;
                    }
                } else {
                    // An orphaned endpoint has no surviving namespace, so it
                    // cannot be safely attributed to a scoped report.
                    continue;
                }
            }
            output.items_processed += 1;
            let target = self.storage.get_memory(link.target_id).await;
            let source_missing = source.is_err();
            let target_missing = target.is_err();
            let source_archived = source.as_ref().map(|m| m.is_archived).unwrap_or(false);
            let target_archived = target.as_ref().map(|m| m.is_archived).unwrap_or(false);
            let code = if source_missing || target_missing {
                "orphaned_link"
            } else if source_archived || target_archived {
                "stale_archived_link"
            } else {
                "stale_link"
            };
            output.findings.push(MaintenanceFinding {
                code: code.into(),
                memory_id: (!source_missing).then_some(source_id),
                related_memory_id: (!target_missing).then_some(link.target_id),
                detail: format!(
                    "{} link ({:?}) crossed the {} day staleness threshold",
                    code, link.link_type, config.stale_after_days
                ),
            });
        }
        Ok(output)
    }

    async fn scan_missing_citations(
        &self,
        config: &MaintenanceConfig,
    ) -> Result<ScanOutput, MnemosyneError> {
        let memories = self
            .storage
            .list_memories(
                config.namespace.clone(),
                config.item_limit.saturating_mul(4).min(MAX_ITEMS_PER_RUN),
                MemorySortOrder::Recent,
            )
            .await?;
        let mut output = ScanOutput {
            items_processed: 0,
            errors_count: 0,
            findings: Vec::new(),
        };
        for memory in memories {
            if output.items_processed >= config.item_limit {
                break;
            }
            if memory.memory_class != crate::types::MemoryClass::Knowledge {
                continue;
            }
            if config
                .namespace
                .as_ref()
                .is_some_and(|namespace| namespace != &memory.namespace)
            {
                continue;
            }
            output.items_processed += 1;
            match &memory.provenance {
                None => output.findings.push(MaintenanceFinding {
                    code: "missing_citation".into(),
                    memory_id: Some(memory.id),
                    related_memory_id: None,
                    detail: "active factual memory has no provenance record".into(),
                }),
                Some(provenance) => {
                    if let Err(error) = provenance.validate() {
                        output.errors_count += 1;
                        output.findings.push(MaintenanceFinding {
                            code: "invalid_citation".into(),
                            memory_id: Some(memory.id),
                            related_memory_id: None,
                            detail: error.to_string(),
                        });
                    } else if let Some(source_id) = provenance.source_memory_id {
                        if self.storage.get_memory(source_id).await.is_err() {
                            output.findings.push(MaintenanceFinding {
                                code: "orphaned_citation".into(),
                                memory_id: Some(memory.id),
                                related_memory_id: Some(source_id),
                                detail: "provenance source memory is missing".into(),
                            });
                        }
                    }
                }
            }
        }
        Ok(output)
    }

    async fn scan_health_summary(
        &self,
        config: &MaintenanceConfig,
    ) -> Result<ScanOutput, MnemosyneError> {
        let mut output = ScanOutput {
            items_processed: 0,
            errors_count: 0,
            findings: Vec::new(),
        };
        for (kind, id) in self
            .storage
            .list_text_learning_orphans(config.item_limit)
            .await?
        {
            if output.items_processed >= config.item_limit {
                break;
            }
            if let Some(namespace) = &config.namespace {
                let Some(memory_id) = MemoryId::from_string(&id).ok() else {
                    continue;
                };
                if self
                    .storage
                    .get_memory(memory_id)
                    .await
                    .map(|memory| memory.namespace != *namespace)
                    .unwrap_or(true)
                {
                    continue;
                }
            }
            output.items_processed += 1;
            output.findings.push(MaintenanceFinding {
                code: format!("orphaned_{}", kind),
                memory_id: MemoryId::from_string(&id).ok(),
                related_memory_id: None,
                detail: "text-learning integrity view reported an orphan".into(),
            });
        }
        if output.items_processed >= config.item_limit {
            return Ok(output);
        }

        let remaining = config.item_limit - output.items_processed;
        let memories = self
            .storage
            .list_memories(
                config.namespace.clone(),
                remaining.saturating_mul(4).min(MAX_ITEMS_PER_RUN),
                MemorySortOrder::Recent,
            )
            .await?;
        for memory in memories {
            if output.items_processed >= config.item_limit {
                break;
            }
            if memory.memory_class != crate::types::MemoryClass::Knowledge {
                continue;
            }
            if config
                .namespace
                .as_ref()
                .is_some_and(|namespace| namespace != &memory.namespace)
            {
                continue;
            }
            output.items_processed += 1;
            if self.storage.get_embedding(&memory.id).await?.is_none() {
                output.findings.push(MaintenanceFinding {
                    code: "missing_embedding".into(),
                    memory_id: Some(memory.id),
                    related_memory_id: None,
                    detail: "active factual memory has no embedding".into(),
                });
            }
        }
        Ok(output)
    }

    fn validate_existing(
        &self,
        record: &MaintenanceRunRecord,
        config: &MaintenanceConfig,
    ) -> Result<(), MaintenanceError> {
        let expected_namespace = config
            .namespace
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .map_err(|error| {
                MaintenanceError::InvalidConfig(format!("invalid maintenance namespace: {error}"))
            })?;
        let expected_timeout_ms = config.max_duration.as_millis() as u64;
        if record.job_kind != config.kind.as_str()
            || record.namespace != expected_namespace
            || record.item_limit != config.item_limit
            || record.retry_limit != config.retry_limit
            || record.timeout_ms != expected_timeout_ms
            || record.stale_after_days != config.stale_after_days
        {
            return Err(MaintenanceError::InvalidConfig(
                "idempotency key is already bound to a different maintenance request".into(),
            ));
        }
        Ok(())
    }

    fn reuse_existing(
        &self,
        record: MaintenanceRunRecord,
    ) -> Result<MaintenanceReport, MaintenanceError> {
        if record.status == "running" {
            return Err(MaintenanceError::AlreadyRunning(record.id));
        }
        if let Some(report_json) = record.report_json {
            if let Ok(report) = serde_json::from_str(&report_json) {
                return Ok(report);
            }
        }
        Ok(MaintenanceReport {
            run_id: record.id,
            idempotency_key: record.idempotency_key,
            kind: match record.job_kind.as_str() {
                "stale_links" => MaintenanceKind::StaleLinks,
                "missing_citations" => MaintenanceKind::MissingCitations,
                _ => MaintenanceKind::HealthSummary,
            },
            namespace: record
                .namespace
                .and_then(|value| serde_json::from_str(&value).ok()),
            status: MaintenanceStatus::parse(&record.status),
            attempts: record.attempts,
            items_processed: record.items_processed,
            findings_count: record.findings_count,
            errors_count: record.errors_count,
            findings: Vec::new(),
            error_message: record.error_message,
            started_at: record.started_at,
            completed_at: record.completed_at.unwrap_or_default(),
        })
    }
}

impl From<serde_json::Error> for MaintenanceError {
    fn from(error: serde_json::Error) -> Self {
        MaintenanceError::InvalidConfig(format!("maintenance report serialization failed: {error}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_clamps_runtime_bounds() {
        let config = MaintenanceConfig {
            kind: MaintenanceKind::HealthSummary,
            namespace: None,
            item_limit: usize::MAX,
            retry_limit: usize::MAX,
            max_duration: Duration::from_secs(24 * 60 * 60),
            stale_after_days: 30,
            idempotency_key: "test-key".into(),
        }
        .bounded()
        .unwrap();
        assert_eq!(config.item_limit, MAX_ITEMS_PER_RUN);
        assert_eq!(config.retry_limit, MAX_RETRIES_PER_RUN);
        assert_eq!(config.max_duration, MAX_RUN_DURATION);
    }

    #[test]
    fn config_rejects_missing_identity_and_invalid_threshold() {
        let base = MaintenanceConfig {
            kind: MaintenanceKind::StaleLinks,
            namespace: None,
            item_limit: 1,
            retry_limit: 0,
            max_duration: Duration::from_secs(1),
            stale_after_days: 0,
            idempotency_key: "".into(),
        };
        assert!(base.clone().bounded().is_err());
        assert!(MaintenanceConfig {
            idempotency_key: "key".into(),
            ..base
        }
        .bounded()
        .is_err());
    }
}
