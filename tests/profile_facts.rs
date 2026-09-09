//! Always-on profile facts (`StorageBackend::profile_facts`).
//!
//! The profile is the query-independent channel: content an agent should see on
//! every call because no query is semantically close enough to retrieve it
//! (identity, routines, communication style). These tests pin the contract that
//! keeps that channel from becoming a junk drawer.

use mnemosyne_core::{
    ConnectionMode, LibsqlStorage, MemoryClass, MemoryId, MemoryNote, MemoryType, Namespace,
    StorageBackend,
};

fn ns() -> Namespace {
    Namespace::Project {
        name: "profile-test".to_string(),
    }
}

fn note(content: &str, memory_type: MemoryType, importance: u8, tags: Vec<&str>) -> MemoryNote {
    let now = chrono::Utc::now();
    MemoryNote {
        id: MemoryId::new(),
        namespace: ns(),
        created_at: now,
        updated_at: now,
        content: content.to_string(),
        summary: content.chars().take(80).collect(),
        keywords: vec![],
        tags: tags.into_iter().map(String::from).collect(),
        context: "profile test".to_string(),
        memory_type,
        importance,
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
        embedding_model: "test-model".to_string(),
        memory_class: MemoryClass::Knowledge,
        provenance: None,
    }
}

async fn storage_with(notes: &[MemoryNote]) -> LibsqlStorage {
    let storage = LibsqlStorage::new(ConnectionMode::InMemory)
        .await
        .expect("in-memory storage");
    for note in notes {
        storage.store_memory(note).await.expect("store");
    }
    storage
}

#[tokio::test]
async fn marked_facts_come_first_and_episodic_rows_never_qualify() {
    let marked = note(
        "Call me Marco, not Dr. Feld.",
        MemoryType::Entity,
        9,
        vec!["identity", "always_on"],
    );
    let guidance = note(
        "Always run the plan before a deploy.",
        MemoryType::Constraint,
        9,
        vec!["deploy"],
    );
    let episode = note(
        "Shipped the FHIR ingest retry on Tuesday.",
        MemoryType::Insight,
        10,
        vec!["work"],
    );
    let storage = storage_with(&[episode.clone(), guidance.clone(), marked.clone()]).await;

    let facts = storage
        .profile_facts(Some(ns()), 3)
        .await
        .expect("profile facts");
    let ids: Vec<String> = facts.iter().map(|f| f.memory.id.to_string()).collect();

    assert_eq!(
        ids,
        vec![marked.id.to_string(), guidance.id.to_string()],
        "explicitly marked facts must lead, standing guidance fills the slot, \
         and a high-importance episode must not appear"
    );
    assert!(
        facts.iter().all(|f| f.match_reason == "profile"),
        "profile entries must be attributable as such"
    );
}

#[tokio::test]
async fn marked_facts_outlive_the_slot_quota_but_not_the_token_budget() {
    let notes: Vec<MemoryNote> = (0..6)
        .map(|i| {
            note(
                &format!("Standing instruction number {}", i),
                MemoryType::Preference,
                9,
                vec!["always_on"],
            )
        })
        .collect();
    let storage = storage_with(&notes).await;

    let facts = storage
        .profile_facts(Some(ns()), 2)
        .await
        .expect("marked profile");
    assert_eq!(
        facts.len(),
        6,
        "a row the caller marked always_on must not be dropped to meet a quota; \n        the quota budgets the inferred fill, not the contract"
    );
    assert!(
        storage
            .profile_facts(Some(ns()), 0)
            .await
            .expect("zero-slot profile")
            .is_empty(),
        "zero slots must disable the channel"
    );

    // The bound that does hold is prompt cost: the profile rides on every call.
    let long: Vec<MemoryNote> = (0..6)
        .map(|i| {
            note(
                &format!("Instruction {} {}", i, "detail".repeat(90)),
                MemoryType::Preference,
                9,
                vec!["always_on"],
            )
        })
        .collect();
    let bulky = storage_with(&long).await;
    let taken = bulky
        .profile_facts(Some(ns()), 2)
        .await
        .expect("bulky profile");
    assert!(
        taken.len() < 6,
        "marked rows must still stop somewhere: took {} of 6 long rows",
        taken.len()
    );
    assert!(
        !taken.is_empty(),
        "the first marked row is always delivered"
    );
}

#[tokio::test]
async fn expired_archived_and_other_namespaces_are_excluded() {
    let mut live = note(
        "I am at the clinic 08:30 to 16:00.",
        MemoryType::Preference,
        9,
        vec!["schedule", "always_on"],
    );
    let mut expired = note(
        "Last week I was on call.",
        MemoryType::Preference,
        10,
        vec!["always_on"],
    );
    expired.expires_at = Some(chrono::Utc::now() - chrono::Duration::days(2));
    let mut archived = note(
        "Old standing instruction.",
        MemoryType::Constraint,
        10,
        vec!["always_on"],
    );
    archived.is_archived = true;
    let other_ns = note(
        "Another project's identity fact.",
        MemoryType::Entity,
        10,
        vec!["always_on"],
    );
    let mut other_ns_note = other_ns.clone();
    other_ns_note.namespace = Namespace::Project {
        name: "elsewhere".to_string(),
    };

    let storage = storage_with(&[live.clone(), expired, archived, other_ns_note]).await;

    let facts = storage
        .profile_facts(Some(ns()), 5)
        .await
        .expect("profile facts");
    assert_eq!(
        facts
            .iter()
            .map(|f| f.memory.id.to_string())
            .collect::<Vec<_>>(),
        vec![live.id.to_string()],
        "expired, archived and foreign-namespace rows must never reach the profile"
    );

    // Unscoped calls still work and must not lose the live row.
    let all = storage
        .profile_facts(None, 5)
        .await
        .expect("unscoped profile facts");
    assert!(all.iter().any(|f| f.memory.id == live.id));
}

#[tokio::test]
async fn profile_is_stable_across_calls() {
    let notes: Vec<MemoryNote> = (0..5)
        .map(|i| {
            note(
                &format!("Equal-weight standing instruction {}", i),
                MemoryType::Preference,
                7,
                vec!["always_on"],
            )
        })
        .collect();
    let storage = storage_with(&notes).await;

    let first: Vec<String> = storage
        .profile_facts(Some(ns()), 3)
        .await
        .expect("first")
        .iter()
        .map(|f| f.memory.id.to_string())
        .collect();
    let second: Vec<String> = storage
        .profile_facts(Some(ns()), 3)
        .await
        .expect("second")
        .iter()
        .map(|f| f.memory.id.to_string())
        .collect();
    assert_eq!(first, second, "ordering is total; ties must not jitter");
}
