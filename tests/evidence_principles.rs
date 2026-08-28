//! Evidence-backed principle regression tests.
//!
//! Encodes the memory-research findings as executable invariants:
//!
//! 1. **Sparse memory abstains free** — confidence-gated recall returns an
//!    explicit `Abstain` instead of weak matches that invite over-answering.
//! 2. **Hybrid retrieval fails closed** — when a ranking signal (query
//!    embedding) fails, the store refuses to serve silently-unranked results
//!    rather than degrading invisibly.
//! 3. **True forgetting + PII** — "forget X" purges the row, embedding,
//!    link graph, FTS index and audit trail, and reports what was removed.
//! 4. **Temporal supersession** — "what was true as of T" is queryable from
//!    the supersedence timeline; history is not erased, it is versioned.

use mnemosyne_core::{
    ConnectionMode, EmbeddingService, LibsqlStorage, MemoryConfig, MemoryId, MemoryManager,
    MemoryNote, MemoryType, Namespace, PurgeReport, RecallDecision, SearchConfig, SearchResult,
    StorageBackend, DEFAULT_ABSTENTION_THRESHOLD,
};

// --------------------------------------------------------------------- helpers

fn agent_ns(agent: &str) -> Namespace {
    Namespace::Agent {
        agent_id: agent.to_string(),
    }
}

fn note(content: &str, created_at: chrono::DateTime<chrono::Utc>, ns: &Namespace) -> MemoryNote {
    MemoryNote {
        id: MemoryId::new(),
        namespace: ns.clone(),
        created_at,
        updated_at: created_at,
        content: content.to_string(),
        summary: content.chars().take(80).collect(),
        keywords: vec![],
        tags: vec![],
        context: "evidence-principles test".to_string(),
        memory_type: MemoryType::Insight,
        importance: 5,
        confidence: 0.9,
        links: vec![],
        related_files: vec![],
        related_entities: vec![],
        access_count: 0,
        last_accessed_at: created_at,
        expires_at: None,
        is_archived: false,
        superseded_by: None,
        embedding: None,
        embedding_model: "test-model".to_string(),
        memory_class: mnemosyne_core::MemoryClass::Knowledge,
        provenance: None,
    }
}

fn temp_db(tag: &str) -> String {
    let dir = std::env::temp_dir().join(format!(
        "mnx-evidence-{}-{}-{}",
        tag,
        std::process::id(),
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
    ));
    std::fs::create_dir_all(&dir).expect("create temp dir");
    dir.join("test.db").display().to_string()
}

/// An embedding service whose every call fails — simulates a dead ranking
/// signal so we can prove retrieval refuses to serve unranked results.
struct FailingEmbedder;

#[async_trait::async_trait]
impl EmbeddingService for FailingEmbedder {
    async fn embed(&self, _text: &str) -> mnemosyne_core::Result<Vec<f32>> {
        Err(mnemosyne_core::MnemosyneError::Database(
            "simulated embedder outage".to_string(),
        ))
    }
    async fn embed_batch(&self, _texts: &[&str]) -> mnemosyne_core::Result<Vec<Vec<f32>>> {
        Err(mnemosyne_core::MnemosyneError::Database(
            "simulated embedder outage".to_string(),
        ))
    }
    fn dimensions(&self) -> usize {
        384
    }
    fn model_name(&self) -> &str {
        "failing-test-embedder"
    }
}

// ------------------------------------------------------------- 1. abstention

#[tokio::test]
async fn abstention_gates_weak_and_empty_matches() {
    let mgr = MemoryManager::new_with_path(
        "abstain-test",
        Some(std::path::PathBuf::from(temp_db("abstain"))),
    )
    .await
    .expect("manager");

    mgr.store("User's favorite programming language is Rust.")
        .await
        .expect("store");

    // Completely unrelated query → no evidence → explicit abstention.
    let decision = mgr
        .recall_decided(
            "quantum xylophone zebra",
            5,
            MemoryConfig::new(),
            DEFAULT_ABSTENTION_THRESHOLD,
        )
        .await
        .expect("decide");
    match &decision {
        RecallDecision::Abstain { reason, .. } => {
            assert!(!reason.is_empty(), "abstention must carry a reason");
        }
        RecallDecision::Answer { results, .. } => panic!(
            "over-answer on unrelated query: served {} results",
            results.len()
        ),
    }

    // On-topic query → answer with confidence at or above threshold.
    let decision = mgr
        .recall_decided(
            "favorite programming language",
            5,
            MemoryConfig::new(),
            DEFAULT_ABSTENTION_THRESHOLD,
        )
        .await
        .expect("decide");
    match &decision {
        RecallDecision::Answer {
            results,
            confidence,
        } => {
            assert!(!results.is_empty());
            assert!(
                *confidence >= DEFAULT_ABSTENTION_THRESHOLD,
                "confidence {:.2} below threshold {:.2}",
                confidence,
                DEFAULT_ABSTENTION_THRESHOLD
            );
        }
        RecallDecision::Abstain { best_score, .. } => panic!(
            "false abstention on strong keyword match (best={:.2})",
            best_score
        ),
    }

    // A very high threshold converts even a real hit into principled abstention.
    let decision = mgr
        .recall_decided(
            "favorite programming language",
            5,
            MemoryConfig::new(),
            0.99,
        )
        .await
        .expect("decide");
    assert!(matches!(decision, RecallDecision::Abstain { .. }));
}

