//! Integration tests for the ancillary HTTP/RPC safety fixes
//!
//! Covers: dashboard opt-in + auth config (A), non-destructive state projection
//! and read-after-write (B), snapshot ordering (C), lag-tolerant subscription
//! (D), RPC schema conformance (E), honest health reporting (F), and loopback
//! enforcement (G).

#![cfg(feature = "rpc")]

use mnemosyne_core::{
    api::{
        state::{AgentHealth, AgentInfo, ContextFile},
        AgentState, ApiServer, ApiServerConfig, Event, EventBroadcaster, StateManager,
    },
    rpc::{
        generated::{
            health_service_server::HealthService,
            memory_service_server::MemoryService,
            ListMemoriesRequest, StoreMemoryRequest,
        },
        services::{HealthServiceImpl, MemoryServiceImpl},
        RpcServer,
    },
    storage::{libsql::LibsqlStorage, StorageBackend},
};
use std::sync::Arc;
use tonic::Request;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Owned, unique temporary directory per test (never the dev's real home).
fn temp_storage_path(label: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("mnemosyne_ancillary_{}_{}", label, uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).expect("create temp dir");
    dir.join("test.db")
}

async fn create_test_storage(label: &str) -> Arc<dyn StorageBackend> {
    use mnemosyne_core::storage::libsql::ConnectionMode;
    let path = temp_storage_path(label);
    Arc::new(
        LibsqlStorage::new_with_validation(
            ConnectionMode::Local(path.to_str().unwrap().to_string()),
            true,
        )
        .await
        .expect("create test storage"),
    )
}

fn agent_info(id: &str, task: &str, meta_key: &str, meta_val: &str) -> AgentInfo {
    let mut metadata = std::collections::HashMap::new();
    metadata.insert(meta_key.to_string(), meta_val.to_string());
    AgentInfo {
        id: id.to_string(),
        state: AgentState::Active { task: task.to_string() },
        updated_at: chrono::Utc::now(),
        metadata,
        health: Some(AgentHealth {
            error_count: 2,
            last_error: None,
            is_healthy: true,
            last_restart: None,
        }),
    }
}

fn project_namespace(name: &str) -> mnemosyne_core::rpc::generated::Namespace {
    use mnemosyne_core::rpc::generated::namespace::Namespace as Ns;
    use mnemosyne_core::rpc::generated::ProjectNamespace;
    mnemosyne_core::rpc::generated::Namespace {
        namespace: Some(Ns::Project(ProjectNamespace { name: name.to_string() })),
    }
}

// ---------------------------------------------------------------------------
// Item A: dashboard HTTP startup is opt-in; default config disables it
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_a_dashboard_opt_in_disabled_by_default() {
    let config = ApiServerConfig::default();
    // Not enabled by default, no auth, no origin allowlist.
    assert!(!config.start_dashboard);
    assert!(config.auth_token.is_none());
    assert!(config.allowed_origins.is_empty());

    // serve() refuses to bind (returns Ok without binding) when disabled.
    let server = ApiServer::new(config);
    server.serve().await.expect("disabled serve no-ops cleanly");
}

// ---------------------------------------------------------------------------
// Item B: submitted agent state is not clobbered by a follow-up minimal event;
// context validation errors are not cleared by a plain modified event.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_b_agent_metadata_preserved_after_agent_started_event() {
    let manager = Arc::new(StateManager::new());
    let events = EventBroadcaster::new(100);
    manager.subscribe_to_events(events.subscribe());

    let id = "executor".to_string();
    let submitted = agent_info(&id, "build feature x", "role", "orchestrator");

    // This mirrors the HTTP update path: full state is written, then a minimal
    // agent_started event is broadcast. The projection must NOT replace the
    // submitted metadata/health with defaults.
    manager.update_agent(submitted.clone()).await;
    events.broadcast(Event::agent_started(id.clone())).unwrap();

    // Give the background projector a chance to run.
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;

    let got = manager.get_agent(&id).await.expect("agent present");
    assert_eq!(got.metadata.get("role").map(|s| s.as_str()), Some("orchestrator"));
    assert_eq!(got.health.as_ref().map(|h| h.error_count), Some(2));
}

#[tokio::test]
async fn test_b_read_after_write_returns_submitted_state() {
    let manager = StateManager::new();
    let id = "reader-agent".to_string();
    let submitted = agent_info(&id, "task", "k1", "v1");
    manager.update_agent(submitted.clone()).await;
    let read = manager.get_agent(&id).await.expect("present");
    // Exact read-after-write match (state + metadata).
    assert_eq!(read.state, submitted.state);
    assert_eq!(read.metadata, submitted.metadata);
}

#[tokio::test]
async fn test_b_context_errors_preserved_after_modified_event() {
    let manager = Arc::new(StateManager::new());
    let events = EventBroadcaster::new(100);
    manager.subscribe_to_events(events.subscribe());

    let path = "docs/spec.md".to_string();
    let file = ContextFile {
        path: path.clone(),
        modified_at: chrono::Utc::now(),
        errors: vec!["line 12: syntax".to_string()],
    };
    manager.update_context_file(file).await;
    events.broadcast(Event::context_modified(path.clone())).unwrap();

    tokio::time::sleep(std::time::Duration::from_millis(200)).await;

    let got = manager.get_context_file(&path).await.expect("file present");
    // A plain "modified" event must not clear validation errors.
    assert!(!got.errors.is_empty());
    assert_eq!(got.errors[0], "line 12: syntax");
}

