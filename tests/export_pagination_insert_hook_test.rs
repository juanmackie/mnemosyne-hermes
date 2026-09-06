//! Integration tests for FIX 11.
//!
//! Task A: export must paginate via a stable offset-based page walk instead of
//! a single `list_memories` call capped at 10,000 rows, and must state its scope
//! as a snapshot (not a full DB backup).
//!
//! Task B: the A-MEM post-insert `with_insert_hook` constructor is explicitly
//! experimental and NOT wired into production; `MemoryLinker::new` (production)
//! keeps the hook disabled by default.

use mnemosyne_core::artifacts::memory_link::MemoryLinker;
use mnemosyne_core::evolution::config::OnInsertConfig;
use mnemosyne_core::storage::libsql::{ConnectionMode, LibsqlStorage};
use mnemosyne_core::storage::{MemorySortOrder, StorageBackend};
use mnemosyne_core::types::{MemoryId, MemoryNote, MemoryType, Namespace};
use mnemosyne_core::MemoryClass;
use std::collections::HashSet;
use std::sync::Arc;

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

/// Mirrors the export handler's stable page walk (FIX 11 Task A). Asserts every
/// row is collected exactly once across pages — none silently dropped at a cap,
/// none duplicated across page boundaries.
#[tokio::test]
async fn export_pagination_walk_collects_every_row_exactly_once() {
    let mut storage: LibsqlStorage = LibsqlStorage::new_with_validation(
        ConnectionMode::InMemory,
        true,
    )
    .await
    .unwrap();

    let ns = Namespace::Project {
        name: "export-pagination-test".to_string(),
    };

    // Insert enough memories to span multiple pages at the small page size used
    // below (exercises the multi-page walk without the cost of 10k inserts).
    const COUNT: usize = 137;
    let mut expected_ids = HashSet::new();
    for i in 0..COUNT {
        let id = MemoryId::new();
        expected_ids.insert(id);
        storage
            .store_memory(&make_memory(ns.clone(), id, format!("memory content {}", i)))
            .await
            .unwrap();
    }

    // The export handler's loop: page_size, offset grows by rows already read.
    const PAGE_SIZE: usize = 16;
    let mut memories = Vec::new();
    loop {
        let page = storage
            .list_memories_page(Some(ns.clone()), PAGE_SIZE, memories.len(), MemorySortOrder::Recent)
            .await
            .unwrap();
        let count = page.len();
        memories.extend(page);
        if count < PAGE_SIZE {
            break;
        }
    }

    assert_eq!(memories.len(), COUNT, "page walk dropped or duplicated rows");

    let collected: HashSet<MemoryId> = memories.into_iter().map(|m| m.id).collect();
    assert_eq!(
        collected, expected_ids,
        "collected ids must exactly match inserted ids"
    );

    // The old single-call path (10k cap) must agree for a store of this size so
    // the behavior is consistent for non-capped stores.
    let one_shot = storage
        .list_memories(Some(ns), 10000, MemorySortOrder::Recent)
        .await
        .unwrap();
    assert_eq!(one_shot.len(), COUNT);
}

/// Mirrors the export handler's pagination loop shape with a page size that
/// exactly divides the row count, so the loop's termination (page == page_size
/// must continue, short page must stop) is fully exercised without an
/// off-by-one at the boundary.
#[tokio::test]
async fn export_pagination_walk_exact_multiple_boundary() {
    let mut storage: LibsqlStorage = LibsqlStorage::new_with_validation(
        ConnectionMode::InMemory,
        true,
    )
    .await
    .unwrap();

    let ns = Namespace::Project {
        name: "export-pagination-boundary".to_string(),
    };

    const PAGE_SIZE: usize = 20;
    const COUNT: usize = PAGE_SIZE * 3; // exact multiple of page size
    for i in 0..COUNT {
        storage
            .store_memory(&make_memory(
                ns.clone(),
                MemoryId::new(),
                format!("memory content {}", i),
            ))
            .await
            .unwrap();
    }

    let mut memories = Vec::new();
    loop {
        let page = storage
            .list_memories_page(Some(ns.clone()), PAGE_SIZE, memories.len(), MemorySortOrder::Recent)
            .await
            .unwrap();
        let count = page.len();
        memories.extend(page);
        if count < PAGE_SIZE {
            break;
        }
    }
    assert_eq!(memories.len(), COUNT);
}

/// FIX 11 Task B: production construction (`MemoryLinker::new`) must leave the
/// A-MEM insert hook disabled by default, and `with_insert_hook` (EXPERIMENTAL)
/// must be constructible with a disabled config without promoting it.
#[tokio::test]
async fn insert_hook_is_experimental_and_disabled_by_default() {
    let storage: LibsqlStorage = LibsqlStorage::new_with_validation(
        ConnectionMode::InMemory,
        true,
    )
    .await
    .unwrap();
    let backend: Arc<dyn StorageBackend> = Arc::new(storage);

    // Production path: plain `new` — hook stays off by default.
    let production = MemoryLinker::new(backend.clone());

    // Experimentally opt-in path: constructible, but default config is disabled.
    let experimental =
        MemoryLinker::with_insert_hook(backend.clone(), None, OnInsertConfig::default());

    // Both constructors must be reachable from the public API. The default
    // config keeps `enabled` false (cost gate) so neither auto-proposes links
    // unless explicitly enabled.
    assert_eq!(OnInsertConfig::default().enabled, false, "must be opt-in");

    // A plain insert through the production linker must succeed, proving the
    // hook does not interfere with normal memory creation.
    let ns = Namespace::Project {
        name: "insert-hook-test".to_string(),
    };
    let _ = production
        .create_artifact_memory(
            MemoryType::Insight,
            "artifact content for task b".to_string(),
            ns.clone(),
            "/tmp/fix11-artifact.md".to_string(),
            5,
            vec!["fix11".to_string()],
        )
        .await
        .expect("production linker must insert a memory");

    // Silence "unused" for the experimental handle while proving construction.
    let _ = experimental.storage();
    let _ = ns;
}
