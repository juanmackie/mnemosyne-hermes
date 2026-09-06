//! Integration tests for HippoRAG-style Personalized PageRank retrieval and
//! the A-MEM-style insertion-time link evolution hook.
//!
//! Covers:
//! - `fetch_ppr_adjacency`: building the weighted undirected graph around seeds
//! - PPR blending in hybrid search (multi-hop recall)
//! - The post-insert link-proposal hook (`evolution.on_insert`)
//!
//! These live as integration tests so the real LibSQL schema (with
//! `memory_links` strength/traversal columns) is exercised.

use mnemosyne_core::{
    LibsqlStorage, LinkType, MemoryId, MemoryLink, MemoryNote, MemoryType, Namespace,
    StorageBackend,
};

mod common;
use common::{create_test_storage, sample_memory};

/// A bare memory (no links yet). Links are attached via `update_memory` after
/// all potential link targets are stored, since the storage backend enforces
/// the graph invariant that every link target exists.
fn note(content: &str) -> MemoryNote {
    sample_memory(content, MemoryType::Insight, 5)
}

/// Build a one-directional MemoryLink (storage materializes both directions).
fn link(target: &str, strength: f32) -> MemoryLink {
    MemoryLink {
        target_id: MemoryId::from_string(target).unwrap(),
        link_type: LinkType::References,
        strength,
        reason: "test link".to_string(),
        created_at: chrono::Utc::now(),
        last_traversed_at: None,
        user_created: false,
    }
}

/// Attach a memory's outgoing link set. Critically, `update_memory` *replaces*
/// a memory's full outgoing set and prunes the reverse direction of any link
/// that is being dropped, so each node must carry its complete outgoing set
/// (including the reverse of every edge it must keep) for the graph to be
/// stable across multiple updates.
async fn attach(storage: &LibsqlStorage, node: &MemoryNote, links: Vec<MemoryLink>) {
    let mut with_links = node.clone();
    with_links.links = links;
    storage.update_memory(&with_links).await.unwrap();
}

#[tokio::test]
async fn ppr_adjacency_is_weighted_and_undirected() {
    let storage = create_test_storage().await;

    // A --0.9-- B --0.5-- C  (chain), with D isolated.
    let a = note("alpha content");
    let b = note("beta content");
    let c = note("gamma content");
    let d = note("delta content");

    // Store all nodes first (link targets must exist before edges reference them).
    storage.store_memory(&a).await.unwrap();
    storage.store_memory(&b).await.unwrap();
    storage.store_memory(&c).await.unwrap();
    storage.store_memory(&d).await.unwrap();

    // Edge A<->B owned by both endpoints; B also carries B<->C.
    attach(&storage, &a, vec![link(&b.id.to_string(), 0.9)]).await;
    attach(
        &storage,
        &b,
        vec![link(&a.id.to_string(), 0.9), link(&c.id.to_string(), 0.5)],
    )
    .await;

    // Seed at A, collect 2 hops.
    let adjacency = storage
        .fetch_ppr_adjacency(&[a.id], 2, Some(Namespace::Global))
        .await
        .unwrap();

    assert!(
        adjacency.contains_key(&a.id.to_string()),
        "seed must be present in adjacency"
    );
    assert!(
        adjacency.contains_key(&b.id.to_string()),
        "1-hop neighbor must be present"
    );

    // Both directions materialized: B lists A (0.9) and C (0.5).
    let b_neighbors = &adjacency[&b.id.to_string()];
    let b_has_a = b_neighbors
        .iter()
        .any(|(n, w)| *n == a.id.to_string() && (*w - 0.9).abs() < 1e-5);
    let b_has_c = b_neighbors
        .iter()
        .any(|(n, w)| *n == c.id.to_string() && (*w - 0.5).abs() < 1e-5);
    assert!(b_has_a, "B must list A with strength 0.9");
    assert!(b_has_c, "B must list C with strength 0.5");

    // C reachable at depth 2, D unreachable.
    assert!(
        adjacency.contains_key(&c.id.to_string()),
        "2-hop node must be present"
    );
    assert!(
        !adjacency.contains_key(&d.id.to_string()),
        "unreachable node must be absent"
    );

    // No dangling/zero-strength edges pollute the graph.
    assert!(
        adjacency
            .values()
            .all(|edges| edges.iter().all(|(_, w)| *w > 0.0)),
        "all edges must have positive strength"
    );
}

#[tokio::test]
async fn depth_cap_limits_adjacency_reach() {
    let storage = create_test_storage().await;

    // A -> B -> C (chain). Seeding at A with max_hops=1 must not reach C.
    let a = note("alpha");
    let b = note("beta");
    let c = note("gamma");

    storage.store_memory(&a).await.unwrap();
    storage.store_memory(&b).await.unwrap();
    storage.store_memory(&c).await.unwrap();

    attach(&storage, &a, vec![link(&b.id.to_string(), 1.0)]).await;
    attach(
        &storage,
        &b,
        vec![link(&a.id.to_string(), 1.0), link(&c.id.to_string(), 1.0)],
    )
    .await;

    let adjacency = storage
        .fetch_ppr_adjacency(&[a.id], 1, Some(Namespace::Global))
        .await
        .unwrap();

    assert!(adjacency.contains_key(&b.id.to_string()));
    assert!(
        !adjacency.contains_key(&c.id.to_string()),
        "max_hops=1 must not reach 2-hop node C"
    );
}
