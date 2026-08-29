use chrono::Utc;
use mnemosyne_core::{
    ConnectionMode, InteractionPolicy, LibsqlStorage, MaintenanceConfig, MaintenanceKind,
    MaintenanceRunner, MemoryId, MemoryLink, MemoryNote, MemoryProposalStatus, MemoryType,
    Namespace, PolicyPolarity, PolicyProposalService, PolicySignalKind, ProposalProvenance,
    ProposalService, ProvenanceSourceKind, ProvenanceSourceRole, StorageBackend,
};
use std::path::PathBuf;
use std::sync::Arc;

fn note(id: MemoryId, content: &str) -> MemoryNote {
    let now = Utc::now();
    MemoryNote {
        id,
        namespace: Namespace::Global,
        created_at: now,
        updated_at: now,
        content: content.into(),
        summary: content.into(),
        keywords: vec!["test".into()],
        tags: vec![],
        context: String::new(),
        memory_type: MemoryType::Insight,
        memory_class: mnemosyne_core::MemoryClass::Knowledge,
        provenance: None,
        importance: 5,
        confidence: 0.9,
        links: vec![],
        related_files: vec![],
        related_entities: vec![],
        access_count: 0,
        last_accessed_at: now,
        expires_at: None,
        is_archived: false,
        superseded_by: None,
        embedding: None,
        embedding_model: String::new(),
    }
}

async fn storage() -> LibsqlStorage {
    storage_with_path().await.0
}

async fn storage_with_path() -> (LibsqlStorage, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    // Keep the directory alive for the duration of the test by placing the
    // database in a stable temporary path; the open connection owns the file.
    let path = directory.path().join(format!("{}.db", MemoryId::new()));
    std::mem::forget(directory);
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    (storage, path)
}

#[tokio::test]
async fn abandoned_running_maintenance_run_can_be_reclaimed() {
    let storage = storage().await;
    assert!(storage
        .start_maintenance_run(
            "first-run",
            "reclaim-key",
            "health_summary",
            None,
            10,
            0,
            std::time::Duration::from_millis(1),
            90,
        )
        .await
        .unwrap());
    tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    let runner = MaintenanceRunner::new(Arc::new(storage));
    let report = runner
        .run(MaintenanceConfig {
            kind: MaintenanceKind::HealthSummary,
            namespace: None,
            item_limit: 10,
            retry_limit: 0,
            max_duration: std::time::Duration::from_millis(1),
            stale_after_days: 90,
            idempotency_key: "reclaim-key".into(),
        })
        .await
        .unwrap();
    assert_ne!(report.run_id, "first-run");
}

#[tokio::test]
async fn maintenance_retries_failures_and_preserves_canonical_memory() {
    let (storage, path) = storage_with_path().await;
    let memory_id = MemoryId::new();
    storage
        .store_memory(&note(memory_id, "must survive maintenance failure"))
        .await
        .unwrap();

    // Remove one read-only integrity projection to induce a real scan error;
    // the durable run table remains available for retry and terminal state.
    let database = libsql::Builder::new_local(&path).build().await.unwrap();
    let connection = database.connect().unwrap();
    connection
        .execute("DROP VIEW text_learning_orphans", ())
        .await
        .unwrap();
    drop(connection);
    drop(database);

    let storage = Arc::new(storage);
    let runner = MaintenanceRunner::new(storage.clone());
    let report = runner
        .run(MaintenanceConfig {
            kind: MaintenanceKind::HealthSummary,
            namespace: Some(Namespace::Global),
            item_limit: 10,
            retry_limit: 1,
            max_duration: std::time::Duration::from_secs(5),
            stale_after_days: 90,
            idempotency_key: "maintenance-failure-retry".into(),
        })
        .await
        .unwrap();
    assert_eq!(report.status, mnemosyne_core::MaintenanceStatus::Failed);
    assert_eq!(
        report.attempts, 2,
        "retry_limit=1 must execute two attempts"
    );
    assert_eq!(report.errors_count, 1);
    assert!(report.error_message.is_some());
    assert_eq!(
        storage.get_memory(memory_id).await.unwrap().content,
        "must survive maintenance failure"
    );
}

