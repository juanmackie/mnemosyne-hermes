//! Memory recall/query command

use mnemosyne_core::utils::retrieval::{estimate_result_tokens, recall_disclosure, recall_legend};
use mnemosyne_core::{build_memory_context_block, is_trivial_prompt, RecallBundle, RecallChannel};
use mnemosyne_core::{
    embeddings::{fallback_embedding_warning, remote_embedding_config},
    orchestration::events::AgentEvent,
    utils::string::truncate_at_char_boundary,
    ConnectionMode, EmbeddingConfig, EmbeddingService, LibsqlStorage, LocalEmbeddingService,
    RemoteEmbeddingService, StorageBackend,
};
use std::sync::Arc;
use tracing::debug;

use super::event_bridge;
use super::helpers::{get_db_path, parse_namespace};

/// Handle memory recall command
#[allow(clippy::too_many_arguments)]
pub async fn handle(
    query: String,
    namespace: Option<String>,
    limit: usize,
    min_importance: Option<u8>,
    tags: Option<String>,
    format: String,
    global_db_path: Option<String>,
    hierarchical: bool,
    trace: bool,
    budget_tokens: Option<usize>,
    abstain_below: Option<f32>,
) -> mnemosyne_core::error::Result<()> {
    let start_time = std::time::Instant::now();

    // Emit CLI command started event
    event_bridge::emit_command_started(
        "recall",
        vec![format!("--query={}", query), format!("--limit={}", limit)],
    )
    .await;

    // Initialize storage and services
    let db_path = get_db_path(global_db_path);
    let storage = LibsqlStorage::new(ConnectionMode::Local(db_path.clone())).await?;

    // Parse namespace strictly so a typo cannot expose global memories.
    let ns = namespace.as_deref().map(parse_namespace).transpose()?;

    // Perform hybrid search (keyword + vector + graph)
    let keyword_results = storage
        .hybrid_search(&query, ns.clone(), limit * 2, true)
        .await?;

    // Vector search credential resolution. The remote (Voyage) provider is used
    // ONLY when an explicit Voyage credential (MNEMOSYNE_VOYAGE_API_KEY) is
    // configured; an Anthropic LLM key never routes here. Otherwise we default
    // to local embeddings, so a configured Anthropic key no longer blocks the
    // local fallback path.
    let mut embedding_mode = "unavailable";
    let mut embedding_warning = None;

    // Vector search. Voyage remote embeddings are tried only when an explicit
    // Voyage credential is configured; otherwise local embeddings are used.
    // Dispatch through the StorageBackend trait so this path returns full
    // SearchResult objects and avoids the per-ID fetch that the inherent
    // LibsqlStorage::vector_search would trigger.
    let vector_results: Vec<mnemosyne_core::types::SearchResult> =
        if let Some((voyage_key, model, base_url)) = remote_embedding_config() {
            match RemoteEmbeddingService::new(voyage_key, model, base_url) {
                Ok(embedding_service) => {
                    embedding_mode = "voyage";
                    match embedding_service.embed(&query).await {
                        Ok(query_embedding) => (&storage as &dyn StorageBackend)
                            // Wide candidate pool: fusion sees deep vector matches
                            // instead of only limit*2 nearest rows.
                            .vector_search(&query_embedding, limit * 4, ns.clone())
                            .await
                            .unwrap_or_default(),
                        Err(_) => Vec::new(),
                    }
                }
                Err(_) => Vec::new(),
            }
        } else {
            // No remote API key — try local embeddings for personal agents working offline.
            debug!("No API key — attempting local embedding for vector search");
            let local_config = EmbeddingConfig {
                show_download_progress: false,
                ..EmbeddingConfig::default()
            };
            match LocalEmbeddingService::new(local_config).await {
                Ok(emb) => {
                    if emb.uses_model_backed_embeddings() {
                        embedding_mode = "local-model";
                    } else {
                        embedding_mode = "deterministic-hash-fallback";
                        if let Ok(memory_count) = storage.count_memories(ns.clone()).await {
                            embedding_warning = fallback_embedding_warning(memory_count);
                            if let Some(warning) = &embedding_warning {
                                tracing::warn!("{}", warning);
                            }
                        }
                    }
                    // Hash fallback vectors preserve the API shape but are not
                    // semantic embeddings: collisions can pull unrelated rows
                    // above a strong lexical match. Keep deterministic offline
                    // recall on the ranked FTS signal until a model-backed local
                    // embedding is available.
                    if !emb.uses_model_backed_embeddings() {
                        Vec::new()
                    } else {
                        let emb_svc: Arc<dyn EmbeddingService> = Arc::new(emb);
                        match emb_svc.embed(&query).await {
                            Ok(query_embedding) => (&storage as &dyn StorageBackend)
                                // Wide candidate pool for local-model recall too.
                                .vector_search(&query_embedding, limit * 4, ns.clone())
                                .await
                                .unwrap_or_default(),
                            Err(e) => {
                                debug!("Local embedding generation failed: {}", e);
                                if format != "json" {
                                    eprintln!(
                                        "{} Local embedding failed, vector search skipped",
                                        mnemosyne_core::icons::status::warning()
                                    );
                                }
                                Vec::new()
                            }
                        }
                    }
                }
                Err(e) => {
                    debug!("Local embedding service unavailable: {}", e);
                    embedding_mode = "unavailable";
                    if format != "json" {
                        eprintln!(
                            "{} Local embeddings unavailable, using keyword search only",
                            mnemosyne_core::icons::status::warning()
                        );
                    }
                    Vec::new()
                }
            }
        };

    let retrieval_weights = storage.retrieval_weights().await;

    // Intent analysis first: chit-chat skips retrieval entirely. This is the
    // CLI entry policy and it gates before any candidate work is spent.
    if hierarchical {
        let plan = mnemosyne_core::intent::plan_queries(&query);
        if plan.should_skip_retrieval() {
            if format == "json" {
                println!(
                    "{}",
                    serde_json::json!({
                        "results": [],
                        "count": 0,
                        "skipped": true,
                        "skip_reason": plan.skip_reason,
                    })
                );
            } else {
                eprintln!(
                    "No retrieval needed ({})",
                    plan.skip_reason.as_deref().unwrap_or("intent analysis")
                );
            }
            return Ok(());
        }
    }

    // Client-side tag filter, applied by the shared ranking path after the
    // limit so it cannot silently widen the served set.
    let tag_filter: Option<Vec<String>> = tags.as_deref().map(|raw| {
        raw.split(',')
            .map(|tag| tag.trim().to_lowercase())
            .filter(|tag| !tag.is_empty())
            .collect()
    });

    // One ranking path serves both dialects: fuse -> coverage rescore ->
    // hierarchical re-rank over the whole pool -> limit -> filters -> abstain.
    // This used to be duplicated here and in the MCP handler, and the two
    // copies had already drifted (see tests/recall_parity.rs).
    let ranked = mnemosyne_core::utils::retrieval::rank_recall(
        &query,
        keyword_results,
        vector_results,
        retrieval_weights.vector,
        limit,
        min_importance,
        tag_filter.as_deref(),
        abstain_below,
        hierarchical,
    );
    let trajectory_json = ranked.trajectory_json.clone();
    let results = ranked.results;
    let result_count = results.len();
    let recall_candidates = ranked.candidates;
    let recall_capped = ranked.capped;
    let recall_abstained = ranked.abstained;
    let mut explain_trace =
        mnemosyne_core::utils::retrieval::RetrievalTrace::for_query(&query, retrieval_weights);
    explain_trace.namespace = ns.as_ref().map(ToString::to_string);
    explain_trace.keyword_candidates = ranked.keyword_candidates;
    explain_trace.vector_candidates = ranked.vector_candidates;
    explain_trace.graph_candidates = ranked.graph_candidates;
    if embedding_warning.is_some() {
        explain_trace
            .fallback_reasons
            .push("fallback_embeddings".to_string());
    }
    explain_trace.result_ids = results
        .iter()
        .map(|result| result.memory.id.to_string())
        .collect();
    if let Err(error) = storage.record_retrieval_trace(&explain_trace).await {
        debug!("Unable to persist CLI retrieval trace: {}", error);
    }

    // Agent-friendly fenced context block for prompt injection.
    if format == "context" {
        let factual = results
            .iter()
            .filter(|result| {
                result.score > 0.0
                    && !result
                        .memory
                        .tags
                        .iter()
                        .any(|tag| tag == "reasoning_strategy")
            })
            .cloned()
            .collect();
        let guidance = (&storage as &dyn StorageBackend)
            .interaction_policy_search(&query, 3)
            .await
            .unwrap_or_default();
        let reasoning = storage
            .search_reasoning_strategies(&query, ns.clone(), 1)
            .await
            .unwrap_or_default()
            .into_iter()
            .map(|hit| hit.result)
            .collect();
        let bundle = RecallBundle {
            factual: RecallChannel {
                results: factual,
                quota: 5,
                abstention_reason: None,
            },
            guidance: RecallChannel {
                results: guidance,
                quota: 3,
                abstention_reason: None,
            },
            reasoning: RecallChannel {
                results: reasoning,
                quota: 1,
                abstention_reason: None,
            },
            budget_tokens: budget_tokens.unwrap_or(512),
        };
        let block = build_memory_context_block(mnemosyne_core::render_recall_bundle(&bundle));
        if !block.is_empty() {
            println!("{block}");
        }
        // Emit recall executed event
        let duration_ms = start_time.elapsed().as_millis() as u64;
        let _ = event_bridge::emit_event(AgentEvent::RecallExecuted {
            query: query.clone(),
            result_count,
            duration_ms,
        })
        .await;
        return Ok(());
    }

    // Fast-path: trivial queries yield no memory context.
    if is_trivial_prompt(&query) {
        eprintln!("Query is a trivial greeting; nothing to recall.");
        return Ok(());
    }

    // Output results
    if format == "json" {
        let json_results: Vec<_> = results
            .iter()
            .map(|result| {
                let m = &result.memory;
                let score = result.score;
                let match_reason = &result.match_reason;
                serde_json::json!({
                    "id": m.id.to_string(),
                    "summary": m.summary,
                    "content": m.content,
                    "importance": m.importance,
                    "tags": m.tags,
                    "memory_type": format!("{:?}", m.memory_type),
                    "memory_class": format!("{:?}", m.memory_class),
                    "score": score,
                    "match_reason": match_reason,
                    "namespace": serde_json::to_string(&m.namespace).unwrap_or_default()
                })
            })
            .collect();

        let est_tokens = estimate_result_tokens(&results);
        let assembled = budget_tokens.map(|budget| {
            let candidates: Vec<mnemosyne_core::context_assembler::Candidate> = results
                .iter()
                .map(|result| {
                    mnemosyne_core::context_assembler::Candidate::new(
                        result.memory.id.to_string(),
                        result.memory.summary.clone(),
                        mnemosyne_core::hierarchy::l0_abstract_for(&result.memory),
                        mnemosyne_core::hierarchy::l1_overview_for(&result.memory),
                        result.memory.content.clone(),
                        result.score,
                    )
                })
                .collect();
            let plan = mnemosyne_core::context_assembler::assemble(&candidates, budget);
            mnemosyne_core::context_assembler::render_markdown(
                &plan,
                &format!("Recall Context: {}", query),
            )
        });

        println!(
            "{}",
            serde_json::json!({
                "disclosure": recall_disclosure(
                    recall_candidates,
                    recall_capped,
                    recall_abstained,
                    results.len(),
                    est_tokens,
                    abstain_below,
                ),
                "legend": recall_legend(),
                "results": json_results,
                "shown": results.len(),
                "candidates": recall_candidates,
                "capped": recall_capped,
                "count": json_results.len(),
                "trajectory": trajectory_json,
                "explain_trace": explain_trace,
                "embedding_mode": embedding_mode,
                "fallback_warning": embedding_warning,
                "assembled_context": assembled,
            })
        );
    } else if results.is_empty() {
        if recall_abstained {
            eprintln!(
                "No memories found — best score was below the abstention threshold (not found, not 'does not exist')."
            );
        } else {
            eprintln!("No memories found matching '{}'", query);
        }
    } else if recall_capped {
        eprintln!(
            "Showing {} of {} ranked candidates (limit reached).\n",
            results.len(),
            recall_candidates
        );
    } else {
        eprintln!("Found {} memories:\n", results.len());
        for (i, result) in results.iter().enumerate() {
            println!(
                "{}. {} (score: {:.2}, importance: {}/10)",
                i + 1,
                result.memory.summary,
                result.score,
                result.memory.importance
            );
            println!("   ID: {}", result.memory.id);
            println!("   Tags: {}", result.memory.tags.join(", "));
            println!(
                "   Content: {}\n",
                truncate_at_char_boundary(&result.memory.content, 100)
            );
        }
    }

    // Emit recall executed event
    let duration_ms = start_time.elapsed().as_millis() as u64;
    let recall_event = AgentEvent::RecallExecuted {
        query: query.clone(),
        result_count,
        duration_ms,
    };
    let _ = event_bridge::emit_event(recall_event).await;

    // Emit search performed event
    let search_event = AgentEvent::SearchPerformed {
        query: query.clone(),
        search_type: "hybrid".to_string(), // keyword + vector search
        result_count,
        duration_ms,
    };
    let _ = event_bridge::emit_event(search_event).await;

    // Emit command completed event
    event_bridge::emit_command_completed(
        "recall",
        duration_ms,
        format!("Found {} results for query '{}'", result_count, query),
    )
    .await;

    Ok(())
}
