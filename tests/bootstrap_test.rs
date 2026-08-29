use chrono::Utc;
use mnemosyne_core::{
    build_bootstrap, BootstrapRequest, ConnectionMode, ConstraintProposalService, LibsqlStorage,
    MemoryClass, MemoryId, MemoryNote, MemoryType, Namespace, StorageBackend,
};
use tempfile::TempDir;

fn memory(
    namespace: Namespace,
    content: &str,
    memory_type: MemoryType,
    tags: &[&str],
    importance: u8,
    confidence: f32,
) -> MemoryNote {
    let now = Utc::now();
    MemoryNote {
        id: MemoryId::new(),
        namespace,
        created_at: now,
        updated_at: now,
        content: content.into(),
        summary: content.into(),
        keywords: vec!["rust".into(), "authentication".into()],
        tags: tags.iter().map(|tag| (*tag).into()).collect(),
        context: String::new(),
        memory_type,
        memory_class: MemoryClass::Knowledge,
        provenance: None,
        importance,
        confidence,
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

async fn storage() -> LibsqlStorage {
    let path = format!("/tmp/mnemosyne_bootstrap_{}.db", uuid::Uuid::new_v4());
    LibsqlStorage::new_with_validation(ConnectionMode::Local(path), true)
        .await
        .expect("create storage")
}

#[tokio::test]
async fn bootstrap_separates_approved_constraints_facts_and_guardrails() {
    let store = storage().await;
    let namespace = Namespace::Project {
        name: "demo".into(),
    };
    store
        .store_memory(&memory(
            namespace.clone(),
            "Do not modify production credentials.",
            MemoryType::Constraint,
            &["manual"],
            10,
            1.0,
        ))
        .await
        .unwrap();
    store
        .store_memory(&memory(
            namespace.clone(),
            "Extracted constraint must not enter bootstrap automatically.",
            MemoryType::Constraint,
            &["extracted"],
            10,
            1.0,
        ))
        .await
        .unwrap();
    store
        .store_memory(&memory(
            namespace.clone(),
            "The authentication service uses Rust.",
            MemoryType::Reference,
            &["architecture"],
            8,
            0.9,
        ))
        .await
        .unwrap();
    store
        .store_memory(&memory(
            namespace.clone(),
            "Authentication changes require running the full test suite.",
            MemoryType::BugFix,
            &["reasoning_strategy", "reasoning_guardrail"],
            8,
            0.9,
        ))
        .await
        .unwrap();

    let project = TempDir::new().unwrap();
    std::process::Command::new("git")
        .args(["init"])
        .current_dir(project.path())
        .output()
        .unwrap();
    std::fs::create_dir_all(project.path().join(".claude/skills")).unwrap();
    std::fs::write(
        project.path().join(".claude/skills/rust-review.md"),
        "---\nname: rust-review\nkeywords: rust, review\ndescription: Review Rust changes\n---\nRun the Rust review workflow.\n",
    )
    .unwrap();

    let response = build_bootstrap(
        &store,
        BootstrapRequest {
            project: Some(project.path().to_path_buf()),
            namespace: Some(namespace),
            task: "review Rust authentication".into(),
            agent: Some("hermes".into()),
            capability: Some("review".into()),
            budget_tokens: 3500,
            min_confidence: 0.5,
        },
    )
    .await
    .unwrap();

    assert_eq!(response.schema_version, "bootstrap.v1");
    assert_eq!(response.agent.as_deref(), Some("hermes"));
    assert_eq!(response.constraints.len(), 1);
    assert_eq!(response.constraints[0].status, "approved");
    assert!(response.constraints[0]
        .text
        .contains("Do not modify production"));
    assert!(response.facts.iter().any(|item| item.text.contains("Rust")));
    assert!(response
        .guardrails
        .iter()
        .any(|item| item.text.contains("full test suite")));
    assert_eq!(response.skills.len(), 1);
    assert_eq!(response.skills[0].name, "rust-review");
    assert!(response.skills[0].path.contains("rust-review.md"));
    assert!(response.budget.used <= response.budget.requested);
}

#[tokio::test]
async fn approved_constraint_proposals_enter_bootstrap_only_after_owner_review() {
    let store = std::sync::Arc::new(storage().await);
    let namespace = Namespace::Project {
        name: "demo".into(),
    };
    let source = memory(
        namespace.clone(),
        "The authentication review found that production credentials must not be changed.",
        MemoryType::Reference,
        &["review"],
        8,
        0.9,
    );
    let source_id = source.id;
    store.store_memory(&source).await.unwrap();

    let service = ConstraintProposalService::new(store.clone());
    let proposal = service
        .propose(
            &namespace,
            "Do not modify production credentials.",
            "deployment",
            10,
            None,
            vec![source_id],
            vec!["production credentials must not be changed".into()],
            "hermes",
            "alice",
        )
        .await
        .unwrap();
    assert_eq!(proposal.status.as_str(), "proposed");

    let before = build_bootstrap(
        store.as_ref(),
        BootstrapRequest {
            project: None,
            namespace: Some(namespace.clone()),
            task: "review authentication".into(),
            agent: None,
            capability: None,
            budget_tokens: 1000,
            min_confidence: 0.5,
        },
    )
    .await
    .unwrap();
    assert!(before.constraints.is_empty());

    assert!(service
        .approve(&proposal.id, "mallory", None)
        .await
        .is_err());
    let approved = service.approve(&proposal.id, "alice", None).await.unwrap();
    assert_eq!(approved.status.as_str(), "approved");

    let after = build_bootstrap(
        store.as_ref(),
        BootstrapRequest {
            project: None,
            namespace: Some(namespace),
            task: "review authentication".into(),
            agent: None,
            capability: None,
            budget_tokens: 1000,
            min_confidence: 0.5,
        },
    )
    .await
    .unwrap();
    assert_eq!(after.constraints.len(), 1);
    assert_eq!(after.constraints[0].id, proposal.id);
    assert_eq!(after.constraints[0].status, "approved");

    let superseded = service
        .supersede(&proposal.id, "alice", Some("replaced by deployment policy"))
        .await
        .unwrap();
    assert_eq!(superseded.status.as_str(), "superseded");
    let retired = build_bootstrap(
        store.as_ref(),
        BootstrapRequest {
            project: None,
            namespace: Some(Namespace::Project {
                name: "demo".into(),
            }),
            task: "review authentication".into(),
            agent: None,
            capability: None,
            budget_tokens: 1000,
            min_confidence: 0.5,
        },
    )
    .await
    .unwrap();
    assert!(retired.constraints.is_empty());
}

#[tokio::test]
async fn bootstrap_abstains_when_task_has_no_matches() {
    let store = storage().await;
    let response = build_bootstrap(
        &store,
        BootstrapRequest {
            project: None,
            namespace: Some(Namespace::Project {
                name: "empty".into(),
            }),
            task: "quantum accounting".into(),
            agent: None,
            capability: None,
            budget_tokens: 100,
            min_confidence: 0.5,
        },
    )
    .await
    .unwrap();

    assert!(response.facts.is_empty());
    assert!(response
        .abstentions
        .iter()
        .any(|reason| reason == "facts:no_confident_match"));
    assert!(response.budget.used <= 100);
}
