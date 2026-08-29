use chrono::Utc;
use mnemosyne_core::reasoning::{
    ExtractedReasoningItem, ReasoningExperience, ReasoningExtraction, ReasoningLessonKind,
    ReasoningMemory, TaskOutcome, REASONING_EXTRACTION_SCHEMA_VERSION,
};
use mnemosyne_core::{
    ConnectionMode, LibsqlStorage, MemoryClass, MemoryId, MemoryLink, MemoryNote, MemoryProvenance,
    MemoryType, Namespace, ProvenanceSourceKind, ProvenanceSourceRole, ReasoningMemoryRecord,
    StorageBackend,
};

fn source_note(id: MemoryId, namespace: Namespace) -> MemoryNote {
    let now = Utc::now();
    MemoryNote {
        id,
        namespace,
        created_at: now,
        updated_at: now,
        content: "Observable task trajectory".into(),
        summary: "Observable task trajectory".into(),
        keywords: vec!["task".into()],
        tags: vec!["reasoning_source".into()],
        context: "completed task".into(),
        memory_type: MemoryType::AgentEvent,
        memory_class: MemoryClass::Knowledge,
        provenance: None,
        importance: 5,
        confidence: 1.0,
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

fn reasoning_record(
    source_id: MemoryId,
    namespace: Namespace,
    id: MemoryId,
    content: &str,
) -> ReasoningMemoryRecord {
    let now = Utc::now();
    ReasoningMemoryRecord {
        memory: ReasoningMemory {
            memory: MemoryNote {
                id,
                namespace,
                created_at: now,
                updated_at: now,
                content: content.into(),
                summary: "Verify pagination before concluding".into(),
                keywords: vec!["pagination".into(), "complete".into()],
                tags: vec![
                    "reasoning_strategy".into(),
                    "reasoning_guardrail".into(),
                    "reasoning_failure".into(),
                ],
                context: "when a task requires a complete result".into(),
                memory_type: MemoryType::BugFix,
                memory_class: MemoryClass::Knowledge,
                provenance: Some(MemoryProvenance {
                    source_kind: ProvenanceSourceKind::Turn,
                    source_memory_id: Some(source_id),
                    session_id: None,
                    turn_id: None,
                    source_role: ProvenanceSourceRole::Assistant,
                    observed_at: now,
                    evidence_quote: "The first page looked complete".into(),
                    extractor_model: Some("test".into()),
                    extraction_schema_version: Some(REASONING_EXTRACTION_SCHEMA_VERSION.into()),
                }),
                importance: 7,
                confidence: 0.9,
                links: vec![MemoryLink {
                    target_id: source_id,
                    link_type: mnemosyne_core::LinkType::References,
                    strength: 1.0,
                    reason: "test source".into(),
                    created_at: now,
                    last_traversed_at: None,
                    user_created: false,
                }],
                related_files: vec![],
                related_entities: vec![],
                access_count: 0,
                last_accessed_at: now,
                expires_at: None,
                is_archived: false,
                superseded_by: None,
                embedding: None,
                embedding_model: String::new(),
            },
            lesson_kind: ReasoningLessonKind::Guardrail,
            title: "Verify pagination before concluding".into(),
            description: "Check every page before claiming completeness".into(),
            applicability: "when a task requires a complete result".into(),
        },
        entities: vec![],
    }
}

#[test]
fn reasoning_extraction_requires_outcome_aligned_lesson_kind() {
    let messages = vec![mnemosyne_core::session_extract::SessionMessage::new(
        "assistant",
        "The first page looked complete",
    )];
    let extraction = ReasoningExtraction {
        schema_version: REASONING_EXTRACTION_SCHEMA_VERSION.into(),
        items: vec![ExtractedReasoningItem {
            title: "Check all pages".into(),
            description: "Use for complete-result tasks".into(),
            content: "Verify pagination before concluding".into(),
            lesson_kind: ReasoningLessonKind::Guardrail,
            applicability: "complete-result tasks".into(),
            confidence: 0.8,
            evidence_quote: "The first page looked complete".into(),
            source_role: "assistant".into(),
        }],
    };
    assert!(extraction
        .validate(&messages, TaskOutcome::Success)
        .is_err());
    assert!(extraction.validate(&messages, TaskOutcome::Failure).is_ok());
}

#[tokio::test]
async fn reasoning_items_are_stored_searched_and_archived_with_their_source() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("reasoning.db");
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let namespace = Namespace::Global;
    let source_id = MemoryId::new();
    storage
        .store_memory(&source_note(source_id, namespace.clone()))
        .await
        .unwrap();

    let experience = ReasoningExperience {
        id: uuid::Uuid::new_v4().to_string(),
        namespace: namespace.clone(),
        source_memory_id: source_id,
        task_summary: "Find every matching record".into(),
        outcome: TaskOutcome::Failure,
        verifier: "reviewer".into(),
        confidence: 0.95,
        outcome_evidence: "Reviewer found an omitted second page".into(),
        created_at: Utc::now(),
    };
    let item = reasoning_record(
        source_id,
        namespace.clone(),
        MemoryId::new(),
        "Check pagination before claiming a complete result.",
    );
    let item_id = item.memory.memory.id;
    storage
        .store_reasoning_experience(&experience, &[item])
        .await
        .unwrap();

    let hits = storage
        .search_reasoning_strategies("pagination complete result", Some(namespace), 1)
        .await
        .unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].result.memory.id, item_id);
    assert_eq!(hits[0].outcome, TaskOutcome::Failure);
    assert_eq!(hits[0].lesson_kind, ReasoningLessonKind::Guardrail);

    let purge = storage.purge_memory(&source_id).await.unwrap();
    assert_eq!(purge.memory_id, source_id.to_string());
    assert!(matches!(
        storage.get_memory(item_id).await,
        Err(mnemosyne_core::MnemosyneError::MemoryNotFound(_))
    ));
    assert!(storage
        .search_reasoning_strategies("pagination", Some(Namespace::Global), 1)
        .await
        .unwrap()
        .is_empty());
}
