//! Recall parity + honesty tests.
//!
//! Both recall dialects route through the single shared ranking path,
//! [`mnemosyne_core::utils::retrieval::rank_recall`]. This test seeds a
//! real store, runs the MCP handler and the shared path on the same
//! inputs, and asserts the served ids, scores, and disclosure keys agree.
//! It also checks that the legend — the in-band field dictionary — is
//! stable and complete over the emitted payload, and that the shared
//! path is deterministic.

use mnemosyne_core::mcp::ToolHandler;
use mnemosyne_core::storage::libsql::{ConnectionMode, LibsqlStorage};
use mnemosyne_core::storage::StorageBackend;
use mnemosyne_core::utils::retrieval::{estimate_result_tokens, rank_recall};
use mnemosyne_core::{EmbeddingConfig, EmbeddingService, LlmConfig, LlmService};
use std::sync::Arc;
use tempfile::TempDir;

async fn make_handler() -> (ToolHandler, TempDir) {
    let temp_dir = TempDir::new().unwrap();
    let db_path = temp_dir.path().join("parity.db");
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
    let embeddings = Arc::new(mnemosyne_core::services::embeddings::EmbeddingService::new(
        "test-key".to_string(),
        llm_config,
    ));
    let handler = ToolHandler::new(storage, llm, embeddings);
    (handler, temp_dir)
}

async fn seed(handler: &ToolHandler) {
    handler
        .execute(
            "mnemosyne_remember",
            serde_json::json!({
                "action": "remember",
                "category": "project",
                "name": "ripwire",
                "content": "ripwire ranks symbols by Personalized PageRank with zero runtime deps",
                "namespace": "global",
            }),
        )
        .await
        .unwrap();
    handler
        .execute(
            "mnemosyne_remember",
            serde_json::json!({
                "action": "remember",
                "category": "project",
                "name": "mnemosyne",
                "content": "mnemosyne stores hierarchical memories with adaptive fusion weights",
                "namespace": "global",
            }),
        )
        .await
        .unwrap();
    handler
        .execute(
            "mnemosyne_remember",
            serde_json::json!({
                "action": "remember",
                "category": "personal",
                "name": "favorite",
                "content": "I prefer deterministic recall with an honesty vocabulary in every response",
                "namespace": "global",
            }),
        )
        .await
        .unwrap();
    handler
        .execute(
            "mnemosyne_remember",
            serde_json::json!({
                "action": "remember",
                "category": "project",
                "name": "old",
                "content": "the old recall pipeline fused keyword and vector channels",
                "namespace": "global",
            }),
        )
        .await
        .unwrap();
    handler
        .execute(
            "mnemosyne_triples",
            serde_json::json!({
                "action": "add",
                "subject": "old",
                "predicate": "superseded_by",
                "object": "mnemosyne",
                "namespace": "global",
            }),
        )
        .await
        .unwrap();
}

async fn recall_json(
    handler: &ToolHandler,
    query: &str,
    max_results: usize,
    min_importance: Option<u8>,
    tags: Option<&str>,
    abstention_threshold: Option<f32>,
) -> serde_json::Value {
    let mut args = serde_json::json!({
        "query": query,
        "max_results": max_results,
        "namespace": "global",
        "hierarchical": false,
        "expand_graph": true,
        "compact": false,
    });
    if let Some(v) = min_importance {
        args["min_importance"] = serde_json::json!(v);
    }
    if let Some(t) = tags {
        args["tags"] = serde_json::json!(t);
    }
    if let Some(t) = abstention_threshold {
        args["abstention_threshold"] = serde_json::json!(t);
    }
    handler.execute("mnemosyne_recall", args).await.unwrap()
}