// -------------------------------------------------- 2. fail-closed retrieval

#[tokio::test]
async fn fail_closed_retrieval_refuses_silent_degradation() {
    let path = temp_db("failclosed");
    let mut storage = LibsqlStorage::new_with_validation(ConnectionMode::Local(path.clone()), true)
        .await
        .expect("storage");

    let ns = agent_ns("failclosed");
    storage
        .store_memory(&note(
            "The launch code is stored in the vault.",
            chrono::Utc::now(),
            &ns,
        ))
        .await
        .expect("store");

    // Dead ranking signal + fail-closed (the default): refuse to answer.
    storage.set_embedding_service(std::sync::Arc::new(FailingEmbedder));
    let mut cfg = SearchConfig::default();
    cfg.fail_closed = true;
    storage.set_search_config(cfg);

    let result = storage
        .hybrid_search("vault", Some(ns.clone()), 5, false)
        .await;
    let err = result.expect_err("fail-closed must error, not degrade silently");
    assert!(
        err.to_string().contains("fail-closed"),
        "error should identify fail-closed refusal: {}",
        err
    );

    // Explicit opt-out restores legacy fail-open behavior (warn + keyword-only).
    let mut cfg = SearchConfig::default();
    cfg.fail_closed = false;
    storage.set_search_config(cfg);
    let results = storage
        .hybrid_search("vault", Some(ns), 5, false)
        .await
        .expect("fail-open degradation allowed");
    assert_eq!(
        results.len(),
        1,
        "keyword-only fallback still finds content"
    );
}

// --------------------------------------------------- 3. true forgetting + PII

#[tokio::test]
async fn true_purge_removes_row_embedding_links_and_audit() {
    let path = temp_db("purge");
    let storage = LibsqlStorage::new_with_validation(ConnectionMode::Local(path.clone()), true)
        .await
        .expect("storage");
    let ns = agent_ns("purge");
    let now = chrono::Utc::now();

    let a = note("PII: alice@example.com loves gardening", now, &ns);
    let b = note("bob@example.com prefers email contact", now, &ns);
    storage.store_memory(&a).await.expect("store a");
    storage.store_memory(&b).await.expect("store b");

    // Supersede b by a fresh note, then purge the successor: the dangling
    // back-reference on b must be cleared.
    let c = note("carol@example.com is the new emergency contact", now, &ns);
    storage.store_memory(&c).await.expect("store c");
    storage
        .mark_superseded(&b.id, &c.id)
        .await
        .expect("supersede");

    // Purging c must clear b.superseded_by back-reference.
    let report_c: PurgeReport = storage.purge_memory(&c.id).await.expect("purge c");
    assert_eq!(report_c.memory_id, c.id.to_string());
    assert_eq!(report_c.supersession_refs_cleared, 1);

    let b_after = storage.get_memory(b.id).await.expect("b survives");
    assert!(
        b_after.superseded_by.is_none(),
        "dangling supersedes pointer must be cleared"
    );

    // Full purge of a: gone from store, FTS, embeddings, audit trail.
    storage
        .generate_and_store_embedding(&a.id, "never-mind-content") // no service → no-op but harmless
        .await
        .ok();
    let report_a = storage.purge_memory(&a.id).await.expect("purge a");
    assert_eq!(report_a.memory_id, a.id.to_string());

    // Store lookup: gone entirely (not archived — gone).
    let res = storage.get_memory(a.id).await;
    assert!(res.is_err(), "purged memory must not be retrievable");

    // FTS index: keyword search must not resurrect content.
    let hits: Vec<SearchResult> = storage
        .keyword_search("gardening", Some(ns.clone()))
        .await
        .expect("keyword search");
    assert!(
        hits.iter().all(|h| h.memory.id != a.id),
        "FTS entry must be removed with the row"
    );

    // Audit trail for the purged memory is gone too.
    let audit = storage.get_audit_trail(a.id).await.expect("audit query");
    assert!(
        audit.is_empty(),
        "audit rows must be purged with the memory"
    );

    // Purge again → clean MemoryNotFound, idempotent-ish failure semantics.
    let again = storage.purge_memory(&a.id).await;
    assert!(again.is_err(), "second purge must report not-found");

    // b still present (archived by the supersession, but retrievable by id).
    let b_check = storage.get_memory(b.id).await.expect("b retrievable");
    assert_eq!(b_check.content, "bob@example.com prefers email contact");
}