#[tokio::test]
async fn maintenance_spawn_does_not_block_interactive_reads() {
    let storage = Arc::new(storage().await);
    let memory_id = MemoryId::new();
    storage
        .store_memory(&note(memory_id, "interactive recall remains available"))
        .await
        .unwrap();
    let runner = Arc::new(MaintenanceRunner::new(storage.clone()));
    let handle = runner.spawn(MaintenanceConfig {
        kind: MaintenanceKind::HealthSummary,
        namespace: Some(Namespace::Global),
        item_limit: 10,
        retry_limit: 0,
        max_duration: std::time::Duration::from_secs(5),
        stale_after_days: 90,
        idempotency_key: "background-maintenance-read-check".into(),
    });
    let recalled = tokio::time::timeout(
        std::time::Duration::from_secs(1),
        storage.keyword_search("interactive", Some(Namespace::Global)),
    )
    .await
    .expect("interactive recall should not wait for maintenance")
    .unwrap();
    assert!(recalled.iter().any(|result| result.memory.id == memory_id));
    assert!(handle.await.unwrap().is_ok());
}

#[tokio::test]
async fn policy_requires_owner_approval_before_materialization() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "Please use concise bullet responses"))
        .await
        .unwrap();
    let policy = InteractionPolicy {
        polarity: PolicyPolarity::Prefer,
        guidance: "Use concise bullet responses".into(),
        applicability: "coding responses".into(),
        signal: PolicySignalKind::DirectPreference,
        confidence: 0.9,
        anchors: vec!["coding".into(), "responses".into()],
        evidence: vec![mnemosyne_core::MemoryProvenance {
            source_kind: ProvenanceSourceKind::Turn,
            source_memory_id: Some(source_id),
            session_id: None,
            turn_id: None,
            source_role: ProvenanceSourceRole::User,
            observed_at: Utc::now(),
            evidence_quote: "Please use concise bullet responses".into(),
            extractor_model: None,
            extraction_schema_version: None,
        }],
    };
    let service = PolicyProposalService::new(storage.clone());
    let proposal = service
        .propose(&Namespace::Global, source_id, policy, "extractor", "owner")
        .await
        .unwrap();
    assert_eq!(proposal.status, MemoryProposalStatus::Pending);
    assert!(storage
        .list_interaction_policies()
        .await
        .unwrap()
        .is_empty());
    assert!(service
        .accept(&proposal.id, "intruder", None)
        .await
        .is_err());
    service.accept(&proposal.id, "owner", None).await.unwrap();
    assert!(storage
        .list_interaction_policies()
        .await
        .unwrap()
        .is_empty());
    let applied = service.apply(&proposal.id, "owner").await.unwrap();
    assert_eq!(applied.status, MemoryProposalStatus::Applied);
    let policies = storage.list_interaction_policies().await.unwrap();
    assert_eq!(policies.len(), 1);
    assert_eq!(policies[0].1.guidance, "Use concise bullet responses");
}

#[tokio::test]
async fn reclaimed_maintenance_lease_fences_old_worker() {
    let storage = Arc::new(storage().await);
    assert!(storage
        .start_maintenance_run(
            "old-owner",
            "fenced-key",
            "health_summary",
            None,
            10,
            0,
            std::time::Duration::from_millis(1),
            90,
        )
        .await
        .unwrap());
    tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    assert!(storage
        .start_maintenance_run(
            "new-owner",
            "fenced-key",
            "health_summary",
            None,
            10,
            0,
            std::time::Duration::from_millis(1),
            90,
        )
        .await
        .unwrap());
    assert!(!storage
        .maintenance_run_lease_active("old-owner")
        .await
        .unwrap());
    assert!(storage
        .maintenance_run_lease_active("new-owner")
        .await
        .unwrap());
    assert!(!storage
        .finish_maintenance_run("old-owner", "success", 1, 1, 0, 0, None, None,)
        .await
        .unwrap());
    assert_eq!(
        storage
            .get_maintenance_run("fenced-key")
            .await
            .unwrap()
            .unwrap()
            .status,
        "running"
    );
}

