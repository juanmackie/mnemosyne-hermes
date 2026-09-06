//! Integration test for FIX 6: `embed --all` must process EVERY eligible
//! record via stable, paginated storage iteration - not the search-based
//! listing whose candidate limit capped the run at 50.
//!
//! Uses a store with more than 50 memories and asserts all of them receive
//! embeddings (none left without a vector).

use async_trait::async_trait;
use mnemosyne_core::embeddings::EmbeddingService;
use mnemosyne_core::storage::libsql::{ConnectionMode, LibsqlStorage};
use mnemosyne_core::storage::{MemorySortOrder, StorageBackend};
use mnemosyne_core::types::{MemoryId, MemoryNote, MemoryType, Namespace};
use mnemosyne_core::{error::Result, MemoryClass};
use std::sync::Arc;
use tempfile::TempDir;

/// Deterministic no-network embedding service so the test never touches a model.
struct StubEmbeddings {
    dimensions: usize,
}

#[async_trait]
impl EmbeddingService for StubEmbeddings {
    async fn embed(&self, text: &str) -> Result<Vec<f32>> {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut h = DefaultHasher::new();
        text.hash(&mut h);
        let base = h.finish();
        Ok((0..self.dimensions)
            .map(|i| ((base.wrapping_add(i as u64) % 1000) as f32 - 500.0) / 500.0)
            .collect())
    }
    async fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        let mut out = Vec::with_capacity(texts.len());
        for t in texts {
            out.push(self.embed(t).await?);
        }
        Ok(out)
    }
    fn dimensions(&self) -> usize {
        self.dimensions
    }
    fn model_name(&self) -> &str {
        "stub-test"
    }
}

fn make_memory(ns: Namespace, id: MemoryId, content: String) -> MemoryNote {
    MemoryNote {
        id,
        namespace: ns,
        created_at: chrono::Utc::now(),
        updated_at: chrono::Utc::now(),
        content,
        summary: format!("Summary: {}", id.0),
        keywords: vec!["test".to_string()],
        tags: vec!["test".to_string()],
        context: "Test memory".to_string(),
        memory_type: MemoryType::Insight,
        importance: 5,
        confidence: 0.9,
        links: vec![],
        related_files: vec![],
        related_entities: vec![],
        access_count: 0,
        last_accessed_at: chrono::Utc::now(),
        expires_at: None,
        is_archived: false,
        superseded_by: None,
        embedding: None,
        embedding_model: String::new(),
        memory_class: MemoryClass::Knowledge,
        provenance: None,
    }
}

#[tokio::test]
async fn embed_all_processes_every_record_in_store_larger_than_50() {
    let temp_dir = TempDir::new().unwrap();
    let db_path = temp_dir.path().join("embed_all_pagination.db");

    let mut storage: LibsqlStorage = LibsqlStorage::new_with_validation(
        ConnectionMode::Local(db_path.to_str().unwrap().to_string()),
        true,
    )
    .await
    .unwrap();

    let ns = Namespace::Project {
        name: "pagination-test".to_string(),
    };

    // Store more than 50 eligible memories (60).
    const COUNT: usize = 60;
    let mut expected_ids = Vec::with_capacity(COUNT);
    for i in 0..COUNT {
        let id = MemoryId::new();
        expected_ids.push(id);
        storage
            .store_memory(&make_memory(ns.clone(), id, format!("memory content {}", i)))
            .await
            .unwrap();
    }

    // Install the stub embedder.
    storage.set_embedding_service(Arc::new(StubEmbeddings { dimensions: 16 }));

    // Emulate `embed --all`: paginated storage iteration, page size below COUNT.
    let page_size = 20usize;
    let mut processed = Vec::new();
    let mut offset = 0usize;
    loop {
        let page = storage
            .list_memories_page(Some(ns.clone()), page_size, offset, MemorySortOrder::Recent)
            .await
            .unwrap();
        let n = page.len();
        for memory in &page {
            // No real embedder needed - stub writes a vector for every record.
            assert!(
                processed.iter().all(|id| id != &memory.id),
                "pagination must not return the same record twice (page at offset {})",
                offset
            );
            storage
                .generate_and_store_embedding(&memory.id, &memory.content)
                .await
                .unwrap();
            processed.push(memory.id);
        }
        offset += n;
        if n < page_size {
            break;
        }
    }

    // Stable pagination must have visited every stored record exactly once.
    assert_eq!(processed.len(), COUNT, "expected all {} memories to be visited", COUNT);
    for id in &expected_ids {
        assert!(
            processed.contains(id),
            "memory {} was never visited by pagination",
            id.0
        );
    }

    // Every eligible record must have received an embedding: none left behind.
    let mut missing = 0usize;
    for id in &expected_ids {
        if storage.get_embedding(id).await.unwrap().is_none() {
            missing += 1;
        }
    }
    assert_eq!(missing, 0, "{} memories left without an embedding", missing);
}
