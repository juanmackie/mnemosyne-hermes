//! Integration tests for canonical write IDs on duplicate/near-duplicate stores.
//!
//! Regression coverage for: duplicate writes previously returned a caller
//! generated ID that did not exist — storage merged into a parent but returned
//! Ok(()), while callers surfaced the (nonexistent) new ID. The store now
//! returns a MemoryStoreResult carrying the canonical stored ID and a
//! Created/Merged status, and every returned ID must resolve via a read.

use mnemosyne_core::{MemoryStoreResult, MemoryStoreStatus, MemoryType, StorageBackend};

mod common;
use common::{create_test_storage, sample_memory};

#[tokio::test]
async fn duplicate_write_returns_same_resolvable_canonical_id() {
    let storage = create_test_storage().await;

    // Two identical writes must resolve to ONE canonical stored row.
    let m1 = sample_memory(
        "duplicate write canonical id test content",
        MemoryType::Insight,
        5,
    );
    let m2 = sample_memory(
        "duplicate write canonical id test content",
        MemoryType::Insight,
        5,
    );
    // Force distinct client ids so any returned id difference is meaningful.
    assert_ne!(m1.id, m2.id);

    let r1: MemoryStoreResult = storage.store_memory(&m1).await.unwrap();
    let r2: MemoryStoreResult = storage.store_memory(&m2).await.unwrap();

    // The second write merged into the first parent's row.
    assert_eq!(r1.status, MemoryStoreStatus::Created);
    assert_eq!(r2.status, MemoryStoreStatus::Merged);

    // Identical content -> identical canonical id.
    assert_eq!(r1.id, r2.id);
    // The canonical id is the first memory's id, NOT the second's generated id.
    assert_eq!(r1.id, m1.id);
    assert_ne!(r2.id, m2.id);

    // Both returned ids resolve to the same stored content.
    let g1 = storage.get_memory(r1.id).await.unwrap();
    let g2 = storage.get_memory(r2.id).await.unwrap();
    assert_eq!(g1.content, "duplicate write canonical id test content");
    assert_eq!(g2.content, "duplicate write canonical id test content");
}

#[tokio::test]
async fn distinct_writes_return_distinct_created_ids() {
    let storage = create_test_storage().await;

    let a = sample_memory(
        "distinct canonical id memory alpha",
        MemoryType::Insight,
        4,
    );
    let b = sample_memory(
        "distinct canonical id memory beta - completely different wording",
        MemoryType::Insight,
        6,
    );

    let ra = storage.store_memory(&a).await.unwrap();
    let rb = storage.store_memory(&b).await.unwrap();

    assert_eq!(ra.status, MemoryStoreStatus::Created);
    assert_eq!(rb.status, MemoryStoreStatus::Created);
    assert_ne!(ra.id, rb.id);

    // Both ids resolve to their own content.
    assert_eq!(storage.get_memory(ra.id).await.unwrap().content, a.content);
    assert_eq!(storage.get_memory(rb.id).await.unwrap().content, b.content);
}
