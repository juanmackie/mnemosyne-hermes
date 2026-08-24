//! Integration tests for MCP parameter validation
//!
//! Verifies that MCP tools properly validate input parameters and reject invalid values

use mnemosyne_core::error::MnemosyneError;
use mnemosyne_core::mcp::ToolHandler;
use mnemosyne_core::services::embeddings::EmbeddingService;
use mnemosyne_core::services::llm::LlmService;
use mnemosyne_core::storage::libsql::{ConnectionMode, LibsqlStorage};
use mnemosyne_core::storage::StorageBackend;
use mnemosyne_core::LlmConfig;
use std::sync::Arc;
use tempfile::TempDir;

async fn create_test_handler() -> (ToolHandler, TempDir) {
    let temp_dir = TempDir::new().unwrap();
    let db_path = temp_dir.path().join("test_validation.db");

    let storage: Arc<dyn StorageBackend> = Arc::new(
        LibsqlStorage::new_with_validation(
            ConnectionMode::Local(db_path.to_str().unwrap().to_string()),
            true,
        )
        .await
        .unwrap(),
    );

    let llm_config = LlmConfig::default();
    let llm = Arc::new(LlmService::new(llm_config.clone()).unwrap());
    let embeddings = Arc::new(EmbeddingService::new("test-key".to_string(), llm_config));

    let handler = ToolHandler::new(storage, llm, embeddings);
    (handler, temp_dir)
}

#[tokio::test]
async fn test_hermes_aliases_are_advertised_and_dispatched() {
    let (handler, _temp) = create_test_handler().await;
    let names: std::collections::HashSet<_> = handler
        .list_tools()
        .into_iter()
        .map(|tool| tool.name)
        .collect();

    assert!(names.contains("mnemosyne.recall"));
    assert!(names.contains("mnemosyne_remember"));
    assert!(names.contains("mnemosyne_recall"));
    assert!(names.contains("mnemosyne_forget"));
    assert!(names.contains("mnemosyne_persona"));
    assert!(names.contains("mnemosyne_canonical"));
    assert!(names.contains("mnemosyne_triples"));

    let result = handler
        .execute("mnemosyne_recall", serde_json::json!({"query": ""}))
        .await;
    assert!(matches!(result, Err(MnemosyneError::ValidationError(_))));

    let canonical = handler
        .execute(
            "mnemosyne_canonical",
            serde_json::json!({"action": "invalid", "category": "identity", "name": "name"}),
        )
        .await;
    assert!(matches!(canonical, Err(MnemosyneError::ValidationError(_))));
}

#[tokio::test]
async fn test_canonical_and_triples_round_trip() {
    let (handler, _temp) = create_test_handler().await;

    let stored = handler
        .execute(
            "mnemosyne_canonical",
            serde_json::json!({
                "action": "remember",
                "category": "identity",
                "name": "name",
                "body": "The user goes by Ada.",
                "namespace": "global"
            }),
        )
        .await
        .unwrap();
    assert_eq!(stored["stored"], true);

    let recalled = handler
        .execute(
            "mnemosyne_canonical",
            serde_json::json!({
                "action": "recall",
                "category": "identity",
                "name": "name",
                "namespace": "global"
            }),
        )
        .await
        .unwrap();
    assert_eq!(recalled["found"], true);
    assert_eq!(recalled["memory"]["content"], "The user goes by Ada.");

    handler
        .execute(
            "mnemosyne_triples",
            serde_json::json!({
                "action": "add",
                "subject": "Ada",
                "predicate": "uses",
                "object": "Rust",
                "namespace": "global"
            }),
        )
        .await
        .unwrap();
    let triples = handler
        .execute(
            "mnemosyne_triples",
            serde_json::json!({
                "action": "query",
                "subject": "Ada",
                "predicate": "uses",
                "namespace": "global"
            }),
        )
        .await
        .unwrap();
    assert_eq!(triples["count"], 1);
    assert_eq!(triples["triples"][0]["content"], "Ada uses Rust");
}

#[tokio::test]
async fn test_recall_empty_query() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "query": "",
        "namespace": "global"
    });

    let result = handler.execute("mnemosyne.recall", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(msg.contains("query"), "Error should mention query field");
            assert!(msg.contains("empty"), "Error should mention empty");
        }
        _ => panic!(
            "Expected ValidationError for empty query, got: {:?}",
            result
        ),
    }
}

