use chrono::Utc;
use mnemosyne_core::storage::libsql::{ConnectionMode, LibsqlStorage, StructuredFact};
use mnemosyne_core::storage::{MemorySortOrder, StorageBackend};
use mnemosyne_core::types::{
    MemoryId, MemoryLink, MemoryNote, MemoryProvenance, MemoryType, Namespace,
    ProvenanceSourceKind, ProvenanceSourceRole,
};

fn note(content: &str, confidence: f32) -> MemoryNote {
    MemoryNote {
        id: MemoryId::new(),
        namespace: Namespace::Global,
        created_at: Utc::now(),
        updated_at: Utc::now(),
        content: content.into(),
        summary: content.into(),
        keywords: vec!["test".into()],
        tags: vec![],
        context: "integrity test".into(),
        memory_type: MemoryType::Insight,
        memory_class: Default::default(),
        provenance: None,
        importance: 5,
        confidence,
        links: vec![],
        related_files: vec![],
        related_entities: vec![],
        access_count: 0,
        last_accessed_at: Utc::now(),
        expires_at: None,
        is_archived: false,
        superseded_by: None,
        embedding: None,
        embedding_model: "test".into(),
    }
}

#[tokio::test]
async fn exact_and_near_duplicates_enrich_one_parent() {
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(format!(
            "/tmp/mnemosyne_integrity_{}.db",
            uuid::Uuid::new_v4()
        )),
        true,
    )
    .await
    .unwrap();
    let mut parent = note("Rust memory storage uses a durable index", 0.8);
    parent.embedding = Some(vec![1.0, 0.0, 0.0]);
    parent.related_entities = vec!["Rust".into()];
    storage.store_memory(&parent).await.unwrap();

    let mut duplicate = note(" rust   memory storage uses a durable index ", 0.9);
    duplicate.embedding = Some(vec![0.99, 0.1, 0.0]);
    duplicate.keywords.push("durable".into());
    storage.store_memory(&duplicate).await.unwrap();
    assert_eq!(storage.count_memories(None).await.unwrap(), 1);
    let merged = storage.get_memory(parent.id).await.unwrap();
    assert!(merged.keywords.contains(&"durable".into()));
    assert!(storage.get_memory(duplicate.id).await.is_err());
}

#[tokio::test]
async fn entities_links_and_fact_supersession_are_centralized() {
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(format!(
            "/tmp/mnemosyne_integrity_{}.db",
            uuid::Uuid::new_v4()
        )),
        true,
    )
    .await
    .unwrap();
    let target = note("Target memory", 0.5);
    storage.store_memory(&target).await.unwrap();
    let mut source = note("Postgres uses the target", 0.5);
    source.related_entities = vec!["Postgres".into()];
    source.links.push(MemoryLink {
        target_id: target.id,
        link_type: mnemosyne_core::types::LinkType::References,
        strength: 1.0,
        reason: "test".into(),
        created_at: Utc::now(),
        last_traversed_at: None,
        user_created: false,
    });
    storage.store_memory(&source).await.unwrap();
    assert!(!storage
        .get_memory(source.id)
        .await
        .unwrap()
        .links
        .is_empty());
    assert!(!storage
        .get_memory(target.id)
        .await
        .unwrap()
        .links
        .is_empty());

    storage
        .store_structured_fact(&StructuredFact {
            memory_id: source.id,
            subject: "service".into(),
            predicate: "status".into(),
            object: "active".into(),
            confidence: 0.5,
            observed_at: Utc::now(),
        })
        .await
        .unwrap();
    storage
        .store_structured_fact(&StructuredFact {
            memory_id: source.id,
            subject: "service".into(),
            predicate: "status".into(),
            object: "retired".into(),
            confidence: 0.9,
            observed_at: Utc::now(),
        })
        .await
        .unwrap();
    let facts = storage
        .list_active_facts("service", "status")
        .await
        .unwrap();
    assert_eq!(facts.len(), 1);
    assert_eq!(facts[0].object, "retired");

    // The same gate also understands the tagged triple representation used by
    // the MCP triples tool and supersedes the older memory row.
    let mut active = note("deployment state active", 0.7);
    active.tags = vec![
        "triple".into(),
        "triple-subject:deployment".into(),
        "triple-predicate:state".into(),
        "triple-object:active".into(),
    ];
    storage.store_memory(&active).await.unwrap();
    let mut retired = note("deployment state retired", 0.9);
    retired.tags = vec![
        "triple".into(),
        "triple-subject:deployment".into(),
        "triple-predicate:state".into(),
        "triple-object:retired".into(),
    ];
    storage.store_memory(&retired).await.unwrap();
    let archived = storage.get_memory(active.id).await.unwrap();
    assert!(archived.is_archived);
    assert_eq!(archived.superseded_by, Some(retired.id));
}

