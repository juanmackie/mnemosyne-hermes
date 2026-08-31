use chrono::{Duration, Utc};
use mnemosyne_core::session_extract::{
    ExtractedEntity, ExtractedMemoryCandidate, ExtractedResponseFeedback, SessionMessage,
    TurnExtraction, EXTRACTION_SCHEMA_VERSION,
};
use mnemosyne_core::{
    ConnectionMode, InteractionPolicy, LearningMemory, LibsqlStorage, MemoryClass, MemoryEntity,
    MemoryId, MemoryNote, MemoryProvenance, MemoryType, Namespace, PolicyPolarity,
    PolicySignalKind, ProvenanceSourceKind, ProvenanceSourceRole, StorageBackend,
};

fn note(id: MemoryId, content: &str) -> MemoryNote {
    let now = Utc::now();
    MemoryNote {
        id,
        namespace: Namespace::Global,
        created_at: now,
        updated_at: now,
        content: content.into(),
        summary: content.into(),
        keywords: vec![],
        tags: vec!["test".into()],
        context: String::new(),
        memory_type: MemoryType::Insight,
        memory_class: MemoryClass::Knowledge,
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

#[test]
fn extraction_requires_verbatim_role_bound_evidence() {
    let messages = vec![
        SessionMessage::new("user", "I prefer concise bullet responses."),
        SessionMessage::new("assistant", "Understood."),
    ];
    let extraction = TurnExtraction {
        schema_version: EXTRACTION_SCHEMA_VERSION.into(),
        candidates: vec![ExtractedMemoryCandidate {
            content: "The user prefers concise bullet responses.".into(),
            kind: "preference".into(),
            confidence: 0.95,
            evidence_quote: "The user prefers concise bullet responses.".into(),
            source_role: "user".into(),
            entities: vec![ExtractedEntity {
                display_name: "Rust".into(),
                normalized_key: "rust".into(),
                role: "technology".into(),
                confidence: 0.8,
            }],
        }],
        response_feedback: None,
    };
    assert!(extraction.validate(&messages).is_err());

    let mut valid = extraction;
    valid.candidates[0].evidence_quote = "I prefer concise bullet responses.".into();
    valid.candidates[0].entities.clear();
    assert!(valid.validate(&messages).is_ok());
}

#[test]
fn generic_feedback_is_not_promoted_but_explicit_style_feedback_is() {
    let generic = ExtractedResponseFeedback {
        polarity: "avoid".into(),
        guidance: "be more accurate".into(),
        applicability: "coding".into(),
        signal: "dissatisfaction".into(),
        confidence: 0.9,
        evidence_quote: "wrong".into(),
        source_role: "user".into(),
        anchors: vec!["coding".into()],
    };
    assert!(!generic.is_actionable());

    let explicit = ExtractedResponseFeedback {
        polarity: "prefer".into(),
        guidance: "Prefer concise bullet responses".into(),
        applicability: "coding".into(),
        signal: "approval".into(),
        confidence: 0.9,
        evidence_quote: "This concise format is exactly right".into(),
        source_role: "user".into(),
        anchors: vec!["coding".into()],
    };
    assert!(explicit.is_actionable());
}

#[tokio::test]
async fn learning_batch_rolls_back_all_derived_rows_on_failure() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("learning.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let id = MemoryId::new();
    let first = LearningMemory {
        memory: note(id, "first derived memory"),
        entities: vec![],
    };
    // Reusing the primary key forces the second insert to fail after the first
    // one has executed inside the same transaction.
    let second = LearningMemory {
        memory: note(id, "second derived memory"),
        entities: vec![],
    };
    assert!(storage
        .store_learning_batch(&[first, second], None, None)
        .await
        .is_err());
    assert_eq!(
        storage
            .count_memories(Some(Namespace::Global))
            .await
            .unwrap(),
        0
    );
}

#[tokio::test]
async fn policy_class_is_excluded_by_class_filtered_recall() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("policy.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let mut policy = note(MemoryId::new(), "Prefer concise bullets");
    policy.memory_class = MemoryClass::InteractionPolicy;
    policy.memory_type = MemoryType::Preference;
    storage.store_memory(&policy).await.unwrap();
    let factual = (&storage as &dyn StorageBackend)
        .hybrid_search_by_class(
            "concise",
            Some(Namespace::Global),
            5,
            false,
            MemoryClass::Knowledge,
        )
        .await
        .unwrap();
    assert!(factual.is_empty());
}

#[tokio::test]
async fn policy_search_accumulates_evidence_and_rejects_archived_sources() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("policy-lifecycle.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let source_id = MemoryId::new();
    storage
        .store_memory(&note(source_id, "The user requested coding help."))
        .await
        .unwrap();

    let policy_id = MemoryId::new();
    let now = Utc::now();
    let evidence = MemoryProvenance {
        source_kind: ProvenanceSourceKind::Turn,
        source_memory_id: Some(source_id),
        session_id: None,
        turn_id: None,
        source_role: ProvenanceSourceRole::User,
        observed_at: now,
        evidence_quote: "Use concise bullets for coding".into(),
        extractor_model: Some("test".into()),
        extraction_schema_version: Some(EXTRACTION_SCHEMA_VERSION.into()),
    };
    let mut policy_note = note(policy_id, "Prefer concise bullet responses");
    policy_note.namespace = Namespace::Global;
    policy_note.memory_class = MemoryClass::InteractionPolicy;
    policy_note.memory_type = MemoryType::Preference;
    policy_note.context = "coding".into();
    policy_note.related_entities = vec!["coding".into()];
    policy_note.provenance = Some(evidence.clone());
    let policy = InteractionPolicy {
        polarity: PolicyPolarity::Prefer,
        guidance: "Prefer concise bullet responses".into(),
        applicability: "coding".into(),
        signal: PolicySignalKind::DirectPreference,
        confidence: 0.9,
        anchors: vec!["coding".into(), "go".into()],
        evidence: vec![evidence],
    };
    storage
        .store_learning_batch(
            &[LearningMemory {
                memory: policy_note,
                entities: vec![MemoryEntity {
                    display_name: "coding".into(),
                    normalized_name: "coding".into(),
                    role: "anchor".into(),
                    confidence: 1.0,
                }],
            }],
            Some((policy_id, policy)),
            None,
        )
        .await
        .unwrap();
    assert_eq!(
        storage
            .search_interaction_policies("coding", 3)
            .await
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        storage
            .search_interaction_policies("go", 3)
            .await
            .unwrap()
            .len(),
        1,
        "single-token anchors should match on token boundaries"
    );
    assert!(storage
        .search_interaction_policies("golf", 3)
        .await
        .unwrap()
        .is_empty());
    storage.archive_memory(source_id).await.unwrap();
    assert!(storage
        .search_interaction_policies("coding", 3)
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn multi_word_entities_seed_hybrid_recall() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("entities.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let memory_id = MemoryId::new();
    let mut memory = note(memory_id, "A design record with no entity wording.");
    memory.related_entities = vec!["Rust Analyzer".into()];
    storage
        .store_learning_batch(
            &[LearningMemory {
                memory,
                entities: vec![MemoryEntity {
                    display_name: "Rust Analyzer".into(),
                    normalized_name: "rust analyzer".into(),
                    role: "tool".into(),
                    confidence: 0.95,
                }],
            }],
            None,
            None,
        )
        .await
        .unwrap();
    let results = storage
        .hybrid_search("Rust Analyzer", Some(Namespace::Global), 5, false)
        .await
        .unwrap();
    assert!(results.iter().any(
        |result| result.memory.id == memory_id && result.match_reason.contains("entity_anchor")
    ));
}

#[tokio::test]
async fn existing_standard_sqlite_database_upgrades_with_knowledge_default() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("standard.db");
    let legacy_id = MemoryId::new();
    {
        let database = libsql::Builder::new_local(&path).build().await.unwrap();
        let connection = database.connect().unwrap();
        connection
            .execute_batch(include_str!("../migrations/sqlite/001_initial_schema.sql"))
            .await
            .unwrap();
        connection
            .execute(
                "INSERT INTO memories (id, namespace, created_at, updated_at, content, summary, keywords, tags, context, memory_type, importance, confidence, related_files, related_entities, access_count, last_accessed_at, expires_at, is_archived, superseded_by, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                libsql::params![
                    legacy_id.to_string(),
                    serde_json::to_string(&Namespace::Global).unwrap(),
                    Utc::now().to_rfc3339(),
                    Utc::now().to_rfc3339(),
                    "legacy memory",
                    "legacy memory",
                    "[]",
                    "[]",
                    "",
                    "insight",
                    5i64,
                    0.8f64,
                    "[]",
                    "[]",
                    0i64,
                    Utc::now().to_rfc3339(),
                    Option::<String>::None,
                    0i64,
                    Option::<String>::None,
                    "",
                ],
            )
            .await
            .unwrap();
    }
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        false,
    )
    .await
    .unwrap();
    let legacy = storage.get_memory(legacy_id).await.unwrap();
    assert_eq!(legacy.memory_class, MemoryClass::Knowledge);
    let id = MemoryId::new();
    storage
        .store_memory(&note(id, "new standard sqlite memory"))
        .await
        .unwrap();
    let loaded = storage.get_memory(id).await.unwrap();
    assert_eq!(loaded.memory_class, MemoryClass::Knowledge);
}

#[tokio::test]
async fn metadata_identity_reuses_one_raw_turn_and_derived_children() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("turn-identity.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();

    let source_id = MemoryId::new();
    let now = Utc::now();
    let provenance = MemoryProvenance {
        source_kind: ProvenanceSourceKind::Turn,
        source_memory_id: None,
        session_id: Some("session-1".into()),
        turn_id: Some("turn-1".into()),
        source_role: ProvenanceSourceRole::Unknown,
        observed_at: now,
        evidence_quote: "User: remember this".into(),
        extractor_model: None,
        extraction_schema_version: None,
    };
    let mut source = note(source_id, "User: remember this");
    source.provenance = Some(provenance.clone());
    storage.store_memory(&source).await.unwrap();

    assert_eq!(
        storage
            .find_turn_source_memory(&Namespace::Global, "session-1", "turn-1")
            .await
            .unwrap(),
        Some(source_id)
    );

    let mut duplicate = note(MemoryId::new(), "User: remember this");
    duplicate.provenance = Some(provenance);
    assert!(
        storage.store_memory(&duplicate).await.is_err(),
        "the retry identity must reject a second raw source"
    );

    let child_id = MemoryId::new();
    let mut child = note(child_id, "Remembered fact");
    child.provenance = Some(MemoryProvenance {
        source_kind: ProvenanceSourceKind::Turn,
        source_memory_id: Some(source_id),
        session_id: Some("session-1".into()),
        turn_id: Some("turn-1".into()),
        source_role: ProvenanceSourceRole::User,
        observed_at: now,
        evidence_quote: "remember this".into(),
        extractor_model: Some("test".into()),
        extraction_schema_version: Some(EXTRACTION_SCHEMA_VERSION.into()),
    });
    storage.store_memory(&child).await.unwrap();
    assert_eq!(
        storage
            .derived_memories_for_source(source_id)
            .await
            .unwrap(),
        vec![(child_id, MemoryClass::Knowledge)]
    );
}

#[tokio::test]
async fn captured_turns_are_transcript_searchable_but_not_recallable() {
    let agent_id = format!("distill-{}", uuid::Uuid::new_v4());
    let manager = mnemosyne_core::MemoryManager::new(agent_id).await.unwrap();
    let first = manager
        .sync_and_learn_with_metadata(
            "I prefer Rust for this service until 2030-01-02.",
            "We decided to keep SQLite for the durable transcript. hello unrecallable raw phrase xyz.",
            Some("session-1"),
            Some("turn-1"),
        )
        .await
        .unwrap();
    assert!(matches!(
        first.extraction_status,
        mnemosyne_core::ExtractionStatus::Succeeded
    ));
    assert_eq!(first.derived_ids.len(), 2);
    let retry = manager
        .sync_and_learn_with_metadata(
            "I prefer Rust for this service until 2030-01-02.",
            "We decided to keep SQLite for the durable transcript. hello unrecallable raw phrase xyz.",
            Some("session-1"),
            Some("turn-1"),
        )
        .await
        .unwrap();
    assert_eq!(retry.source_memory_id, first.source_memory_id);
    assert_eq!(retry.derived_ids, first.derived_ids);

    let transcript = manager
        .search_session_transcripts("unrecallable raw phrase", Some("session-1"), 5)
        .await
        .unwrap();
    assert_eq!(transcript.len(), 1);
    assert_eq!(transcript[0].turn_id.as_deref(), Some("turn-1"));
    assert_eq!(
        transcript[0].valid_until.unwrap().date_naive().to_string(),
        "2030-01-02"
    );

    // This phrase exists only in the captured assistant turn, so recall must
    // not rank the raw source row.
    assert!(manager
        .recall("unrecallable raw phrase", 5)
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn accessed_old_memory_gets_a_bounded_search_time_reinforcement() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("recency.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let now = Utc::now();
    let idle_id = MemoryId::new();
    let active_id = MemoryId::new();
    let mut idle = note(idle_id, "alpha shared memory");
    idle.created_at = now - Duration::days(90);
    idle.updated_at = idle.created_at;
    idle.last_accessed_at = idle.created_at;
    let mut active = note(active_id, "alpha shared memory");
    active.created_at = idle.created_at;
    active.updated_at = active.created_at;
    active.last_accessed_at = now;
    storage.store_memory(&idle).await.unwrap();
    storage.store_memory(&active).await.unwrap();

    let results = storage
        .hybrid_search("alpha", Some(Namespace::Global), 5, false)
        .await
        .unwrap();
    let active_result = results.iter().find(|r| r.memory.id == active_id).unwrap();
    let idle_result = results.iter().find(|r| r.memory.id == idle_id).unwrap();
    assert!(active_result.score > idle_result.score);
}
