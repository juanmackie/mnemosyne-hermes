use chrono::Utc;
use mnemosyne_core::{
    ConnectionMode, LibsqlStorage, MemoryClass, MemoryId, MemoryNote, MemoryType, Namespace,
    StorageBackend,
};

fn note(content: &str) -> MemoryNote {
    let now = Utc::now();
    MemoryNote {
        id: MemoryId::new(),
        namespace: Namespace::Global,
        created_at: now,
        updated_at: now,
        content: content.into(),
        summary: content.into(),
        keywords: vec![],
        tags: vec![],
        context: "retrieval test".into(),
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

#[tokio::test]
async fn recall_persists_scoped_trace_and_evaluates_golden_feedback() {
    let path = std::env::temp_dir().join(format!("mnemosyne_retrieval_{}.db", MemoryId::new()));
    let storage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(path.to_string_lossy().into_owned()),
        true,
    )
    .await
    .unwrap();
    let memory = note("The GPT-5.6 service uses vector embeddings.");
    storage.store_memory(&memory).await.unwrap();

    let results = storage
        .hybrid_search("gpt5.6 embeddings", Some(Namespace::Global), 5, false)
        .await
        .unwrap();
    assert!(results.iter().any(|result| result.memory.id == memory.id));
    storage
        .harvest_retrieval_golden_item("gpt5.6 embeddings", &[memory.id], Some(Namespace::Global))
        .await
        .unwrap();
    let report = storage.run_retrieval_evaluation(10).await.unwrap();
    assert_eq!(report.sample_count, 1);
    assert!(report.precision_at_5 > 0.0);

    let traces = storage.list_retrieval_traces(10).await.unwrap();
    assert!(!traces.is_empty());
    assert_eq!(traces[0].namespace.as_deref(), Some("global"));
    assert!(!traces[0].rewritten_terms.is_empty());
}
