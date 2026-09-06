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

use async_trait::async_trait;
use mnemosyne_core::{
    artifacts::{LinkProposer, MemoryLinker},
    evolution::OnInsertConfig,
    LibsqlStorage, LinkType, MemoryId, MemoryLink, MemoryNote, MemoryType, Namespace, Result,
    SearchConfig, StorageBackend,
};
use std::sync::Arc;

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
        .fetch_ppr_adjacency(
            &[a.id],
            2,
            Some(Namespace::Global),
            LibsqlStorage::PPR_NODE_BUDGET,
            LibsqlStorage::PPR_EDGE_BUDGET,
            LibsqlStorage::PPR_QUERY_BATCH,
        )
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
        .fetch_ppr_adjacency(
            &[a.id],
            1,
            Some(Namespace::Global),
            LibsqlStorage::PPR_NODE_BUDGET,
            LibsqlStorage::PPR_EDGE_BUDGET,
            LibsqlStorage::PPR_QUERY_BATCH,
        )
        .await
        .unwrap();

    assert!(adjacency.contains_key(&b.id.to_string()));
    assert!(
        !adjacency.contains_key(&c.id.to_string()),
        "max_hops=1 must not reach 2-hop node C"
    );
}

/// Traversal budgets bound the PPR subgraph on a high-degree `memory_links`
/// graph, so two-hop BFS can't explode into an unbounded graph. Seeding a hub
/// that links to many neighbors with a tiny `node_budget` must cap the number
/// of visited nodes (seed counts against the budget).
#[tokio::test]
async fn node_budget_caps_traversal_on_high_degree_graph() {
    let storage = create_test_storage().await;

    // Hub H linked to many neighbors; H is stored and then linked out to all.
    let hub = note("hub");
    storage.store_memory(&hub).await.unwrap();
    let mut hub_links = Vec::new();
    for i in 0..10 {
        let n = note(&format!("neighbor_{}", i));
        storage.store_memory(&n).await.unwrap();
        hub_links.push(link(&n.id.to_string(), 0.9));
    }
    attach(&storage, &hub, hub_links).await;

    // node_budget=3 : seed (hub) + at most 2 more nodes may be visited.
    let adjacency = storage
        .fetch_ppr_adjacency(&[hub.id], 1, Some(Namespace::Global), 3, 400, 50)
        .await
        .unwrap();

    assert!(
        adjacency.len() <= 3,
        "node budget must cap subgraph size, got {}",
        adjacency.len()
    );
    assert!(
        adjacency.contains_key(&hub.id.to_string()),
        "seed must always survive"
    );
}

/// Batching the frontier into small SQL `IN (...)` lists must not change the
/// resulting graph: a tiny `query_batch` should return the same adjacency as
/// an effectively unlimited one for a small chain.
#[tokio::test]
async fn query_batching_preserves_adjacency() {
    let storage = create_test_storage().await;

    let a = note("alpha");
    let b = note("beta");
    let c = note("gamma");
    storage.store_memory(&a).await.unwrap();
    storage.store_memory(&b).await.unwrap();
    storage.store_memory(&c).await.unwrap();
    attach(&storage, &a, vec![link(&b.id.to_string(), 0.9)]).await;
    attach(
        &storage,
        &b,
        vec![link(&a.id.to_string(), 0.9), link(&c.id.to_string(), 0.5)],
    )
    .await;

    let unlimited = storage
        .fetch_ppr_adjacency(&[a.id], 2, Some(Namespace::Global), 500, 400, 100)
        .await
        .unwrap();
    let batched = storage
        .fetch_ppr_adjacency(&[a.id], 2, Some(Namespace::Global), 500, 400, 1)
        .await
        .unwrap();

    assert_eq!(
        batched.len(),
        unlimited.len(),
        "batching must not drop nodes"
    );
    let mut total_batched = 0usize;
    let mut total_unlimited = 0usize;
    for (id, nbrs) in &batched {
        let un = &unlimited[id];
        total_batched += nbrs.len();
        total_unlimited += un.len();
        assert_eq!(nbrs.len(), un.len(), "edge count differs for {}", id);
    }
    assert_eq!(
        total_batched, total_unlimited,
        "batching must not change total edges"
    );
}

/// Build a SearchConfig with deterministic channels (vector off) and PPR
/// togglable, so the blend's effect can be isolated against an identical
/// fixture.
fn search_config(ppr_on: bool) -> mnemosyne_core::SearchConfig {
    let mut config = mnemosyne_core::SearchConfig::default();
    config.enable_vector_search = false; // no embedding service in this test
    config.enable_graph_expansion = true; // ensure 2-hop node reaches candidates
    config.enable_ppr = ppr_on;
    config.ppr_weight = 0.5;
    config
}