#[tokio::test]
async fn link_traversal_metadata_survives_read_modify_write() {
    let storage = storage().await;
    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(target_id, "target"))
        .await
        .unwrap();
    let mut source = note(source_id, "source");
    source.links.push(MemoryLink {
        target_id,
        link_type: mnemosyne_core::LinkType::References,
        strength: 0.8,
        reason: "protected reference".into(),
        created_at: Utc::now() - chrono::Duration::days(120),
        last_traversed_at: None,
        user_created: true,
    });
    storage.store_memory(&source).await.unwrap();
    storage
        .record_link_traversal(&source_id, &target_id)
        .await
        .unwrap();
    let mut loaded = storage.get_memory(source_id).await.unwrap();
    assert!(loaded.links[0].last_traversed_at.is_some());
    assert!(loaded.links[0].user_created);
    loaded.updated_at = Utc::now();
    storage.update_memory(&loaded).await.unwrap();
    let round_trip = storage.get_memory(source_id).await.unwrap();
    assert!(round_trip.links[0].last_traversed_at.is_some());
    assert!(round_trip.links[0].user_created);
}

#[tokio::test]
async fn normal_content_update_invalidates_old_embedding() {
    let storage = storage().await;
    let id = MemoryId::new();
    storage.store_memory(&note(id, "old text")).await.unwrap();
    storage.store_embedding(&id, &vec![0.3; 384]).await.unwrap();
    let mut updated = storage.get_memory(id).await.unwrap();
    updated.content = "new text".into();
    updated.summary = "new text".into();
    updated.embedding = None;
    updated.updated_at = Utc::now();
    storage.update_memory(&updated).await.unwrap();
    assert!(storage.get_embedding(&id).await.unwrap().is_none());
}

#[tokio::test]
async fn stale_link_report_handles_rfc3339_link_timestamps() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(target_id, "target"))
        .await
        .unwrap();
    let mut source = note(source_id, "source");
    source.links.push(MemoryLink {
        target_id,
        link_type: mnemosyne_core::LinkType::References,
        strength: 0.8,
        reason: "old reference".into(),
        created_at: Utc::now() - chrono::Duration::days(120),
        last_traversed_at: None,
        user_created: false,
    });
    storage.store_memory(&source).await.unwrap();
    let runner = MaintenanceRunner::new(storage.clone());
    let report = runner
        .run(MaintenanceConfig {
            kind: MaintenanceKind::StaleLinks,
            namespace: Some(Namespace::Global),
            item_limit: 10,
            retry_limit: 0,
            max_duration: std::time::Duration::from_secs(5),
            stale_after_days: 90,
            idempotency_key: "stale-link-key".into(),
        })
        .await
        .unwrap();
    assert_eq!(report.status, mnemosyne_core::MaintenanceStatus::Success);
    assert_eq!(report.findings_count, 1);
    assert_eq!(report.findings[0].code, "stale_link");

    storage.archive_memory(target_id).await.unwrap();
    let archived_report = runner
        .run(MaintenanceConfig {
            kind: MaintenanceKind::StaleLinks,
            namespace: Some(Namespace::Global),
            item_limit: 10,
            retry_limit: 0,
            max_duration: std::time::Duration::from_secs(5),
            stale_after_days: 90,
            idempotency_key: "archived-link-key".into(),
        })
        .await
        .unwrap();
    assert!(archived_report
        .findings
        .iter()
        .any(|finding| finding.code == "stale_archived_link"));
}

