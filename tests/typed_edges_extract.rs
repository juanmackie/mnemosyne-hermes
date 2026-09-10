//! P2 (supermemory borrow — typed graph edges): a later turn that restates an
//! existing fact is dedup-skipped today and the relation is silently dropped.
//! This test pins the new behavior: the restatement records a typed,
//! bidirectional `extends` graph edge from the turn's source memory to the
//! fact it reaffirms — without creating a duplicate row.
//!
//! Before this change the edge was never written (assertion below fails on the
//! old code); after it, the fact is reachable from every turn that reaffirms
//! it, which is the structure recall's graph lane and DERIVES inference build on.

use mnemosyne_core::memory_manager::MemoryManager;
use mnemosyne_core::storage::MemorySortOrder;
use mnemosyne_core::types::LinkType;

#[tokio::test]
async fn reaffirmed_fact_gets_extends_edge_without_duplicate() {
    let db_path =
        std::env::temp_dir().join(format!("mnemosyne-typed-edges-{}.db", uuid::Uuid::new_v4()));
    let mgr = MemoryManager::new_with_path("typed-edges-agent", Some(db_path))
        .await
        .expect("open manager");

    let fact = "I prefer dark mode in my terminal.";

    // First mention extracts and stores the fact.
    let r1 = mgr
        .sync_and_learn(fact, "noted.")
        .await
        .expect("first turn learns the fact");
    let m_fact = *r1
        .derived_ids
        .first()
        .expect("fact extracted on first turn");
    assert!(
        mgr.get(&m_fact).await.expect("read").is_some(),
        "fact memory exists"
    );

    // Restating the same fact must not create a second row.
    let r2 = mgr
        .sync_and_learn(fact, "got it.")
        .await
        .expect("second turn restates it");
    assert!(
        r2.derived_ids.is_empty(),
        "identical restatement is dedup-skipped, not re-stored"
    );

    // The write-lane proof: the restating turn's source is now linked to the
    // fact via a bidirectional `extends` edge (supermemory's `extends` op).
    let fact_note = mgr
        .get(&m_fact)
        .await
        .expect("read")
        .expect("fact still present");
    let has_extends = fact_note
        .links
        .iter()
        .any(|l| l.link_type == LinkType::Extends && l.target_id == r2.source_memory_id);
    assert!(
        has_extends,
        "reaffirming turn must link to the fact via `extends`; got links {:?}",
        fact_note
            .links
            .iter()
            .map(|l| (l.link_type.clone(), l.target_id.to_string()))
            .collect::<Vec<_>>()
    );

    // Structural guard: exactly one stored row holds this fact.
    let matching = mgr
        .list(50, MemorySortOrder::Recent)
        .await
        .expect("list")
        .iter()
        .filter(|m| m.content.contains("dark mode"))
        .count();
    assert_eq!(matching, 1, "no duplicate row for the reaffirmed fact");
}
