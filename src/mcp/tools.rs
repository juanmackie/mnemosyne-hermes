//! MCP tool implementations
//!
//! Provides 8 core memory tools organized around the OODA loop:
//! - OBSERVE: recall, list
//! - ORIENT: graph, context
//! - DECIDE: remember, consolidate
//! - ACT: update, delete

use crate::error::{MnemosyneError, Result};
use crate::services::{EmbeddingService, LlmService};
use crate::storage::StorageBackend;
use crate::types::{MemoryId, MemoryNote, MemoryType, Namespace};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;
use tracing::{debug, info, warn};

/// Event sink for routing events to dashboard
#[derive(Clone)]
pub enum EventSink {
    /// Direct broadcaster (this process owns the API server)
    Local(crate::api::EventBroadcaster),
    /// HTTP forwarder (remote API server exists)
    Remote {
        client: reqwest::Client,
        api_url: String,
    },
    /// No event broadcasting available
    None,
}

impl EventSink {
    /// Emit an event (async, non-blocking)
    pub async fn emit(&self, event: crate::api::Event) -> Result<()> {
        match self {
            Self::Local(broadcaster) => {
                if let Err(e) = broadcaster.broadcast(event) {
                    debug!("Failed to broadcast event locally: {}", e);
                }
            }
            Self::Remote { client, api_url } => {
                let url = format!("{}/events/emit", api_url);
                // Fire and forget with short timeout (don't block MCP operations)
                let client = client.clone();
                let event_clone = event.clone();
                tokio::spawn(async move {
                    match tokio::time::timeout(
                        std::time::Duration::from_millis(100),
                        client.post(&url).json(&event_clone).send(),
                    )
                    .await
                    {
                        Ok(Ok(_)) => debug!("Event forwarded to remote API server"),
                        Ok(Err(e)) => debug!("Failed to forward event: {}", e),
                        Err(_) => debug!("Event forwarding timed out"),
                    }
                });
            }
            Self::None => {
                debug!("Event sink unavailable: {:?}", event.event_type);
            }
        }
        Ok(())
    }
}

/// Tool schema definition
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Tool {
    /// Tool name (e.g., "mnemosyne.recall")
    pub name: String,

    /// Human-readable description
    pub description: String,

    /// JSON Schema for input parameters
    pub input_schema: Value,
}

const MAX_PAGE_OFFSET: usize = 100_000;
const DEFAULT_CONTEXT_MAX_RESULTS: usize = 100;
const DEFAULT_GRAPH_MAX_RESULTS: usize = 100;
const MAX_CONTEXT_INPUT_IDS: usize = 1_000;
const MAX_GRAPH_SEEDS: usize = 1_000;
const MAX_GRAPH_HOPS: usize = 8;
const RECOMMENDED_ABSTENTION_THRESHOLD: f32 = 0.30;

#[derive(Debug, Clone, Copy)]
struct PageInfo {
    offset: usize,
    limit: usize,
    count: usize,
    has_more: bool,
}

fn paginate<T>(mut items: Vec<T>, offset: usize, limit: usize) -> (Vec<T>, PageInfo) {
    let total = items.len();
    let start = offset.min(total);
    let end = start.saturating_add(limit).min(total);
    let has_more = end < total;
    let page = items.drain(start..end).collect();

    (
        page,
        PageInfo {
            offset,
            limit,
            count: end.saturating_sub(start),
            has_more,
        },
    )
}

/// Tool handler that dispatches to appropriate implementation
pub struct ToolHandler {
    storage: Arc<dyn StorageBackend>,
    llm: Arc<LlmService>,
    embeddings: Arc<EmbeddingService>,
    event_sink: EventSink,
    /// Namespace supplied by the MCP process environment. Explicit tool
    /// arguments still override this value.
    default_namespace: Namespace,
}

impl ToolHandler {
    /// Create a new tool handler (no event broadcasting)
    pub fn new(
        storage: Arc<dyn StorageBackend>,
        llm: Arc<LlmService>,
        embeddings: Arc<EmbeddingService>,
    ) -> Self {
        Self::new_with_default_namespace(
            storage,
            llm,
            embeddings,
            EventSink::None,
            Namespace::Global,
        )
    }

    /// Create a new tool handler with event sink
    pub fn new_with_event_sink(
        storage: Arc<dyn StorageBackend>,
        llm: Arc<LlmService>,
        embeddings: Arc<EmbeddingService>,
        event_sink: EventSink,
    ) -> Self {
        Self::new_with_default_namespace(storage, llm, embeddings, event_sink, Namespace::Global)
    }

    /// Create a handler with the validated namespace configured for this MCP
    /// process. Explicit tool arguments still override this value.
    pub fn new_with_default_namespace(
        storage: Arc<dyn StorageBackend>,
        llm: Arc<LlmService>,
        embeddings: Arc<EmbeddingService>,
        event_sink: EventSink,
        default_namespace: Namespace,
    ) -> Self {
        Self {
            storage,
            llm,
            embeddings,
            event_sink,
            default_namespace,
        }
    }

    /// Create a new tool handler with event broadcasting (deprecated - use new_with_event_sink)
    #[deprecated(note = "Use new_with_event_sink instead")]
    pub fn new_with_events(
        storage: Arc<dyn StorageBackend>,
        llm: Arc<LlmService>,
        embeddings: Arc<EmbeddingService>,
        event_broadcaster: Option<crate::api::EventBroadcaster>,
    ) -> Self {
        let event_sink = match event_broadcaster {
            Some(broadcaster) => EventSink::Local(broadcaster),
            None => EventSink::None,
        };
        Self::new_with_default_namespace(storage, llm, embeddings, event_sink, Namespace::Global)
    }