#[tokio::test]
async fn maintenance_is_bounded_durable_and_idempotent() {
    let storage = Arc::new(storage().await);
    for _ in 0..4 {
        storage
            .store_memory(&note(MemoryId::new(), "uncited factual memory"))
            .await
            .unwrap();
    }
    let runner = MaintenanceRunner::new(storage.clone());
    let config = MaintenanceConfig {
        kind: MaintenanceKind::MissingCitations,
        namespace: Some(Namespace::Global),
        item_limit: 2,
        retry_limit: 1,
        max_duration: std::time::Duration::from_secs(5),
        stale_after_days: 90,
        idempotency_key: "test-maintenance-key".into(),
    };
    let first = runner.run(config.clone()).await.unwrap();
    assert_eq!(
        first.status,
        mnemosyne_core::MaintenanceStatus::Success,
        "maintenance report: {:?}",
        first
    );
    assert_eq!(first.items_processed, 2);
    assert_eq!(first.findings_count, 2);

    let mut mismatched = config.clone();
    mismatched.item_limit = 1;
    assert!(runner.run(mismatched).await.is_err());
    let second = runner.run(config).await.unwrap();
    assert_eq!(second.run_id, first.run_id);
    assert_eq!(
        storage.list_maintenance_runs(None, 10).await.unwrap().len(),
        1
    );
}

#[tokio::test]
async fn proposal_requires_owner_review_and_applies_only_current_base() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "approved source"))
        .await
        .unwrap();
    storage
        .store_memory(&note(target_id, "old canonical text"))
        .await
        .unwrap();
    storage
        .store_embedding(&target_id, &vec![0.1; 384])
        .await
        .unwrap();
    assert!(storage.get_embedding(&target_id).await.unwrap().is_some());
    let service = ProposalService::new(storage.clone());
    let provenance = ProposalProvenance {
        source_memory_ids: vec![source_id],
        evidence_quotes: vec!["approved source".into()],
    };
    let proposal = service
        .propose_update(
            target_id,
            "new canonical text",
            "agent",
            "owner",
            provenance,
        )
        .await
        .unwrap();
    assert_eq!(proposal.status, MemoryProposalStatus::Pending);
    assert!(proposal.diff_text.contains("-old canonical text"));
    assert!(proposal.diff_text.contains("+new canonical text"));
    assert!(service
        .accept(&proposal.id, "intruder", None)
        .await
        .is_err());
    service.accept(&proposal.id, "owner", None).await.unwrap();
    let applied = service.apply(&proposal.id, "owner").await.unwrap();
    assert_eq!(applied.status, MemoryProposalStatus::Applied);
    let updated = storage.get_memory(target_id).await.unwrap();
    assert_eq!(updated.content, "new canonical text");
    assert_eq!(updated.summary, "new canonical text");
    assert_eq!(
        updated
            .provenance
            .as_ref()
            .and_then(|provenance| provenance.source_memory_id),
        Some(source_id)
    );
    assert!(storage.get_embedding(&target_id).await.unwrap().is_none());
    assert!(service.apply(&proposal.id, "owner").await.is_err());
}

#[tokio::test]
async fn proposal_rejects_fabricated_or_missing_scope_evidence() {
    let storage = Arc::new(storage().await);
    let target_id = MemoryId::new();
    let source_id = MemoryId::new();
    storage
        .store_memory(&note(target_id, "target"))
        .await
        .unwrap();
    storage
        .store_memory(&note(source_id, "real source"))
        .await
        .unwrap();
    let service = ProposalService::new(storage);
    assert!(service
        .propose_update(
            target_id,
            "replacement",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![MemoryId::new()],
                evidence_quotes: vec!["fabricated".into()],
            },
        )
        .await
        .is_err());
    assert!(service
        .propose_update(
            target_id,
            "replacement",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["not in source".into()],
            },
        )
        .await
        .is_err());
    assert!(service
        .propose_update(
            target_id,
            "replacement",
            "agent",
            "*",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["real source".into()],
            },
        )
        .await
        .is_err());
}