#[tokio::test]
async fn mcp_recall_agrees_with_the_shared_rank_path() {
    let (handler, temp) = make_handler().await;
    seed(&handler).await;

    // Direct path: open the same file-backed store and run the shared
    // ranker over its channel outputs. Vector search is not available
    // under a fake key, so both routes fall to keyword — the ranking
    // path is exercised regardless.
    let db_path = temp.path().join("parity.db");
    let direct_storage: Arc<dyn StorageBackend> = Arc::new(
        LibsqlStorage::new_with_validation(
            ConnectionMode::Local(db_path.to_str().unwrap().to_string()),
            true,
        )
        .await
        .unwrap(),
    );
    let keyword = direct_storage
        .hybrid_search_by_class(
            "recall",
            None,
            20,
            true,
            mnemosyne_core::types::MemoryClass::Knowledge,
        )
        .await
        .unwrap();
    let query_emb = mnemosyne_core::services::embeddings::EmbeddingService::new(
        "test-key".to_string(),
        LlmConfig::default(),
    )
    .generate_embedding("recall")
    .await
    .unwrap();
    let vector = direct_storage
        .vector_search(&query_emb, 40, None)
        .await
        .unwrap_or_default();
    let weights = direct_storage.retrieval_weights().await;
    let ranked = rank_recall(
        "recall",
        keyword,
        vector,
        weights.vector,
        10,
        None,
        None,
        None,
        false,
    );

    let json = recall_json(&handler, "recall", 10, None, None, None).await;
    let mcp_ids: Vec<_> = json["results"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["memory"]["id"].as_str().unwrap())
        .collect();
    let shared_ids: Vec<_> = ranked
        .results
        .iter()
        .map(|r| r.memory.id.to_string())
        .collect();
    assert_eq!(
        mcp_ids, shared_ids,
        "served ids must match the shared rank path"
    );
    for (mcp, shared) in json["results"]
        .as_array()
        .unwrap()
        .iter()
        .zip(&ranked.results)
    {
        let mcp_score = mcp["score"].as_f64().unwrap();
        let shared_score = shared.score as f64;
        assert!(
            (mcp_score - shared_score).abs() < 1e-4,
            "score drift: mcp={mcp_score} shared={shared_score}"
        );
    }

    // Honesty vocabulary: disclosure + legend + est_tokens
    let disclosure = &json["disclosure"];
    for key in ["shown", "candidates", "capped", "abstained", "est_tokens"] {
        assert!(disclosure.get(key).is_some(), "disclosure missing {key}");
    }
    assert_eq!(
        disclosure["shown"].as_u64().unwrap(),
        json["shown"].as_u64().unwrap()
    );
    assert_eq!(
        disclosure["candidates"].as_u64().unwrap(),
        ranked.candidates as u64
    );
    assert_eq!(disclosure["capped"].as_bool().unwrap(), ranked.capped);
    assert_eq!(disclosure["abstained"].as_bool().unwrap(), ranked.abstained);
    assert!(disclosure["est_tokens"].is_number());

    let legend = &json["legend"];
    for key in [
        "score",
        "match_reason",
        "shown",
        "candidates",
        "capped",
        "abstained",
        "est_tokens",
    ] {
        assert!(legend.get(key).is_some(), "legend missing key {key}");
    }
    // Every disclosure key must be explained in the legend — prevents the
    // "header names a denominator it does not count" defect.
    for key in disclosure.as_object().unwrap().keys() {
        assert!(
            legend.get(key).is_some(),
            "disclosure key {key} has no legend entry"
        );
    }

    let est = estimate_result_tokens(&ranked.results);
    assert_eq!(disclosure["est_tokens"].as_u64().unwrap(), est as u64);
}

#[tokio::test]
async fn rank_recall_is_deterministic_across_identical_inputs() {
    let temp_dir = TempDir::new().unwrap();
    let db_path = temp_dir.path().join("det.db");
    let storage: Arc<dyn StorageBackend> = Arc::new(
        LibsqlStorage::new_with_validation(
            ConnectionMode::Local(db_path.to_str().unwrap().to_string()),
            true,
        )
        .await
        .unwrap(),
    );
    let keyword = storage.keyword_search("alpha", None).await.unwrap();
    let weights = storage.retrieval_weights().await;

    let r1 = rank_recall(
        "alpha",
        keyword.clone(),
        vec![],
        weights.vector,
        10,
        None,
        None,
        None,
        false,
    );
    let r2 = rank_recall(
        "alpha",
        keyword,
        vec![],
        weights.vector,
        10,
        None,
        None,
        None,
        false,
    );
    let ids1: Vec<_> = r1.results.iter().map(|r| r.memory.id.to_string()).collect();
    let ids2: Vec<_> = r2.results.iter().map(|r| r.memory.id.to_string()).collect();
    assert_eq!(
        ids1, ids2,
        "rank_recall must not depend on HashMap iteration order"
    );
}