    /// Get list of all available tools
    pub fn list_tools(&self) -> Vec<Tool> {
        let mut tools = vec![
            // OBSERVE tools
            Tool {
                name: "mnemosyne.recall".to_string(),
                description: "Search memories by semantic query, keywords, or tags. Returns ranked results with relevance scores.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (semantic or keyword)"
                        },
                        "namespace": {
                            "type": "string",
                            "description": "Optional namespace filter (e.g., 'project:myapp')"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results",
                            "default": 10
                        },
                        "min_importance": {
                            "type": "integer",
                            "description": "Minimum importance threshold (1-10)"
                        },
                        "abstention_threshold": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Optional minimum score required to return results. Below this threshold the tool abstains explicitly."
                        },
                        "expand_graph": {
                            "type": "boolean",
                            "default": true
                        },
                        "hierarchical": {
                            "type": "boolean",
                            "default": false
                        },
                        "budget_tokens": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional shared context assembly budget"
                        }
                    },
                    "required": ["query"]
                }),
            },
            Tool {
                name: "mnemosyne.list".to_string(),
                description: "List recent memories in a namespace. Useful for browsing memory history.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Namespace to list (e.g., 'project:myapp', 'global')"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of memories to return",
                            "default": 20
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Number of memories to skip before returning the page",
                            "default": 0
                        }
                    }
                }),
            },
            // ORIENT tools
            Tool {
                name: "mnemosyne.graph".to_string(),
                description: "Get memory graph starting from seed memory IDs. Traverses semantic links to build context.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "seed_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Starting memory IDs for graph traversal"
                        },
                        "max_hops": {
                            "type": "integer",
                            "description": "Maximum link hops from seed nodes",
                            "default": 2
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum memories returned (default 100)",
                            "default": 100
                        }
                    },
                    "required": ["seed_ids"]
                }),
            },
            Tool {
                name: "mnemosyne.context".to_string(),
                description: "Get full context for specific memory IDs, including linked memories and metadata.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "memory_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Memory IDs to retrieve"
                        },
                        "include_links": {
                            "type": "boolean",
                            "description": "Whether to include linked memories",
                            "default": true
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum memories returned after optional link expansion (default 100)",
                            "default": 100
                        }
                    },
                    "required": ["memory_ids"]
                }),
            },
            // DECIDE tools
            Tool {
                name: "mnemosyne.remember".to_string(),
                description: "Store a new memory with LLM enrichment. Automatically generates summary, keywords, tags, and semantic links.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Memory content to store"
                        },
                        "namespace": {
                            "type": "string",
                            "description": "Namespace (e.g., 'project:myapp', 'global')"
                        },
                        "importance": {
                            "type": "integer",
                            "description": "Importance level (1-10), if not provided LLM will determine"
                        },
                        "context": {
                            "type": "string",
                            "description": "Additional context about when/why this is relevant"
                        }
                    },
                    "required": ["content", "namespace"]
                }),
            },
            Tool {
                name: "mnemosyne.consolidate".to_string(),
                description: "Analyze and merge/supersede similar memories to prevent duplication.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "memory_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Memory IDs to consider for consolidation"
                        },
                        "namespace": {
                            "type": "string",
                            "description": "Optional namespace to search for candidates"
                        }
                    }
                }),
            },
            // ACT tools
            Tool {
                name: "mnemosyne.update".to_string(),
                description: "Update an existing memory's content, importance, or tags.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "type": "string",
                            "description": "Memory ID to update"
                        },
                        "content": {
                            "type": "string",
                            "description": "New content (triggers re-embedding)"
                        },
                        "importance": {
                            "type": "integer",
                            "description": "New importance level (1-10)"
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "New tags (replaces existing)"
                        },
                        "add_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Additional tags (appends to existing)"
                        }
                    },
                    "required": ["memory_id"]
                }),
            },
            Tool {
                name: "mnemosyne.delete".to_string(),
                description: "Archive (soft delete) a memory. Does not permanently delete, can be restored.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "type": "string",
                            "description": "Memory ID to archive"
                        }
                    },
                    "required": ["memory_id"]
                }),
            },
            Tool {
                name: "mnemosyne.used".to_string(),
                description: "Report which recalled memories were actually useful. Strengthens future ranking via the online relevance learner.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "memory_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs of memories that were used/helpful"
                        }
                    },
                    "required": ["memory_ids"]
                }),
            },
            Tool {
                name: "mnemosyne.hierarchy".to_string(),
                description: "Browse the hierarchical topic tree over memories. Returns directories with L0 abstracts and L1 overviews plus freshness metadata, for navigation without loading full content.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Optional namespace filter (e.g. 'project:myapp')"
                        },
                        "max_nodes": {
                            "type": "integer",
                            "description": "Maximum tree nodes to return (default 200)"
                        }
                    }
                }),
            },
            Tool {
                name: "mnemosyne.persona".to_string(),
                description: "Read durable preference and constraint memories for a personal-agent persona.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "description": "Persona namespace (default global)"},
                        "limit": {"type": "integer", "default": 50}
                    }
                }),
            },
            Tool {
                name: "mnemosyne.canonical".to_string(),
                description: "Store or recall one current canonical fact by category and name; prior values are updated in place and remain auditable in memory metadata.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["remember", "recall"]},
                        "category": {"type": "string"},
                        "name": {"type": "string"},
                        "body": {"type": "string"},
                        "namespace": {"type": "string", "default": "global"}
                    },
                    "required": ["action", "category", "name"]
                }),
            },
            Tool {
                name: "mnemosyne.triples".to_string(),
                description: "Add or query subject-predicate-object knowledge triples stored as searchable, tagged memories.".to_string(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "query"]},
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "namespace": {"type": "string", "default": "global"}
                    },
                    "required": ["action", "subject", "predicate"]
                }),
            },
        ];

        // Hermes exposes provider tools as underscore-separated names, while
        // older MCP clients use the dotted names above. Publish both names
        // with identical schemas so clients can migrate without a config or
        // prompt rewrite. Keeping this as a generated alias list prevents the
        // two surfaces from drifting apart.
        let aliases = [
            ("mnemosyne.recall", "mnemosyne_recall"),
            ("mnemosyne.list", "mnemosyne_list"),
            ("mnemosyne.used", "mnemosyne_used"),
            ("mnemosyne.hierarchy", "mnemosyne_hierarchy"),
            ("mnemosyne.graph", "mnemosyne_graph"),
            ("mnemosyne.context", "mnemosyne_context"),
            ("mnemosyne.remember", "mnemosyne_remember"),
            ("mnemosyne.consolidate", "mnemosyne_consolidate"),
            ("mnemosyne.update", "mnemosyne_update"),
            ("mnemosyne.delete", "mnemosyne_forget"),
            ("mnemosyne.persona", "mnemosyne_persona"),
            ("mnemosyne.canonical", "mnemosyne_canonical"),
            ("mnemosyne.triples", "mnemosyne_triples"),
        ];
        let aliased_tools: Vec<Tool> = aliases
            .into_iter()
            .filter_map(|(canonical, alias)| {
                tools
                    .iter()
                    .find(|tool| tool.name == canonical)
                    .cloned()
                    .map(|mut tool| {
                        tool.name = alias.to_string();
                        tool.description = format!(
                            "Hermes-compatible alias for {}. {}",
                            canonical, tool.description
                        );
                        tool
                    })
            })
            .collect();
        tools.extend(aliased_tools);
        tools
    }

    /// Execute a tool call
    pub async fn execute(&self, tool_name: &str, params: Value) -> Result<Value> {
        let canonical_name = match tool_name {
            "mnemosyne_recall" => "mnemosyne.recall",
            "mnemosyne_list" => "mnemosyne.list",
            "mnemosyne_used" => "mnemosyne.used",
            "mnemosyne_hierarchy" => "mnemosyne.hierarchy",
            "mnemosyne_graph" => "mnemosyne.graph",
            "mnemosyne_context" => "mnemosyne.context",
            "mnemosyne_remember" => "mnemosyne.remember",
            "mnemosyne_consolidate" => "mnemosyne.consolidate",
            "mnemosyne_update" => "mnemosyne.update",
            "mnemosyne_forget" => "mnemosyne.delete",
            "mnemosyne_persona" => "mnemosyne.persona",
            "mnemosyne_canonical" => "mnemosyne.canonical",
            "mnemosyne_triples" => "mnemosyne.triples",
            other => other,
        };
        info!("🔧 MCP tool called: {} (external process)", tool_name);
        debug!("MCP tool params: {:?}", params);

        let result = match canonical_name {
            "mnemosyne.recall" => self.recall(params).await,
            "mnemosyne.list" => self.list(params).await,
            "mnemosyne.used" => self.used(params).await,
            "mnemosyne.hierarchy" => self.hierarchy(params).await,
            "mnemosyne.graph" => self.graph(params).await,
            "mnemosyne.context" => self.context(params).await,
            "mnemosyne.remember" => self.remember(params).await,
            "mnemosyne.consolidate" => self.consolidate(params).await,
            "mnemosyne.update" => self.update(params).await,
            "mnemosyne.delete" => self.delete(params).await,
            "mnemosyne.persona" => self.persona(params).await,
            "mnemosyne.canonical" => self.canonical(params).await,
            "mnemosyne.triples" => self.triples(params).await,
            _ => {
                warn!(
                    "{} Unknown MCP tool: {}",
                    crate::icons::status::error(),
                    tool_name
                );
                Ok(serde_json::json!({
                    "error": format!("Unknown tool: {}", tool_name)
                }))
            }
        };

        match &result {
            Ok(_) => info!(
                "{} MCP tool {} completed successfully",
                crate::icons::status::success(),
                tool_name
            ),
            Err(e) => warn!(
                "{} MCP tool {} failed: {}",
                crate::icons::status::error(),
                tool_name,
                e
            ),
        }

        result
    }

    // === Validation Helpers ===

    /// Validate importance value (must be 1-10)
    /// Build a MemoryNote heuristically when no LLM is configured.
    ///
    /// Uses the first sentence as the summary and simple whitespace-derived
    /// keywords so that core remember/recall keeps working on local-only
    /// personal agents without any cloud API key.
    fn memory_without_enrichment(content: &str, context: &str) -> Result<MemoryNote> {
        use chrono::Utc;
        let now = Utc::now();
        let summary = content
            .split_once(". ")
            .map(|(first, _)| format!("{}.", first))
            .unwrap_or_else(|| content.chars().take(120).collect());
        let keywords: Vec<String> = content
            .split(|c: char| c.is_whitespace() || c == ',' || c == '.')
            .filter(|w| w.len() > 3)
            .take(8)
            .map(|w| w.to_lowercase())
            .collect();

        Ok(MemoryNote {
            id: MemoryId::new(),
            namespace: Namespace::Global, // overridden by caller
            created_at: now,
            updated_at: now,
            content: content.to_string(),
            summary,
            keywords,
            tags: vec!["un-enriched".to_string()],
            context: context.to_string(),
            memory_type: MemoryType::Insight,
            importance: 5,
            confidence: 0.6,
            links: Vec::new(),
            related_files: Vec::new(),
            related_entities: Vec::new(),
            access_count: 0,
            last_accessed_at: now,
            expires_at: None,
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: String::new(),
            memory_class: crate::types::MemoryClass::Knowledge,
            provenance: None,
        })
    }

    fn validate_importance(importance: u8) -> Result<()> {
        if !(1..=10).contains(&importance) {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "Importance must be between 1-10, got {}",
                importance
            )));
        }
        Ok(())
    }

    /// Validate max_results (must be 1-1000)
    fn validate_max_results(max_results: usize) -> Result<usize> {
        if max_results == 0 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "max_results must be at least 1".to_string(),
            ));
        }
        if max_results > 1000 {
            warn!("max_results capped at 1000 (requested: {})", max_results);
            return Ok(1000);
        }
        Ok(max_results)
    }

    fn validate_graph_hops(max_hops: usize) -> Result<usize> {
        if max_hops > MAX_GRAPH_HOPS {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "max_hops must not exceed {}",
                MAX_GRAPH_HOPS
            )));
        }
        Ok(max_hops)
    }

    fn validate_offset(offset: usize) -> Result<usize> {
        if offset > MAX_PAGE_OFFSET {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "offset must not exceed {}",
                MAX_PAGE_OFFSET
            )));
        }
        Ok(offset)
    }

    fn validate_abstention_threshold(threshold: f32) -> Result<f32> {
        if !threshold.is_finite() || !(0.0..=1.0).contains(&threshold) {
            return Err(crate::error::MnemosyneError::ValidationError(
                "abstention_threshold must be a finite number between 0 and 1".to_string(),
            ));
        }
        Ok(threshold)
    }

    /// Validate non-empty string
    fn validate_non_empty(value: &str, field_name: &str) -> Result<()> {
        if value.trim().is_empty() {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "{} cannot be empty",
                field_name
            )));
        }
        Ok(())
    }

    /// Validate content length (max 100KB)
    fn validate_content_length(content: &str) -> Result<()> {
        const MAX_CONTENT_LENGTH: usize = 100_000; // 100KB
        if content.len() > MAX_CONTENT_LENGTH {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "Content too large: {} bytes (max: {} bytes)",
                content.len(),
                MAX_CONTENT_LENGTH
            )));
        }
        Ok(())
    }

    // === OBSERVE Tools ===

    async fn recall(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct RecallParams {
            query: String,
            namespace: Option<String>,
            max_results: Option<usize>,
            min_importance: Option<u8>,
            expand_graph: Option<bool>,
            hierarchical: Option<bool>,
            budget_tokens: Option<usize>,
            abstention_threshold: Option<f32>,
        }

        let params: RecallParams = serde_json::from_value(params)?;

        // Validate query
        Self::validate_non_empty(&params.query, "query")?;

        // Validate max_results
        let max_results = Self::validate_max_results(params.max_results.unwrap_or(10))?;

        // Validate min_importance if provided
        if let Some(min_imp) = params.min_importance {
            Self::validate_importance(min_imp)?;
        }

        if let Some(threshold) = params.abstention_threshold {
            Self::validate_abstention_threshold(threshold)?;
        }

        // Parse namespace, defaulting to the scope configured for this MCP
        // process rather than broadening recall to every namespace.
        let namespace = Some(self.namespace_or_default(params.namespace.as_deref())?);

        // Graph expansion is safe for direct matches because the storage
        // layer excludes seed memories and only returns depth>0 neighbors.
        let expand_graph = params.expand_graph.unwrap_or(true);

        // Phase 1: Keyword + graph search. If the storage backend refuses to
        // serve unranked results because its vector signal failed, fall back
        // to keyword search while making the degradation explicit below.
        let mut degraded = false;
        let mut degraded_reasons = Vec::new();
        let keyword_results = match self
            .storage
            .hybrid_search_by_class(
                &params.query,
                namespace.clone(),
                max_results * 2,
                expand_graph,
                crate::types::MemoryClass::Knowledge,
            )
            .await
        {
            Ok(results) => results,
            Err(error) if error.to_string().contains("fail-closed retrieval") => {
                warn!(
                    "Storage hybrid search degraded ({}); serving keyword results",
                    error
                );
                degraded = true;
                degraded_reasons.push("hybrid_search_vector_unavailable");
                self.storage
                    .keyword_search(&params.query, namespace.clone())
                    .await
                    .map(|results| {
                        results
                            .into_iter()
                            .filter(|result| {
                                result.memory.memory_class == crate::types::MemoryClass::Knowledge
                            })
                            .collect::<Vec<_>>()
                    })?
            }
            Err(error) => return Err(error),
        };

        // Keyless release builds use deterministic hash embeddings. They still
        // provide a vector-shaped signal, but semantic quality can collapse as
        // the store grows, so surface that degradation to both logs and clients.
        let fallback_embeddings = self.embeddings.uses_fallback_embeddings();
        let active_memory_count = if fallback_embeddings {
            match self.storage.count_memories(namespace.clone()).await {
                Ok(count) => count,
                Err(error) => {
                    warn!("Unable to count memories for embedding warning: {}", error);
                    0
                }
            }
        } else {
            0
        };
        let fallback_warning = if fallback_embeddings {
            crate::embeddings::fallback_embedding_warning(active_memory_count)
        } else {
            None
        };
        if let Some(warning) = &fallback_warning {
            warn!("{}", warning);
        }

        // Phase 2: Vector similarity search (degrade loudly if unavailable)
        if fallback_embeddings {
            degraded = true;
            degraded_reasons.push("fallback_embeddings");
        }
        let vector_results = match self.embeddings.generate_embedding(&params.query).await {
            Ok(query_embedding) => self
                .storage
                // Wide candidate pool: fusion sees deep vector matches
                // instead of only max_results*2 nearest rows.
                .vector_search(&query_embedding, max_results * 4, namespace.clone())
                .await
                .map(|results| {
                    results
                        .into_iter()
                        .filter(|result| {
                            result.memory.memory_class == crate::types::MemoryClass::Knowledge
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_else(|e| {
                    warn!(
                        "Vector search unavailable ({}); serving keyword+graph results",
                        e
                    );
                    degraded = true;
                    degraded_reasons.push("vector_search_unavailable");
                    Vec::new()
                }),
            Err(e) => {
                warn!(
                    "Query embedding failed ({}); serving keyword+graph results",
                    e
                );
                degraded = true;
                degraded_reasons.push("query_embedding_unavailable");
                Vec::new()
            }
        };

        // Guidance is recalled independently and is never mixed into the
        // factual result ranking or abstention decision.
        let policy_results = self
            .storage
            .interaction_policy_search(&params.query, 3)
            .await?;

        // Phase 3: Merge and re-rank factual results
        let mut memory_scores = std::collections::HashMap::new();

        // Add keyword results with 40% weight
        for result in keyword_results {
            memory_scores
                .entry(result.memory.id)
                .or_insert((result.memory.clone(), vec![]))
                .1
                .push(("keyword", result.score * 0.4));
        }

        // Add vector results with 30% weight
        for result in vector_results {
            memory_scores
                .entry(result.memory.id)
                .or_insert((result.memory.clone(), vec![]))
                .1
                .push(("vector", result.score * 0.3));
        }

        // Compute final scores
        let mut results: Vec<_> = memory_scores
            .into_iter()
            .map(|(_id, (memory, score_components))| {
                let total_score: f32 = score_components.iter().map(|(_, s)| s).sum();
                let match_reason = score_components
                    .iter()
                    .map(|(method, score)| format!("{}: {:.2}", method, score))
                    .collect::<Vec<_>>()
                    .join(", ");

                crate::types::SearchResult {
                    memory,
                    score: total_score,
                    match_reason: format!("hybrid ({})", match_reason),
                }
            })
            .collect();

        // Sort by score descending
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Coverage rescoring before truncation: promote candidates covering
        // most of the query's content terms over single-token OR matches.
        crate::utils::retrieval::apply_coverage_rescore(&params.query, &mut results);
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Limit results
        results.truncate(max_results);

        // Filter by minimum importance if specified
        if let Some(min_importance) = params.min_importance {
            results.retain(|r| r.memory.importance >= min_importance);
        }

        let best_score = results.first().map(|result| result.score).unwrap_or(0.0);
        let abstention_threshold = params.abstention_threshold;
        let abstained = abstention_threshold
            .map(|threshold| best_score < threshold)
            .unwrap_or(false);
        if abstained {
            results.clear();
        }

        // Optional hierarchical reranking through the topic tree
        let mut trajectory_json: Option<serde_json::Value> = None;
        if params.hierarchical.unwrap_or(false) {
            let note_refs: Vec<&crate::types::MemoryNote> =
                results.iter().map(|r| &r.memory).collect();
            let raw_scores: Vec<f32> = results.iter().map(|r| r.score).collect();
            let (ranked, trajectory) = crate::hierarchy::rerank_results(
                &note_refs,
                &raw_scores,
                crate::hierarchy::RetrieverConfig::default(),
                true,
            );
            trajectory_json = serde_json::from_str(&trajectory.to_json()).ok();
            results = ranked
                .into_iter()
                .filter_map(|(i, s)| {
                    results.get(i).cloned().map(|mut r| {
                        r.score = s;
                        r.match_reason = format!("{} [hierarchical]", r.match_reason);
                        r
                    })
                })
                .collect();
        }

        let token_ledger = params.budget_tokens.map(|budget| {
            let mut candidates = results
                .iter()
                .map(|result| {
                    crate::context_assembler::Candidate::new(
                        result.memory.id.to_string(),
                        result.memory.summary.clone(),
                        result.memory.summary.clone(),
                        result.memory.summary.clone(),
                        result.memory.content.clone(),
                        result.score,
                    )
                })
                .collect::<Vec<_>>();
            candidates.extend(policy_results.iter().map(|result| {
                crate::context_assembler::Candidate::new(
                    format!("policy-{}", result.memory.id),
                    "Response guidance",
                    result.memory.content.clone(),
                    result.memory.content.clone(),
                    result.memory.content.clone(),
                    result.score,
                )
            }));
            let plan = crate::context_assembler::assemble(&candidates, budget);
            serde_json::to_value(plan.ledger)
                .unwrap_or_else(|_| serde_json::json!({"budget_tokens": budget}))
        });

        // Increment access counts for both independently returned channels.
        for result in results.iter().chain(policy_results.iter()) {
            if let Err(e) = self.storage.increment_access(result.memory.id).await {
                warn!("Failed to increment access count: {}", e);
            }
        }

        // Emit event through event sink
        let event = crate::api::Event::memory_recalled(params.query.clone(), results.len());
        if let Err(e) = self.event_sink.emit(event).await {
            warn!("Failed to emit memory recalled event: {}", e);
        }

        info!(
            "{} MCP recall: found {} memories for query '{}' (namespace: {:?})",
            crate::icons::action::search(),
            results.len(),
            params.query,
            params.namespace
        );

        Ok(serde_json::json!({
            "results": results,
            "response_guidance": policy_results,
            "channels": {
                "factual": {
                    "quota": max_results,
                    "count": results.len(),
                    "abstained": abstained,
                    "abstention_reason": if abstained { Some("best factual result score was below abstention_threshold") } else { None::<&str> }
                },
                "response_guidance": {
                    "quota": 3,
                    "count": policy_results.len(),
                    "abstained": policy_results.is_empty(),
                    "abstention_reason": if policy_results.is_empty() { Some("no eligible anchored policy matched") } else { None::<&str> }
                }
            },
            "token_ledger": token_ledger,
            "query": params.query,
            "count": results.len(),
            "method": if params.hierarchical.unwrap_or(false) {
                "hierarchical_hybrid_search"
            } else {
                "hybrid_search (keyword 40% + vector 30% + graph)"
            },
            "trajectory": trajectory_json,
            // Loud degradation flag: true means the ranking signal is
            // unavailable or relies on deterministic fallback embeddings.
            "degraded": degraded,
            "degraded_reasons": degraded_reasons,
            "embedding_mode": self.embeddings.embedding_mode(),
            "fallback_warning": fallback_warning,
            "best_score": best_score,
            "abstained": abstained,
            "abstention_enabled": abstention_threshold.is_some(),
            // Preserve the documented recommendation for callers that do not
            // opt into abstention, while returning the requested threshold
            // when one was supplied.
            "abstention_threshold": abstention_threshold.unwrap_or(RECOMMENDED_ABSTENTION_THRESHOLD),
            "abstention_reason": if abstained {
                Some("best result score was below abstention_threshold")
            } else {
                None::<&str>
            }
        }))
    }

    async fn used(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct UsedParams {
            memory_ids: Vec<String>,
        }

        let params: UsedParams = serde_json::from_value(params)?;

        let mut confirmed = Vec::new();
        let mut failed = Vec::new();
        for id_str in &params.memory_ids {
            match MemoryId::from_string(id_str) {
                Ok(id) => match self.storage.increment_access(id).await {
                    Ok(()) => confirmed.push(id_str.clone()),
                    Err(e) => {
                        failed.push(serde_json::json!({"id": id_str, "error": e.to_string()}))
                    }
                },
                Err(e) => failed.push(serde_json::json!({"id": id_str, "error": e.to_string()})),
            }
        }

        // Emit usage feedback event for the online relevance learner
        let event =
            crate::api::Event::memory_recalled("used-feedback".to_string(), confirmed.len());
        if let Err(e) = self.event_sink.emit(event).await {
            warn!("Failed to emit used feedback event: {}", e);
        }

        info!(
            "{} MCP used: {} memories marked as helpful",
            crate::icons::status::success(),
            confirmed.len()
        );

        Ok(serde_json::json!({
            "confirmed": confirmed,
            "failed": failed,
            "count": confirmed.len()
        }))
    }

    async fn hierarchy(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct HierarchyParams {
            namespace: Option<String>,
            max_nodes: Option<usize>,
        }

        let params: HierarchyParams = serde_json::from_value(params)?;

        let namespace = Some(self.namespace_or_default(params.namespace.as_deref())?);
        let max_nodes = Self::validate_max_results(params.max_nodes.unwrap_or(200))?;

        let notes = self
            .storage
            .list_memories(
                namespace,
                max_nodes.saturating_mul(4),
                crate::storage::MemorySortOrder::Recent,
            )
            .await?;
        let refs: Vec<&crate::types::MemoryNote> = notes.iter().collect();
        let tree = crate::hierarchy::build_tree(&refs);
        let node_count = tree.len();
        let truncated = node_count > max_nodes;

        info!(
            "{} MCP hierarchy: {} nodes over {} memories",
            crate::icons::action::search(),
            node_count,
            notes.len()
        );

        Ok(serde_json::json!({
            "nodes": tree.into_iter().take(max_nodes).collect::<Vec<_>>(),
            "memory_count": notes.len(),
            "count": node_count.min(max_nodes),
            "max_nodes": max_nodes,
            "truncated": truncated
        }))
    }

    async fn persona(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct PersonaParams {
            namespace: Option<String>,
            limit: Option<usize>,
        }

        let params: PersonaParams = serde_json::from_value(params)?;
        let namespace = Some(self.namespace_or_default(params.namespace.as_deref())?);
        let limit = params.limit.unwrap_or(50).clamp(1, 1000);
        let memories = self
            .storage
            .list_memories(
                namespace,
                limit * 4,
                crate::storage::MemorySortOrder::Importance,
            )
            .await?
            .into_iter()
            .filter(|memory| {
                matches!(
                    memory.memory_type,
                    MemoryType::Preference | MemoryType::Constraint
                ) || memory
                    .tags
                    .iter()
                    .any(|tag| tag == "persona" || tag == "canonical")
            })
            .take(limit)
            .collect::<Vec<_>>();

        Ok(serde_json::json!({
            "memories": memories,
            "count": memories.len(),
            "surface": "persona"
        }))
    }

    async fn canonical(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct CanonicalParams {
            action: String,
            category: String,
            name: String,
            body: Option<String>,
            namespace: Option<String>,
        }

        let params: CanonicalParams = serde_json::from_value(params)?;
        Self::validate_non_empty(&params.category, "category")?;
        Self::validate_non_empty(&params.name, "name")?;
        let namespace = self.namespace_or_default(params.namespace.as_deref())?;
        let category_tag = format!("canonical-category:{}", params.category);
        let name_tag = format!("canonical-name:{}", params.name);
        let mut existing = self
            .storage
            .list_memories(
                Some(namespace.clone()),
                1000,
                crate::storage::MemorySortOrder::Recent,
            )
            .await?
            .into_iter()
            .find(|memory| {
                memory.tags.iter().any(|tag| tag == &category_tag)
                    && memory.tags.iter().any(|tag| tag == &name_tag)
                    && memory.tags.iter().any(|tag| tag == "canonical")
            });

        match params.action.as_str() {
            "recall" => Ok(serde_json::json!({
                "found": existing.is_some(),
                "memory": existing,
                "category": params.category,
                "name": params.name
            })),
            "remember" => {
                let body = params.body.ok_or_else(|| {
                    MnemosyneError::ValidationError(
                        "body is required when action is remember".to_string(),
                    )
                })?;
                Self::validate_non_empty(&body, "body")?;
                Self::validate_content_length(&body)?;
                let tags = vec![
                    "canonical".to_string(),
                    "persona".to_string(),
                    category_tag,
                    name_tag,
                ];

                let memory_id = if let Some(memory) = existing.as_mut() {
                    memory.content = body.clone();
                    memory.summary = body.chars().take(160).collect();
                    memory.tags = tags;
                    memory.memory_type = MemoryType::Preference;
                    memory.context = format!("canonical:{}:{}", params.category, params.name);
                    memory.updated_at = chrono::Utc::now();
                    let id = memory.id;
                    self.storage.update_memory(memory).await?;
                    id
                } else {
                    let mut memory = Self::memory_without_enrichment(
                        &body,
                        &format!("canonical:{}:{}", params.category, params.name),
                    )?;
                    memory.namespace = namespace;
                    memory.memory_type = MemoryType::Preference;
                    memory.importance = 10;
                    memory.tags = tags;
                    let id = memory.id;
                    self.storage.store_memory(&memory).await?;
                    id
                };

                Ok(serde_json::json!({
                    "stored": true,
                    "memory_id": memory_id.to_string(),
                    "category": params.category,
                    "name": params.name
                }))
            }
            action => Err(MnemosyneError::ValidationError(format!(
                "Unsupported canonical action '{}'; use remember or recall",
                action
            ))),
        }
    }

    async fn triples(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct TripleParams {
            action: String,
            subject: String,
            predicate: String,
            object: Option<String>,
            namespace: Option<String>,
        }

        let params: TripleParams = serde_json::from_value(params)?;
        Self::validate_non_empty(&params.subject, "subject")?;
        Self::validate_non_empty(&params.predicate, "predicate")?;
        let namespace = self.namespace_or_default(params.namespace.as_deref())?;
        let subject_tag = format!("triple-subject:{}", params.subject);
        let predicate_tag = format!("triple-predicate:{}", params.predicate);
        let memories = self
            .storage
            .list_memories(
                Some(namespace.clone()),
                1000,
                crate::storage::MemorySortOrder::Recent,
            )
            .await?;

        match params.action.as_str() {
            "query" => {
                let object_tag = params
                    .object
                    .as_deref()
                    .map(|value| format!("triple-object:{}", value));
                let matches = memories
                    .into_iter()
                    .filter(|memory| {
                        memory.tags.iter().any(|tag| tag == "triple")
                            && memory.tags.iter().any(|tag| tag == &subject_tag)
                            && memory.tags.iter().any(|tag| tag == &predicate_tag)
                            && object_tag.as_ref().map_or(true, |tag| {
                                memory.tags.iter().any(|memory_tag| memory_tag == tag)
                            })
                    })
                    .collect::<Vec<_>>();
                Ok(serde_json::json!({
                    "triples": matches,
                    "count": matches.len()
                }))
            }
            "add" => {
                let object = params.object.ok_or_else(|| {
                    MnemosyneError::ValidationError(
                        "object is required when action is add".to_string(),
                    )
                })?;
                Self::validate_non_empty(&object, "object")?;
                let object_tag = format!("triple-object:{}", object);
                let mut memory = Self::memory_without_enrichment(
                    &format!("{} {} {}", params.subject, params.predicate, object),
                    "knowledge triple",
                )?;
                memory.namespace = namespace.clone();
                memory.memory_type = MemoryType::Entity;
                memory.importance = 8;
                memory.tags = vec![
                    "triple".to_string(),
                    subject_tag.clone(),
                    predicate_tag.clone(),
                    object_tag,
                ];

                // A subject/predicate slot has one current object. Preserve
                // the prior value as an archived superseded memory.
                if let Some(mut prior) = memories.into_iter().find(|candidate| {
                    candidate.tags.iter().any(|tag| tag == "triple")
                        && candidate.tags.iter().any(|tag| tag == &subject_tag)
                        && candidate.tags.iter().any(|tag| tag == &predicate_tag)
                        && !candidate.is_archived
                }) {
                    prior.superseded_by = Some(memory.id);
                    prior.is_archived = true;
                    prior.updated_at = chrono::Utc::now();
                    self.storage.update_memory(&prior).await?;
                }
                let id = memory.id;
                self.storage.store_memory(&memory).await?;
                Ok(serde_json::json!({
                    "stored": true,
                    "memory_id": id.to_string(),
                    "subject": params.subject,
                    "predicate": params.predicate,
                    "object": object
                }))
            }
            action => Err(MnemosyneError::ValidationError(format!(
                "Unsupported triples action '{}'; use add or query",
                action
            ))),
        }
    }

    async fn list(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct ListParams {
            namespace: Option<String>,
            limit: Option<usize>,
            offset: Option<usize>,
            sort_by: Option<String>,
        }

        let params: ListParams = serde_json::from_value(params)?;

        // Parse namespace using the process default when omitted.
        let namespace = Some(self.namespace_or_default(params.namespace.as_deref())?);

        // Parse sort order
        use crate::storage::MemorySortOrder;
        let sort_by = match params.sort_by.as_deref() {
            Some("importance") => MemorySortOrder::Importance,
            Some("access_count") => MemorySortOrder::AccessCount,
            _ => MemorySortOrder::Recent, // Default
        };

        let limit = Self::validate_max_results(params.limit.unwrap_or(20))?;
        let offset = Self::validate_offset(params.offset.unwrap_or(0))?;
        let window_end = offset.checked_add(limit).ok_or_else(|| {
            crate::error::MnemosyneError::ValidationError(
                "pagination window is too large".to_string(),
            )
        })?;

        // Fetch one sentinel row so callers can reliably determine whether a
        // subsequent page exists without requiring a separate COUNT query.
        let memories = self
            .storage
            .list_memories(namespace, window_end.saturating_add(1), sort_by)
            .await?;
        let (memories, page) = paginate(memories, offset, limit);

        Ok(serde_json::json!({
            "memories": memories,
            "count": page.count,
            "offset": page.offset,
            "limit": page.limit,
            "has_more": page.has_more,
            "next_offset": if page.has_more { Some(page.offset + page.count) } else { None },
            "sort_by": match sort_by {
                MemorySortOrder::Recent => "recent",
                MemorySortOrder::Importance => "importance",
                MemorySortOrder::AccessCount => "access_count",
            }
        }))
    }

    // === ORIENT Tools ===

    async fn graph(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct GraphParams {
            seed_ids: Vec<String>,
            max_hops: Option<usize>,
            max_results: Option<usize>,
        }

        let params: GraphParams = serde_json::from_value(params)?;
        if params.seed_ids.len() > MAX_GRAPH_SEEDS {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "seed_ids must not contain more than {} IDs",
                MAX_GRAPH_SEEDS
            )));
        }

        // Parse seed IDs
        let seed_ids: Result<Vec<MemoryId>> = params
            .seed_ids
            .iter()
            .map(|s| {
                MemoryId::from_string(s)
                    .map_err(|e| crate::error::MnemosyneError::InvalidId(e.to_string()))
            })
            .collect();

        let seed_ids = seed_ids?;
        let max_hops = Self::validate_graph_hops(params.max_hops.unwrap_or(2))?;
        let max_results =
            Self::validate_max_results(params.max_results.unwrap_or(DEFAULT_GRAPH_MAX_RESULTS))?;

        // Call storage graph traversal
        // Note: MCP graph tool doesn't filter by namespace for exploratory traversal
        let memories = self
            .storage
            .graph_traverse_bounded(&seed_ids, max_hops, None, max_results)
            .await?;
        let (memories, page) = paginate(memories, 0, max_results);

        Ok(serde_json::json!({
            "memories": memories,
            "count": page.count,
            "max_results": page.limit,
            "truncated": page.has_more
        }))
    }

    async fn context(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct ContextParams {
            memory_ids: Vec<String>,
            include_links: Option<bool>,
            max_results: Option<usize>,
        }

        let params: ContextParams = serde_json::from_value(params)?;

        // Validate memory_ids is not empty and prevent an oversized fan-out
        // before fetching the requested memories or expanding their links.
        if params.memory_ids.is_empty() {
            return Err(crate::error::MnemosyneError::ValidationError(
                "memory_ids cannot be empty".to_string(),
            ));
        }
        if params.memory_ids.len() > MAX_CONTEXT_INPUT_IDS {
            return Err(crate::error::MnemosyneError::ValidationError(format!(
                "memory_ids must not contain more than {} IDs",
                MAX_CONTEXT_INPUT_IDS
            )));
        }

        // Parse memory IDs
        let memory_ids: Result<Vec<MemoryId>> = params
            .memory_ids
            .iter()
            .map(|s| {
                MemoryId::from_string(s)
                    .map_err(|e| crate::error::MnemosyneError::InvalidId(e.to_string()))
            })
            .collect();

        let memory_ids = memory_ids?;
        let include_links = params.include_links.unwrap_or(true);
        let max_results =
            Self::validate_max_results(params.max_results.unwrap_or(DEFAULT_CONTEXT_MAX_RESULTS))?;

        // Fetch memories
        let mut memories = Vec::new();
        for id in memory_ids {
            match self.storage.get_memory(id).await {
                Ok(memory) => memories.push(memory),
                Err(e) => warn!("Failed to get memory {}: {}", id, e),
            }
        }

        // Optionally fetch linked memories via graph traversal
        if include_links && !memories.is_empty() {
            // Use graph traversal to get linked memories (1-hop)
            let seed_ids: Vec<MemoryId> = memories.iter().map(|m| m.id).collect();
            match self
                .storage
                .graph_traverse_bounded(&seed_ids, 1, None, max_results)
                .await
            {
                Ok(linked) => {
                    // Add linked memories that aren't already in the result set
                    for linked_memory in linked {
                        if !memories.iter().any(|m| m.id == linked_memory.id) {
                            memories.push(linked_memory);
                        }
                    }
                    debug!("Context expanded to {} memories with links", memories.len());
                }
                Err(e) => warn!("Failed to fetch linked memories: {}", e),
            }
        }

        let (memories, page) = paginate(memories, 0, max_results);

        Ok(serde_json::json!({
            "memories": memories,
            "count": page.count,
            "max_results": page.limit,
            "truncated": page.has_more
        }))
    }

    // === DECIDE Tools ===

    async fn remember(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct RememberParams {
            content: String,
            /// Optional — a small local model WILL omit this. Defaults to the
            /// MCP process namespace so omitted calls remain scope-safe.
            namespace: Option<String>,
            importance: Option<u8>,
            context: Option<String>,
        }

        let params: RememberParams = serde_json::from_value(params)?;

        // Validate content
        Self::validate_non_empty(&params.content, "content")?;
        Self::validate_content_length(&params.content)?;

        // Validate importance if provided
        if let Some(importance) = params.importance {
            Self::validate_importance(importance)?;
        }

        // Parse namespace using the process default when omitted.
        let namespace = self.namespace_or_default(params.namespace.as_deref())?;

        // Enrich with LLM when available; degrade gracefully otherwise
        // (local-first personal agents often run without any cloud LLM key)
        let context = params
            .context
            .unwrap_or_else(|| "User-provided memory".to_string());
        let mut memory = match self.llm.enrich_memory(&params.content, &context).await {
            Ok(m) => m,
            Err(e) => {
                warn!(
                    "LLM enrichment unavailable ({}); storing memory without enrichment",
                    e
                );
                Self::memory_without_enrichment(&params.content, &context)?
            }
        };

        // Override with user-provided values
        memory.namespace = namespace;
        if let Some(importance) = params.importance {
            memory.importance = importance; // Already validated above
        }

        // Auto-generate embedding for vector search
        debug!("Generating embedding for memory: {}", memory.id);
        let embedding = self.embeddings.generate_embedding(&memory.content).await?;
        memory.embedding = Some(embedding);

        // Store memory (with embedding)
        self.storage.store_memory(&memory).await?;

        // Emit event through event sink
        let event = crate::api::Event::memory_stored(memory.id.to_string(), memory.summary.clone());
        if let Err(e) = self.event_sink.emit(event).await {
            warn!("Failed to emit memory stored event: {}", e);
        }

        info!(
            "{} MCP remember: stored memory '{}' (importance: {}, tags: {:?})",
            crate::icons::action::save(),
            memory.summary.chars().take(60).collect::<String>(),
            memory.importance,
            memory.tags
        );

        Ok(serde_json::json!({
            "memory_id": memory.id.to_string(),
            "summary": memory.summary,
            "importance": memory.importance,
            "tags": memory.tags
        }))
    }

    async fn consolidate(&self, params: Value) -> Result<Value> {
        use crate::types::ConsolidationDecision;

        #[derive(Deserialize)]
        struct ConsolidateParams {
            memory_ids: Option<Vec<String>>,
            namespace: Option<String>,
            auto_apply: Option<bool>,
        }

        let params: ConsolidateParams = serde_json::from_value(params)?;

        let auto_apply = params.auto_apply.unwrap_or(false);

        // If specific memory IDs provided, analyze those
        if let Some(id_strs) = params.memory_ids {
            if id_strs.len() != 2 {
                return Ok(serde_json::json!({
                    "error": "Exactly 2 memory IDs required for pairwise consolidation"
                }));
            }

            let id_a = MemoryId::from_string(&id_strs[0])
                .map_err(|e| crate::error::MnemosyneError::InvalidId(e.to_string()))?;
            let id_b = MemoryId::from_string(&id_strs[1])
                .map_err(|e| crate::error::MnemosyneError::InvalidId(e.to_string()))?;

            let memory_a = self.storage.get_memory(id_a).await?;
            let memory_b = self.storage.get_memory(id_b).await?;

            // Get LLM decision
            let decision = self.llm.should_consolidate(&memory_a, &memory_b).await?;

            // Apply if auto_apply is true
            if auto_apply {
                match decision {
                    ConsolidationDecision::Merge { into, content } => {
                        let mut memory = if into == id_a { memory_a } else { memory_b };
                        memory.content = content;
                        memory.embedding = None;
                        memory.updated_at = chrono::Utc::now();
                        self.storage.update_memory(&memory).await?;

                        // Archive the other one
                        let archived = if into == id_a { id_b } else { id_a };
                        self.storage.archive_memory(archived).await?;

                        return Ok(serde_json::json!({
                            "action": "merged",
                            "kept": into.to_string(),
                            "archived": archived.to_string()
                        }));
                    }
                    ConsolidationDecision::Supersede { kept, superseded } => {
                        // Update the superseded memory's metadata
                        let mut memory = self.storage.get_memory(superseded).await?;
                        memory.superseded_by = Some(kept);
                        memory.is_archived = true;
                        self.storage.update_memory(&memory).await?;

                        return Ok(serde_json::json!({
                            "action": "superseded",
                            "kept": kept.to_string(),
                            "superseded": superseded.to_string()
                        }));
                    }
                    ConsolidationDecision::KeepBoth => {
                        return Ok(serde_json::json!({
                            "action": "keep_both",
                            "reason": "Memories are distinct enough to maintain separately"
                        }));
                    }
                }
            } else {
                // Return recommendation without applying
                return Ok(serde_json::json!({
                    "recommendation": match decision {
                        ConsolidationDecision::Merge { .. } => "merge",
                        ConsolidationDecision::Supersede { .. } => "supersede",
                        ConsolidationDecision::KeepBoth => "keep_both",
                    },
                    "auto_applied": false,
                    "hint": "Set auto_apply: true to apply this decision"
                }));
            }
        }

        // Otherwise, find candidates in namespace
        let namespace = Some(self.namespace_or_default(params.namespace.as_deref())?);

        let candidates = self
            .storage
            .find_consolidation_candidates(namespace)
            .await?;

        Ok(serde_json::json!({
            "candidates": candidates.len(),
            "message": "Candidate finding not yet fully implemented (needs similarity scoring)"
        }))
    }

    // === ACT Tools ===

    async fn update(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct UpdateParams {
            memory_id: String,
            content: Option<String>,
            importance: Option<u8>,
            tags: Option<Vec<String>>,
            add_tags: Option<Vec<String>>,
        }

        let params: UpdateParams = serde_json::from_value(params)?;

        // Validate content if provided
        if let Some(ref content) = params.content {
            Self::validate_non_empty(content, "content")?;
            Self::validate_content_length(content)?;
        }

        // Validate importance if provided
        if let Some(importance) = params.importance {
            Self::validate_importance(importance)?;
        }

        // Parse memory ID
        let memory_id = MemoryId::from_string(&params.memory_id)
            .map_err(|e| crate::error::MnemosyneError::InvalidId(e.to_string()))?;

        // Get existing memory
        let mut memory = self.storage.get_memory(memory_id).await?;

        // Apply updates
        if let Some(content) = params.content {
            memory.content = content.clone();

            // Re-generate embedding when content changes
            match self.embeddings.generate_embedding(&content).await {
                Ok(new_embedding) => {
                    memory.embedding = Some(new_embedding);
                    info!("Regenerated embedding for updated memory");
                }
                Err(e) => {
                    warn!("Failed to regenerate embedding: {}. Update will proceed without embedding.", e);
                    // Explicitly clear the prior vector so a failed
                    // regeneration cannot leave stale semantic results live.
                    memory.embedding = None;
                    // Continue with update even if embedding fails
                }
            }
        }

        if let Some(importance) = params.importance {
            memory.importance = importance; // Already validated above
        }

        if let Some(tags) = params.tags {
            memory.tags = tags;
        } else if let Some(add_tags) = params.add_tags {
            for tag in add_tags {
                if !memory.tags.contains(&tag) {
                    memory.tags.push(tag);
                }
            }
        }

        memory.updated_at = chrono::Utc::now();

        // Update storage
        self.storage.update_memory(&memory).await?;

        Ok(serde_json::json!({
            "memory_id": memory.id.to_string(),
            "updated": true
        }))
    }

    async fn delete(&self, params: Value) -> Result<Value> {
        #[derive(Deserialize)]
        struct DeleteParams {
            memory_id: String,
        }

        let params: DeleteParams = serde_json::from_value(params)?;

        // Parse memory ID
        let memory_id = MemoryId::from_string(&params.memory_id)
            .map_err(|e| crate::error::MnemosyneError::InvalidId(e.to_string()))?;

        // Archive (soft delete)
        self.storage.archive_memory(memory_id).await?;

        Ok(serde_json::json!({
            "memory_id": memory_id.to_string(),
            "archived": true
        }))
    }

    // === Helper Methods ===

    fn parse_namespace(&self, namespace_str: &str) -> Result<Namespace> {
        Namespace::parse(namespace_str)
    }

    fn namespace_or_default(&self, namespace: Option<&str>) -> Result<Namespace> {
        namespace
            .map(|value| self.parse_namespace(value))
            .transpose()
            .map(|parsed| parsed.unwrap_or_else(|| self.default_namespace.clone()))
    }
}

#[cfg(test)]
mod tests {
    use super::paginate;

    #[test]
    fn paginate_reports_following_page() {
        let (page, info) = paginate(vec![1, 2, 3, 4, 5], 2, 2);

        assert_eq!(page, vec![3, 4]);
        assert_eq!(info.offset, 2);
        assert_eq!(info.limit, 2);
        assert_eq!(info.count, 2);
        assert!(info.has_more);
    }

    #[test]
    fn paginate_handles_offset_past_end() {
        let (page, info) = paginate(vec![1, 2], 10, 2);

        assert!(page.is_empty());
        assert_eq!(info.count, 0);
        assert!(!info.has_more);
    }
}