#[tokio::test]
async fn changed_evidence_source_blocks_apply_even_if_quote_remains() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "source remains quoted"))
        .await
        .unwrap();
    storage
        .store_memory(&note(target_id, "target"))
        .await
        .unwrap();
    let service = ProposalService::new(storage.clone());
    let proposal = service
        .propose_update(
            target_id,
            "replacement",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["source remains quoted".into()],
            },
        )
        .await
        .unwrap();
    let mut changed_source = storage.get_memory(source_id).await.unwrap();
    changed_source.content = "updated source remains quoted".into();
    changed_source.updated_at = Utc::now();
    storage.update_memory(&changed_source).await.unwrap();
    service.accept(&proposal.id, "owner", None).await.unwrap();
    assert!(service.apply(&proposal.id, "owner").await.is_err());
    assert_eq!(
        storage.get_memory(target_id).await.unwrap().content,
        "target"
    );
    assert_eq!(
        service.get(&proposal.id).await.unwrap().unwrap().status,
        MemoryProposalStatus::Failed
    );
}

#[tokio::test]
async fn policy_updates_stay_out_of_generic_factual_proposals() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    let policy_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "explicit preference"))
        .await
        .unwrap();
    let mut policy = note(policy_id, "Prefer concise replies");
    policy.memory_class = mnemosyne_core::MemoryClass::InteractionPolicy;
    policy.memory_type = MemoryType::Preference;
    storage.store_memory(&policy).await.unwrap();
    let service = ProposalService::new(storage);
    assert!(service
        .propose_update(
            policy_id,
            "Prefer verbose replies",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["explicit preference".into()],
            },
        )
        .await
        .is_err());
}

#[tokio::test]
async fn dismissed_proposal_preserves_canonical_memory() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "source"))
        .await
        .unwrap();
    storage
        .store_memory(&note(target_id, "unchanged"))
        .await
        .unwrap();
    let service = ProposalService::new(storage.clone());
    let proposal = service
        .propose_update(
            target_id,
            "must not apply",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["source".into()],
            },
        )
        .await
        .unwrap();
    let dismissed = service
        .dismiss(&proposal.id, "owner", Some("not sufficiently supported"))
        .await
        .unwrap();
    assert_eq!(dismissed.status, MemoryProposalStatus::Dismissed);
    assert_eq!(
        storage.get_memory(target_id).await.unwrap().content,
        "unchanged"
    );
    assert!(service.apply(&proposal.id, "owner").await.is_err());
}