#[tokio::test]
async fn raw_turns_cannot_persist_inline_embeddings() {
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(format!(
            "/tmp/mnemosyne_integrity_{}.db",
            uuid::Uuid::new_v4()
        )),
        true,
    )
    .await
    .unwrap();
    let mut raw = note("captured raw turn", 0.5);
    raw.tags = vec!["turn_sync".into()];
    raw.provenance = Some(MemoryProvenance {
        source_kind: ProvenanceSourceKind::Turn,
        source_memory_id: None,
        session_id: None,
        turn_id: None,
        source_role: ProvenanceSourceRole::Unknown,
        observed_at: Utc::now(),
        evidence_quote: "captured raw turn".into(),
        extractor_model: None,
        extraction_schema_version: None,
    });
    raw.embedding = Some(vec![1.0, 0.0, 0.0]);
    storage.store_memory(&raw).await.unwrap();
    assert!(storage.get_embedding(&raw.id).await.unwrap().is_none());
}

#[tokio::test]
async fn orphan_repair_is_bounded_and_reports_counts() {
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(format!(
            "/tmp/mnemosyne_integrity_{}.db",
            uuid::Uuid::new_v4()
        )),
        true,
    )
    .await
    .unwrap();
    let report = storage.repair_orphans(10).await.unwrap();
    assert_eq!(report.embeddings_removed, 0);
    assert_eq!(report.graph_links_removed, 0);
    let _ = storage
        .list_memories(None, 10, MemorySortOrder::Recent)
        .await
        .unwrap();
}

#[tokio::test]
async fn merged_near_duplicate_keeps_append_only_evidence_and_source() {
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(format!(
            "/tmp/mnemosyne_integrity_{}.db",
            uuid::Uuid::new_v4()
        )),
        true,
    )
    .await
    .unwrap();

    // Two distinct raw sources so provenance source references validate.
    let source_a = note("Source A original transcript", 0.5);
    storage.store_memory(&source_a).await.unwrap();
    let source_b = note("Source B original transcript", 0.5);
    storage.store_memory(&source_b).await.unwrap();

    // Parent with its own primary provenance pointing at source A.
    let mut parent = note("Rust memory storage uses a durable index", 0.8);
    parent.embedding = Some(vec![1.0, 0.0, 0.0]);
    parent.provenance = Some(MemoryProvenance {
        source_kind: ProvenanceSourceKind::Turn,
        source_memory_id: Some(source_a.id),
        session_id: Some("sess-a".into()),
        turn_id: Some("turn-a".into()),
        source_role: ProvenanceSourceRole::User,
        observed_at: Utc::now(),
        evidence_quote: "A alpha evidence".into(),
        extractor_model: Some("t-1".into()),
        extraction_schema_version: Some("1".into()),
    });
    storage.store_memory(&parent).await.unwrap();

    // Near-duplicate carrying its OWN distinct evidence from source B; it
    // must merge into the parent without destroying the parent's attribution.
    let mut child = note("Rust memory storage uses a durable WAL index", 0.9);
    child.embedding = Some(vec![0.98, 0.2, 0.0]);
    child.provenance = Some(MemoryProvenance {
        source_kind: ProvenanceSourceKind::Turn,
        source_memory_id: Some(source_b.id),
        session_id: Some("sess-b".into()),
        turn_id: Some("turn-b".into()),
        source_role: ProvenanceSourceRole::User,
        observed_at: Utc::now(),
        evidence_quote: "B beta evidence".into(),
        extractor_model: Some("t-2".into()),
        extraction_schema_version: Some("1".into()),
    });
    storage.store_memory(&child).await.unwrap();

    // Both statements collapsed into the single parent row.
    assert_eq!(storage.count_memories(None).await.unwrap(), 1);

    let merged = storage.get_memory(parent.id).await.unwrap();
    assert!(merged.content.contains("Rust memory storage uses a durable index"));
    assert!(merged.content.contains("WAL index"));
    // The parent's OWN primary provenance must survive the merge.
    let parent_prov = merged.provenance.as_ref().expect("parent provenance kept");
    assert_eq!(parent_prov.evidence_quote, "A alpha evidence");
    assert_eq!(parent_prov.source_memory_id, Some(source_a.id));

    // Append-only evidence keeps the merged statement's attribution too.
    let evidence = storage
        .list_memory_evidence(parent.id)
        .await
        .unwrap();
    let beta = evidence
        .iter()
        .find(|e| e.evidence_quote == "B beta evidence")
        .expect("merged statement evidence retained");
    assert_eq!(beta.source_memory_id, Some(source_b.id));

    // Deleting an individual source must not misattribute surviving content.
    storage.purge_memory(&source_b.id).await.unwrap();
    let after = storage.list_memory_evidence(parent.id).await.unwrap();
    let beta = after
        .iter()
        .find(|e| e.evidence_quote == "B beta evidence")
        .expect("surviving evidence kept");
    // The deleted source reference is cleared, NOT reassigned to parent/source A.
    assert_eq!(beta.source_memory_id, None);
    let still_parent = storage.get_memory(parent.id).await.unwrap();
    assert_eq!(
        still_parent.provenance.as_ref().unwrap().evidence_quote,
        "A alpha evidence",
        "surviving parent attribution unchanged after source purge"
    );
}