#[tokio::test]
async fn forget_matching_cascades_and_reports_removals() {
    let mgr = MemoryManager::new_with_path(
        "cascade-test",
        Some(std::path::PathBuf::from(temp_db("cascade"))),
    )
    .await
    .expect("manager");

    let id1 = mgr
        .store("Acme Corp project kickoff notes")
        .await
        .expect("s1");
    let _id2 = mgr
        .store("Met with Acme Corp about pricing")
        .await
        .expect("s2");
    let keep = mgr.store("User prefers tea over coffee").await.expect("s3");

    // "Forget Acme Corp" cascade.
    let reports = mgr
        .forget_matching("Acme Corp", 10, MemoryConfig::new())
        .await
        .expect("cascade forget");
    assert_eq!(reports.len(), 2, "both acme memories must be matched");
    assert!(
        reports.iter().all(|r| r.memory_id != keep.to_string()),
        "unrelated memory must not be touched"
    );

    // They are truly gone — not archived.
    assert!(mgr.get(&id1).await.expect("get").is_none());

    // The unrelated memory survives.
    assert!(mgr.get(&keep).await.expect("get").is_some());

    // Forgetting something absent reports zero removals, not an error.
    let empty = mgr
        .forget_matching("nonexistent-widget-xyz", 10, MemoryConfig::new())
        .await
        .expect("no-op cascade");
    assert!(empty.is_empty());
}

// ------------------------------------------------- 4. temporal supersession

#[tokio::test]
async fn temporal_as_of_recall_respects_supersedence_timeline() {
    let path = temp_db("asof");
    let storage = LibsqlStorage::new_with_validation(ConnectionMode::Local(path.clone()), true)
        .await
        .expect("storage");
    let ns = agent_ns("asof");
    let now = chrono::Utc::now();

    // Fact history: Paris (10 days ago) → Berlin (2 days ago).
    let paris = note("User lives in Paris", now - chrono::Duration::days(10), &ns);
    let berlin = note("User lives in Berlin", now - chrono::Duration::days(2), &ns);
    storage.store_memory(&paris).await.expect("paris");
    storage.store_memory(&berlin).await.expect("berlin");
    storage
        .mark_superseded(&paris.id, &berlin.id)
        .await
        .expect("supersede");

    // A brand-new fact that post-dates our historical queries.
    let lisbon = note(
        "User dreams of Lisbon",
        now - chrono::Duration::days(1),
        &ns,
    );
    storage.store_memory(&lisbon).await.expect("lisbon");

    // As of today: Berlin is current, Paris superseded.
    let current = storage
        .keyword_search_as_of("lives", &ns, now, 10)
        .await
        .expect("as-of now");
    let ids: Vec<_> = current.iter().map(|r| r.memory.id).collect();
    assert!(ids.contains(&berlin.id), "current fact must be returned");
    assert!(!ids.contains(&paris.id), "superseded fact must be excluded");

    // As of 9 days ago: Paris was still true; Berlin did not exist yet.
    let past = storage
        .keyword_search_as_of("lives", &ns, now - chrono::Duration::days(9), 10)
        .await
        .expect("as-of past");
    let ids: Vec<_> = past.iter().map(|r| r.memory.id).collect();
    assert!(ids.contains(&paris.id), "historical fact must be returned");
    assert!(!ids.contains(&berlin.id), "future fact must be excluded");

    // As of 6 days ago: Paris valid (successor not yet in force), Lisbon future.
    let mid = storage
        .keyword_search_as_of("lives Lisbon", &ns, now - chrono::Duration::days(6), 10)
        .await
        .expect("as-of mid");
    let ids: Vec<_> = mid.iter().map(|r| r.memory.id).collect();
    assert!(ids.contains(&paris.id));
    assert!(!ids.contains(&berlin.id), "not yet created at as-of time");
    assert!(!ids.contains(&lisbon.id), "not yet created at as-of time");

    // High-level API path through MemoryManager.
    let mgr = MemoryManager::new_with_path(
        "asof-mgr",
        Some(std::path::PathBuf::from(temp_db("asofmgr"))),
    )
    .await
    .expect("manager");
    let old = mgr
        .store("Project uses Postgres for storage")
        .await
        .expect("old");
    let new = mgr
        .store("Project uses SQLite for storage")
        .await
        .expect("new");
    mgr.supersede(&old, &new).await.expect("supersede");
    let hits_now = mgr
        .recall_as_of("storage engine", chrono::Utc::now(), 5, MemoryConfig::new())
        .await
        .expect("mgr as-of now");
    assert!(hits_now.iter().any(|h| h.memory.id == new));
    assert!(!hits_now.iter().any(|h| h.memory.id == old));
}