#[tokio::test]
async fn test_recall_whitespace_only_query() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "query": "   \t\n  ",
        "namespace": "global"
    });

    let result = handler.execute("mnemosyne.recall", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(msg.contains("query"), "Error should mention query field");
        }
        _ => panic!("Expected ValidationError for whitespace query"),
    }
}

#[tokio::test]
async fn test_recall_zero_max_results() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "query": "test",
        "namespace": "global",
        "max_results": 0
    });

    let result = handler.execute("mnemosyne.recall", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(
                msg.contains("max_results"),
                "Error should mention max_results"
            );
            assert!(msg.contains("at least 1"), "Error should mention minimum");
        }
        _ => panic!("Expected ValidationError for max_results=0"),
    }
}

#[tokio::test]
async fn test_recall_excessive_max_results() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "query": "test",
        "namespace": "global",
        "max_results": 5000
    });

    // Should succeed but cap at 1000 (tested via logs)
    let result = handler.execute("mnemosyne.recall", params).await;

    // Should not error, just cap silently
    assert!(
        result.is_ok(),
        "Large max_results should be capped, not error"
    );
}

#[tokio::test]
async fn test_recall_invalid_min_importance() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "query": "test",
        "namespace": "global",
        "min_importance": 15
    });

    let result = handler.execute("mnemosyne.recall", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(
                msg.contains("Importance"),
                "Error should mention importance"
            );
            assert!(msg.contains("1-10"), "Error should mention valid range");
        }
        _ => panic!("Expected ValidationError for min_importance=15"),
    }
}

#[tokio::test]
async fn test_remember_empty_content() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "content": "",
        "namespace": "global"
    });

    let result = handler.execute("mnemosyne.remember", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(
                msg.contains("content"),
                "Error should mention content field"
            );
            assert!(msg.contains("empty"), "Error should mention empty");
        }
        _ => panic!("Expected ValidationError for empty content"),
    }
}

#[tokio::test]
async fn test_remember_excessive_content_length() {
    let (handler, _temp) = create_test_handler().await;

    // Create 200KB content (exceeds 100KB limit)
    let large_content = "a".repeat(200_000);
    let params = serde_json::json!({
        "content": large_content,
        "namespace": "global"
    });

    let result = handler.execute("mnemosyne.remember", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(
                msg.contains("Content too large"),
                "Error should mention size"
            );
            assert!(msg.contains("100000"), "Error should mention max size");
        }
        _ => panic!("Expected ValidationError for large content"),
    }
}

#[tokio::test]
async fn test_remember_invalid_importance() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "content": "Test memory",
        "namespace": "global",
        "importance": 0
    });

    let result = handler.execute("mnemosyne.remember", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(
                msg.contains("Importance"),
                "Error should mention importance"
            );
            assert!(msg.contains("1-10"), "Error should mention valid range");
        }
        _ => panic!("Expected ValidationError for importance=0"),
    }
}

#[tokio::test]
async fn test_context_empty_memory_ids() {
    let (handler, _temp) = create_test_handler().await;

    let params = serde_json::json!({
        "memory_ids": []
    });

    let result = handler.execute("mnemosyne.context", params).await;

    match result {
        Err(MnemosyneError::ValidationError(msg)) => {
            assert!(
                msg.contains("memory_ids"),
                "Error should mention memory_ids"
            );
            assert!(msg.contains("empty"), "Error should mention empty");
        }
        _ => panic!("Expected ValidationError for empty memory_ids"),
    }
}

#[tokio::test]
async fn test_valid_parameters_accepted() {
    let (handler, _temp) = create_test_handler().await;

    // Test valid recall parameters
    let params = serde_json::json!({
        "query": "valid query",
        "namespace": "global",
        "max_results": 10,
        "min_importance": 5
    });

    let result = handler.execute("mnemosyne.recall", params).await;
    // May fail due to test API key, but shouldn't fail validation
    if let Err(MnemosyneError::ValidationError(msg)) = result {
        panic!("Valid parameters should not fail validation: {}", msg);
    }
}