/// Run hybrid search for `query` against a storage, returning a map of
/// memory content -> score for easy comparison.
async fn run_search(
    storage: &LibsqlStorage,
    query: &str,
) -> std::collections::HashMap<String, f32> {
    let results = storage
        .hybrid_search(query, Some(Namespace::Global), 20, true)
        .await
        .unwrap();
    results
        .into_iter()
        .map(|r| (r.memory.content.clone(), r.score))
        .collect()
}

#[tokio::test]
async fn ppr_blend_promotes_multi_hop_memory() {
    // A --0.9-- B --0.9-- C. Query "alpha" keyword-matches only A (the seed);
    // C is two hops away and only appears because of graph expansion. PPR
    // seeded at A should spread positive mass to C and boost its score.
    let mut off = create_test_storage().await;
    off.set_search_config(search_config(false));
    let mut on = create_test_storage().await;
    on.set_search_config(search_config(true));

    for storage in [&off, &on] {
        let a = note("alpha primary target");
        let b = note("beta secondary");
        let c = note("gamma tertiary");
        storage.store_memory(&a).await.unwrap();
        storage.store_memory(&b).await.unwrap();
        storage.store_memory(&c).await.unwrap();
        attach(storage, &a, vec![link(&b.id.to_string(), 0.9)]).await;
        attach(
            storage,
            &b,
            vec![link(&a.id.to_string(), 0.9), link(&c.id.to_string(), 0.9)],
        )
        .await;
    }

    let baseline = run_search(&off, "alpha").await;
    let boosted = run_search(&on, "alpha").await;

    // The 2-hop node must be present in both (graph-expanded) result sets.
    let base_c = *baseline
        .get("gamma tertiary")
        .expect("2-hop node must be a candidate without PPR");
    let on_c = *boosted
        .get("gamma tertiary")
        .expect("2-hop node must be a candidate with PPR");

    assert!(
        on_c > base_c,
        "PPR must boost the 2-hop linked memory (base {base_c}, ppr {on_c})"
    );
}

/// A canned proposer: returns a fixed set of proposed links for any input.
/// Lets the hook's orchestration be asserted deterministically without a live
/// LLM API call.
struct StubProposer {
    links: Vec<MemoryLink>,
}

#[async_trait]
impl LinkProposer for StubProposer {
    async fn propose(
        &self,
        _new_memory: &MemoryNote,
        _candidates: &[MemoryNote],
    ) -> Result<Vec<MemoryLink>> {
        Ok(self.links.clone())
    }
}

/// `evolution.on_insert`: the A-MEM post-insert hook proposes cross-links
/// between a freshly-inserted memory and the k nearest existing memories,
/// *without* any scheduler run.
///
/// A new memory (embedding identical to an existing one's) is inserted, the
/// stub proposer suggests a link from it to the nearest neighbour, and the
/// hook materialises that link bidirectionally on both memories.
#[tokio::test]
async fn insert_hook_links_similar_memory_without_scheduler() {
    let mut storage = create_test_storage().await;
    storage.set_search_config(SearchConfig {
        enable_vector_search: true,
        ..Default::default()
    });

    // An existing, semantically-similar memory.
    let existing = note("Alpha: retries with exponential backoff");
    storage.store_memory(&existing).await.unwrap();
    let embedding = vec![0.9_f32, 0.1, 0.1];
    storage
        .store_embedding(&existing.id, &embedding, "test")
        .await
        .unwrap();

    // The new memory whose insert triggers the hook. Same embedding -> nearest.
    let new_note = note("Beta: exponential backoff for network calls");
    storage.store_memory(&new_note).await.unwrap();
    storage
        .store_embedding(&new_note.id, &embedding, "test")
        .await
        .unwrap();

    let proposer = StubProposer {
        links: vec![link(&existing.id.to_string(), 0.8)],
    };
    let linker = MemoryLinker::with_insert_hook(
        Arc::new(storage),
        Some(Arc::new(proposer)),
        OnInsertConfig {
            enabled: true,
            k: 8,
        },
    );

    let n_proposed = linker.run_insert_hook(new_note.id).await.unwrap();
    assert_eq!(n_proposed, 1, "hook should propose exactly one cross-link");

    // The new memory now carries an outgoing cross-link to the similar memory.
    let after_new = linker.storage().get_memory(new_note.id).await.unwrap();
    assert!(
        after_new.links.iter().any(|l| l.target_id == existing.id),
        "new memory should gain an outgoing cross-link to the nearest neighbour"
    );

    // The edge is bidirectional: the existing memory links back to the new one.
    let after_existing = linker.storage().get_memory(existing.id).await.unwrap();
    assert!(
        after_existing
            .links
            .iter()
            .any(|l| l.target_id == new_note.id),
        "cross-link must be materialized bidirectionally"
    );
}