#[tokio::test]
async fn standard_sqlite_upgrade_supports_maintenance_and_proposals() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("standard-upgrade.db");
    {
        let database = libsql::Builder::new_local(&path).build().await.unwrap();
        let connection = database.connect().unwrap();
        connection
            .execute_batch(include_str!("../migrations/sqlite/001_initial_schema.sql"))
            .await
            .unwrap();
        connection
            .execute_batch(
                "CREATE TABLE audit_log_legacy (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, operation TEXT NOT NULL, memory_id TEXT, details TEXT NOT NULL); INSERT INTO audit_log_legacy (operation, details) VALUES ('create', 'legacy details'); DROP TABLE audit_log; ALTER TABLE audit_log_legacy RENAME TO audit_log;",
            )
            .await
            .unwrap();
    }
    let storage = Arc::new(
        LibsqlStorage::new_with_validation(
            ConnectionMode::Local(path.to_string_lossy().into_owned()),
            false,
        )
        .await
        .unwrap(),
    );
    let audit_database = libsql::Builder::new_local(&path).build().await.unwrap();
    let audit_connection = audit_database.connect().unwrap();
    let mut audit_rows = audit_connection
        .query(
            "SELECT metadata FROM audit_log WHERE metadata = 'legacy details'",
            (),
        )
        .await
        .unwrap();
    let migrated_metadata: String = audit_rows.next().await.unwrap().unwrap().get(0).unwrap();
    assert_eq!(migrated_metadata, "legacy details");
    drop(audit_rows);
    drop(audit_connection);
    drop(audit_database);

    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "standard source"))
        .await
        .unwrap();
    let target_embedding = vec![0.25; 384];
    let mut target = note(target_id, "standard old");
    target.embedding = Some(target_embedding.clone());
    storage.store_memory(&target).await.unwrap();
    assert_eq!(
        storage
            .get_embedding(&target_id)
            .await
            .unwrap()
            .unwrap()
            .len(),
        384
    );
    let vector_results = (&*storage as &dyn StorageBackend)
        .vector_search(&target_embedding, 5, Some(Namespace::Global))
        .await
        .unwrap();
    assert!(vector_results
        .iter()
        .any(|result| result.memory.id == target_id));
    let similar_id = MemoryId::new();
    let mut similar = note(similar_id, "standard twin");
    similar.embedding = Some(target_embedding.clone());
    storage.store_memory(&similar).await.unwrap();
    let consolidation = storage
        .find_consolidation_candidates(Some(Namespace::Global))
        .await
        .unwrap();
    assert!(consolidation.iter().any(|(left, right)| {
        (left.id == target_id && right.id == similar_id)
            || (left.id == similar_id && right.id == target_id)
    }));
    let maintenance = MaintenanceRunner::new(storage.clone())
        .run(MaintenanceConfig {
            kind: MaintenanceKind::MissingCitations,
            namespace: Some(Namespace::Global),
            item_limit: 10,
            retry_limit: 0,
            max_duration: std::time::Duration::from_secs(5),
            stale_after_days: 90,
            idempotency_key: "standard-maintenance".into(),
        })
        .await
        .unwrap();
    assert_eq!(
        maintenance.status,
        mnemosyne_core::MaintenanceStatus::Success
    );
    let proposal = ProposalService::new(storage.clone())
        .propose_update(
            target_id,
            "standard new",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["standard source".into()],
            },
        )
        .await
        .unwrap();
    ProposalService::new(storage.clone())
        .accept(&proposal.id, "owner", None)
        .await
        .unwrap();
    ProposalService::new(storage.clone())
        .apply(&proposal.id, "owner")
        .await
        .unwrap();
    let updated = storage.get_memory(target_id).await.unwrap();
    assert_eq!(updated.content, "standard new");
    assert_eq!(updated.summary, "standard new");
    assert!(storage.get_embedding(&target_id).await.unwrap().is_none());

    let purged_id = MemoryId::new();
    storage
        .store_memory(&note(purged_id, "purge me"))
        .await
        .unwrap();
    storage
        .store_embedding(&purged_id, &target_embedding)
        .await
        .unwrap();
    let purge_report = storage.purge_memory(&purged_id).await.unwrap();
    assert!(purge_report.embedding_removed);
    let database = libsql::Builder::new_local(&path).build().await.unwrap();
    let connection = database.connect().unwrap();
    let remaining: i64 = connection
        .query(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?",
            libsql::params![purged_id.to_string()],
        )
        .await
        .unwrap()
        .next()
        .await
        .unwrap()
        .unwrap()
        .get(0)
        .unwrap();
    assert_eq!(remaining, 0);
}

#[tokio::test]
async fn stale_accepted_proposal_fails_without_overwriting_canonical_memory() {
    let storage = Arc::new(storage().await);
    let source_id = MemoryId::new();
    let target_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "source"))
        .await
        .unwrap();
    storage
        .store_memory(&note(target_id, "original"))
        .await
        .unwrap();
    let service = ProposalService::new(storage.clone());
    let proposal = service
        .propose_update(
            target_id,
            "proposed",
            "agent",
            "owner",
            ProposalProvenance {
                source_memory_ids: vec![source_id],
                evidence_quotes: vec!["source".into()],
            },
        )
        .await
        .unwrap();
    service.accept(&proposal.id, "owner", None).await.unwrap();
    let mut changed = storage.get_memory(target_id).await.unwrap();
    changed.content = "independent update".into();
    changed.updated_at = Utc::now();
    storage.update_memory(&changed).await.unwrap();

    assert!(service.apply(&proposal.id, "owner").await.is_err());
    assert_eq!(
        storage.get_memory(target_id).await.unwrap().content,
        "independent update"
    );
    assert_eq!(
        service.get(&proposal.id).await.unwrap().unwrap().status,
        MemoryProposalStatus::Failed
    );
}