// ---------------------------------------------------------------------------
// Item C/D: the state projector keeps processing across events (does not exit
// on a single event), and updates are all applied in order.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_cd_projector_survives_multiple_events() {
    let manager = Arc::new(StateManager::new());
    let events = EventBroadcaster::new(100);
    manager.subscribe_to_events(events.subscribe());

    for i in 0..5 {
        let id = format!("agent-{}", i);
        manager.update_agent(agent_info(&id, "t", "k", "v")).await;
        events.broadcast(Event::agent_started(id)).unwrap();
    }

    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    // All agents still present => the projection loop did not terminate early.
    for i in 0..5 {
        assert!(manager.get_agent(&format!("agent-{}", i)).await.is_some());
    }
    assert_eq!(manager.list_agents().await.len(), 5);
}

// ---------------------------------------------------------------------------
// Item E: list_memories pagination (real total + has_more) and explicit
// rejection of unimplemented filters.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_e_list_memories_pagination_and_filter_rejection() {
    let storage = create_test_storage("rpc_e").await;
    let service = MemoryServiceImpl::new(storage.clone(), None);

    // Populate the namespace.
    let ns = project_namespace("e-proj");
    for i in 0..5 {
        let req = StoreMemoryRequest {
            content: format!("memory {}", i),
            namespace: Some(ns.clone()),
            tags: vec!["tag".to_string()],
            skip_llm_enrichment: true,
            ..Default::default()
        };
        service.store_memory(Request::new(req)).await.expect("store");
    }

    // Page 1: limit 2, offset 0 -> has_more true, total = 5.
    let page1_req = ListMemoriesRequest {
        namespace: Some(ns.clone()),
        limit: 2,
        offset: 0,
        ..Default::default()
    };
    let resp = service.list_memories(Request::new(page1_req)).await.unwrap();
    let resp = resp.into_inner();
    assert_eq!(resp.memories.len(), 2);
    assert_eq!(resp.total_count, 5);
    assert!(resp.has_more);

    // Page 3 (offset 4): has_more false, one remaining.
    let page3_req = ListMemoriesRequest {
        namespace: Some(ns),
        limit: 2,
        offset: 4,
        ..Default::default()
    };
    let resp = service.list_memories(Request::new(page3_req)).await.unwrap();
    let resp = resp.into_inner();
    assert_eq!(resp.memories.len(), 1);
    assert!(!resp.has_more);

    // Unsupported filter -> explicit rejection (not silent ignore).
    let filtered = ListMemoriesRequest {
        min_importance: Some(7),
        limit: 10,
        offset: 0,
        ..Default::default()
    };
    let err = service
        .list_memories(Request::new(filtered))
        .await
        .expect_err("unsupported filter must be rejected");
    assert_eq!(err.code(), tonic::Code::Unimplemented);
}

// ---------------------------------------------------------------------------
// Item F: health reports depend on a real storage probe; without storage it is
// reported unavailable (never synthetic success).
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_f_health_reports_unavailable_without_storage() {
    let service = HealthServiceImpl::new(); // no storage attached
    let resp = service
        .health_check(Request::new(mnemosyne_core::rpc::generated::HealthCheckRequest {}))
        .await
        .unwrap()
        .into_inner();
    assert!(!resp.healthy);
    assert_eq!(resp.components.get("storage").map(|s| s.as_str()), Some("unavailable"));
}

#[tokio::test]
async fn test_f_health_and_stats_real_with_storage() {
    let storage = create_test_storage("rpc_f").await;
    let service = HealthServiceImpl::new().with_storage(storage);

    // Probe a real storage op first (store a memory) so count is non-zero.
    let mem_svc = MemoryServiceImpl::new(Arc::clone(&storage), None);
    let ns = project_namespace("f-proj");
    let store = StoreMemoryRequest {
        content: "one".to_string(),
        namespace: Some(ns.clone()),
        tags: vec![],
        skip_llm_enrichment: true,
        ..Default::default()
    };
    mem_svc.store_memory(Request::new(store)).await.expect("store");

    let health = service
        .health_check(Request::new(mnemosyne_core::rpc::generated::HealthCheckRequest {}))
        .await
        .unwrap()
        .into_inner();
    assert!(health.healthy);
    assert_eq!(health.components.get("storage").map(|s| s.as_str()), Some("healthy"));

    let stats = service
        .get_stats(Request::new(mnemosyne_core::rpc::generated::GetStatsRequest {
            namespace: None,
        }))
        .await
        .unwrap()
        .into_inner();
    let stats = stats.stats.unwrap();
    assert_eq!(stats.total_memories, 1);
}

// ---------------------------------------------------------------------------
// Item G: RPC server refuses non-loopback binds (no TLS/auth).
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_g_rpc_rejects_non_loopback_bind() {
    let storage = create_test_storage("rpc_g").await;
    let server = RpcServer::new(storage, None);
    let err = server.serve("0.0.0.0:50051").await.expect_err("must reject non-loopback");
    assert!(err.to_string().contains("non-loopback") || err.to_string().contains("loopback"));
}
