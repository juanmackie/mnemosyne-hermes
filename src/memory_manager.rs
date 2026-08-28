//! Simple, high-level memory API for personal AI agents.
//!
//! ## Design goals (inspired by Mem0, Letta, Zep, SuperLocalMemory)
//! - Agent namespace isolation – each agent owns its own private memory scope
//! - Flat text I/O – plain strings in, plain strings out
//! - Proactive lifecycle – `forget()` manages recall quality
//! - Graph-aware recall – related memories surfaced via `MemoryLink`
//! - Offline-capable – local embeddings used automatically
//! - Bare-minimum API – store / recall / list / forget / update
//!
//! ## Quick start
//!
//! ```no_run
//! use mnemosyne_core::MemoryManager;
//!
//! # fn main() -> mnemosyne_core::error::Result<()> {
//! # tokio::runtime::Runtime::new().unwrap().block_on(async {
//! // Open (or create) a per-agent database
//! let agent = MemoryManager::new("my-agent").await?;
//!
//! // Store a memory (auto-enriched by default)
//! let id = agent.store("User prefers dark mode in all apps.").await?;
//!
//! // Semantic recall – highest fidelity hits first
//! let hits = agent.recall("What UI theme does the user like?", 5).await?;
//! for r in &hits {
//!     println!("[{:.2}] {}", r.score, r.memory.summary);
//! }
//!
//! // Soft-delete a wrong or superseded memory
//! agent.forget(&id).await?;
//! # Ok(())
//! # })
//! # }
//! ```

use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::Mutex;

use crate::{
    error::{MnemosyneError, Result},
    storage::{libsql::ConnectionMode, libsql::PurgeReport, MemorySortOrder, StorageBackend},
    types::{MemoryClass, MemoryId, MemoryLink, MemoryNote, Namespace, SearchResult},
};

/// Re-export so consumers can write `MemoryConfig::tags(vec![MemoryType::Feature])`.
pub use crate::types::MemoryType;

/// Explicit decision from a recall attempt: either ranked evidence to
/// answer with, or a principled abstention.
///
/// Evidence principle: *sparse memory abstains free* — eager retrieval must
/// buy an explicit "I don't know" discipline instead of over-answering on
/// weak matches.
#[derive(Debug, Clone)]
pub enum RecallDecision {
    /// Confident-enough matches; `confidence` is the top normalized score.
    Answer {
        results: Vec<SearchResult>,
        confidence: f32,
    },
    /// No result met the threshold — the caller should say "I don't know".
    Abstain { reason: String, best_score: f32 },
}

/// Default abstention threshold for [`MemoryManager::recall_decided`].
/// Hybrid scores are weighted sums (max ≈ 1.0); below this the store is
/// effectively guessing.
pub const DEFAULT_ABSTENTION_THRESHOLD: f32 = 0.30;

/// Per-operation configuration for [`MemoryManager`].
#[derive(Debug, Clone, Default)]
pub struct MemoryConfig {
    /// Namespace override. Defaults to the agent's private `Agent` namespace.
    pub namespace: Option<Namespace>,
    /// Skip LLM enrichment (skip summarisation + tagging).
    pub skip_enrich: bool,
    /// Maximum recall results.
    pub max_results: Option<usize>,
    /// Only recall memories with importance ≥ this value (1-10).
    pub min_importance: Option<u8>,
    /// Tags to attach to a newly stored memory.
    pub tags: Vec<String>,
    /// Memory type for a newly stored memory.
    pub memory_type: Option<MemoryType>,
    /// Orthogonal class; defaults to factual knowledge.
    pub memory_class: crate::types::MemoryClass,
}

impl MemoryConfig {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn namespace(mut self, ns: Namespace) -> Self {
        self.namespace = Some(ns);
        self
    }
    pub fn skip_enrich(mut self) -> Self {
        self.skip_enrich = true;
        self
    }
    pub fn max_results(mut self, n: usize) -> Self {
        self.max_results = Some(n);
        self
    }
    pub fn min_importance(mut self, n: u8) -> Self {
        self.min_importance = Some(n);
        self
    }
    pub fn tags(mut self, tags: impl Into<Vec<String>>) -> Self {
        self.tags = tags.into();
        self
    }
    pub fn memory_type(mut self, memory_type: MemoryType) -> Self {
        self.memory_type = Some(memory_type);
        self
    }
    pub fn memory_class(mut self, memory_class: crate::types::MemoryClass) -> Self {
        self.memory_class = memory_class;
        self
    }
}

/// Ergonomic, agent-first memory API.
///
/// Owns a [`LibsqlStorage`] in `Arc<Mutex<…>>` – thread-safe and `Clone`.
/// All default operations target the agent's private [`Namespace::Agent`] scope.
/// The agent identifier is also used as a database filename, so it is restricted
/// to portable filename characters.
#[derive(Clone)]
pub struct MemoryManager {
    agent_id: String,
    storage: Arc<Mutex<crate::storage::libsql::LibsqlStorage>>,
    default_namespace: Namespace,
}

impl std::fmt::Debug for MemoryManager {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MemoryManager")
            .field("agent_id", &self.agent_id)
            .field("default_namespace", &self.default_namespace)
            .finish_non_exhaustive()
    }
}

fn validate_agent_id(agent_id: &str) -> Result<()> {
    if agent_id.is_empty()
        || agent_id == "."
        || agent_id == ".."
        || !agent_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
    {
        return Err(MnemosyneError::ValidationError(
            "agent_id must contain only ASCII letters, digits, '-', '_' or '.'".to_string(),
        ));
    }
    Ok(())
}

// ----------------------------------------------------------------------- factory

impl MemoryManager {
    /// Open (or create) the agent's database at the default location
    /// (`~/.mnemosyne/<agent_id>.db`).
    ///
    /// # Errors
    /// Fails if the database cannot be opened or initialised.
    pub async fn new(agent_id: impl Into<String>) -> Result<Self> {
        Self::new_with_path(agent_id, None).await
    }

    /// Open the agent's database at an explicit file path.
    pub async fn new_with_path(
        agent_id: impl Into<String>,
        db_path: Option<PathBuf>,
    ) -> Result<Self> {
        let agent_id: String = agent_id.into();
        validate_agent_id(&agent_id)?;
        let path = match db_path {
            Some(p) => p.display().to_string(),
            None => {
                let mut dir = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
                dir.push(".mnemosyne");
                let _ = std::fs::create_dir_all(&dir);
                dir.push(format!("{}.db", agent_id));
                dir.display().to_string()
            }
        };
        let mode = ConnectionMode::Local(path);
        let init = crate::storage::libsql::LibsqlStorage::new_with_validation(mode, true).await?;
        Ok(Self {
            agent_id: agent_id.clone(),
            storage: Arc::new(Mutex::new(init)),
            default_namespace: Namespace::Agent { agent_id },
        })
    }

    /// Open with an explicit [`ConnectionMode`] (e.g. remote libsql / Turso).
    pub async fn with_connection(
        agent_id: impl Into<String>,
        mode: ConnectionMode,
    ) -> Result<Self> {
        let agent_id: String = agent_id.into();
        validate_agent_id(&agent_id)?;
        let init = crate::storage::libsql::LibsqlStorage::new_with_validation(mode, true).await?;
        Ok(Self {
            agent_id: agent_id.clone(),
            storage: Arc::new(Mutex::new(init)),
            default_namespace: Namespace::Agent { agent_id },
        })
    }

    // --------------------------------------------------------------------- read

    /// Recall semantically similar memories via hybrid search.
    ///
    /// Results ranked highest score first.
    pub async fn recall(
        &self,
        query: impl Into<String>,
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        self.recall_with_config(query, limit, MemoryConfig::new())
            .await
    }

    /// Recall with full configuration (namespace, importance filter, …).
    pub async fn recall_with_config(
        &self,
        query: impl Into<String>,
        limit: usize,
        config: MemoryConfig,
    ) -> Result<Vec<SearchResult>> {
        let q: String = query.into();
        let ns = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let min_imp = config.min_importance;

        let mut results = {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner
                .hybrid_search(&q, Some(ns.clone()), limit * 2, true)
                .await?
        };

        if let Some(min) = min_imp {
            results.retain(|r| r.memory.importance >= min);
        }
        results.truncate(limit);
        results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        Ok(results)
    }

    /// Recall with confidence-gated abstention.
    ///
    /// Returns [`RecallDecision::Abstain`] when nothing scores above
    /// `threshold` rather than serving weak matches that invite over-answering.
    /// A threshold of `0.0` degrades to plain [`recall`](Self::recall).
    pub async fn recall_decided(
        &self,
        query: impl Into<String>,
        limit: usize,
        config: MemoryConfig,
        threshold: f32,
    ) -> Result<RecallDecision> {
        let results = self.recall_with_config(query, limit * 2, config).await?;
        let best = results.iter().map(|r| r.score).fold(0.0_f32, f32::max);
        if results.is_empty() {
            return Ok(RecallDecision::Abstain {
                reason: "no memories matched the query".to_string(),
                best_score: 0.0,
            });
        }
        if best < threshold {
            return Ok(RecallDecision::Abstain {
                reason: format!(
                    "best match score {:.2} below abstention threshold {:.2}",
                    best, threshold
                ),
                best_score: best,
            });
        }
        let mut results = results;
        results.truncate(limit);
        // Confidence is clamped top score (hybrid weights sum to ~1.0).
        let confidence = best.min(1.0);
        Ok(RecallDecision::Answer {
            results,
            confidence,
        })
    }

    /// Temporal supersession query: "what was true as of `as_of`?"
    ///
    /// Recalls normally, then filters through the supersedence timeline:
    /// - memories created after `as_of` are excluded (not yet true),
    /// - a memory superseded by another one created at or before `as_of` is
    ///   excluded (the newer fact was already in force),
    /// - a memory whose superseding memory did not exist yet at `as_of`
    ///   is kept (the old fact was still current then).
    pub async fn recall_as_of(
        &self,
        query: impl Into<String>,
        as_of: chrono::DateTime<chrono::Utc>,
        limit: usize,
        config: MemoryConfig,
    ) -> Result<Vec<SearchResult>> {
        let ns = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let mut results = {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner
                .keyword_search_as_of(&query.into(), &ns, as_of, limit)
                .await?
        };
        results.truncate(limit);
        Ok(results)
    }

    /// List memories sorted by recency, importance, or access count.
    pub async fn list(&self, limit: usize, sort_by: MemorySortOrder) -> Result<Vec<MemoryNote>> {
        self.list_with_config(limit, sort_by, MemoryConfig::new())
            .await
    }

    /// List with optional tag filter.
    pub async fn list_with_config(
        &self,
        limit: usize,
        sort_by: MemorySortOrder,
        config: MemoryConfig,
    ) -> Result<Vec<MemoryNote>> {
        let ns = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let guard = self.storage.lock().await;
        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
        let mut notes = inner.list_memories(Some(ns), limit, sort_by).await?;
        if !config.tags.is_empty() {
            let lower: Vec<String> = config.tags.iter().map(|t| t.to_lowercase()).collect();
            notes.retain(|n| n.tags.iter().any(|t| lower.contains(&t.to_lowercase())));
        }
        Ok(notes)
    }

    /// Fetch a memory by id, or `Ok(None)` if not found / archived.
    pub async fn get(&self, id: &MemoryId) -> Result<Option<MemoryNote>> {
        let guard = self.storage.lock().await;
        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
        // StorageBackend::get_memory takes MemoryId by value; returns Result<MemoryNote>.
        // We wrap in Option for the public API.
        match inner.get_memory(*id).await {
            Ok(m) if m.id == *id => Ok(Some(m)),
            Ok(_) => Ok(None),
            Err(MnemosyneError::MemoryNotFound(_)) => Ok(None),
            Err(e) => Err(e),
        }
    }

    // -------------------------------------------------------------------- write

    /// Store a plain-text memory in the agent's default namespace.
    ///
    /// The note receives a bounded summary and can be embedded when the backing
    /// storage has an embedding service configured. Use
    /// [`store_with_config`](Self::store_with_config) with `.skip_enrich()` to
    /// skip the optional embedding step.
    pub async fn store(&self, content: impl Into<String>) -> Result<MemoryId> {
        self.store_with_config(content, MemoryConfig::new()).await
    }

    /// Store with full configuration.
    pub async fn store_with_config(
        &self,
        content: impl Into<String>,
        config: MemoryConfig,
    ) -> Result<MemoryId> {
        let ns = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let content_str = content.into();

        let summary = crate::utils::string::truncate_at_char_boundary(&content_str, 200);
        let note = MemoryNote {
            id: MemoryId::new(),
            namespace: ns.clone(),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            content: content_str.clone(),
            summary,
            keywords: Vec::new(),
            tags: config.tags.clone(),
            context: String::new(),
            memory_type: config
                .memory_type
                .unwrap_or(crate::types::MemoryType::Insight),
            memory_class: config.memory_class,
            provenance: None,
            importance: config.min_importance.unwrap_or(5),
            confidence: 0.8,
            links: Vec::new(),
            related_files: Vec::new(),
            related_entities: Vec::new(),
            access_count: 0,
            last_accessed_at: chrono::Utc::now(),
            expires_at: None,
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: String::new(),
        };

        let note_id = note.id;

        // Persist via StorageBackend trait
        {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            <crate::storage::libsql::LibsqlStorage as StorageBackend>::store_memory(inner, &note)
                .await?;
        }

        // Optional embedding
        if !config.skip_enrich {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            let _ = inner
                .generate_and_store_embedding(&note_id, &content_str)
                .await;
        }

        Ok(note_id)
    }

    /// Natural-language alias for [`store`].
    pub async fn remember(&self, content: impl Into<String>) -> Result<MemoryId> {
        self.store(content).await
    }

    /// Update an existing memory; only `Some(..)` fields are changed.
    ///
    /// Changing `content` clears the embedding column (reconsolidation re-embeds).
    pub async fn update(
        &self,
        id: &MemoryId,
        content: Option<String>,
        importance: Option<u8>,
        tags: Option<Vec<String>>,
    ) -> Result<MemoryNote> {
        let guard = self.storage.lock().await;
        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;

        // Use MemoryUpdates struct: build update, then apply via trait
        // Use MemoryId by value (Copy); trait impl takes `id: MemoryId`.
        let current = inner.get_memory(*id).await?;

        // Apply partial updates in-memory
        let mut note = current;
        if let Some(c) = content {
            note.content = c;
            note.updated_at = chrono::Utc::now();
            note.embedding = None;
        }
        if let Some(i) = importance {
            note.importance = i;
        }
        if let Some(t) = tags {
            note.tags = t;
        }

        // Persist the updated MemoryNote (not MemoryUpdates)
        <crate::storage::libsql::LibsqlStorage as StorageBackend>::update_memory(inner, &note)
            .await?;
        // get_memory takes MemoryId by value; returns Result<MemoryNote> directly.
        Ok(inner.get_memory(*id).await?)
    }

    // -------------------------------------------------------------------- lifecycle

    /// Soft-delete (archive) a memory.
    pub async fn forget(&self, id: &MemoryId) -> Result<()> {
        let guard = self.storage.lock().await;
        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
        <crate::storage::libsql::LibsqlStorage as StorageBackend>::archive_memory(inner, *id)
            .await?;
        Ok(())
    }

    /// True delete: purge a memory from the store, embeddings, link graph,
    /// FTS index, and audit trail. Unrecoverable — unlike [`forget`](Self::forget).
    /// Returns a report of exactly what was removed ("forget X cascades and
    /// reports what was removed").
    pub async fn forget_purge(&self, id: &MemoryId) -> Result<PurgeReport> {
        let guard = self.storage.lock().await;
        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
        inner.purge_memory(id).await
    }

    /// Record that `new` supersedes `old` (archives the old fact and links the
    /// successor). Enables temporal queries — history is versioned, not erased.
    pub async fn supersede(&self, old: &MemoryId, new: &MemoryId) -> Result<()> {
        let guard = self.storage.lock().await;
        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
        inner.mark_superseded(old, new).await
    }

    /// "Forget X" cascade: find all memories in the namespace matching the
    /// given text (content/summary/keywords/tags/context, including archived)
    /// and truly delete them. Returns a per-memory removal report.
    pub async fn forget_matching(
        &self,
        needle: impl Into<String>,
        limit: usize,
        config: MemoryConfig,
    ) -> Result<Vec<PurgeReport>> {
        let ns = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let ids = {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner
                .find_purge_candidates(&ns, &needle.into(), limit)
                .await?
        };
        let mut reports = Vec::with_capacity(ids.len());
        for id in &ids {
            reports.push(self.forget_purge(id).await?);
        }
        Ok(reports)
    }
}

use crate::session_extract::{
    lexical_similarity, ExtractionStatus, SessionMessage, TurnLearningResult, SKIP_THRESHOLD,
};
use crate::utils::is_trivial_prompt;

impl MemoryManager {
    /// The agent identifier for this manager.
    #[inline]
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    /// Prefetch context for the given query, returning plain text ready for
    /// agent prompt injection.
    ///
    /// Mirrors the `prefetch_all` contract in hermes-agent's `MemoryManager`:
    /// returns merged plain-text blocks from each provider.  Failures in any
    /// provider are swallowed so the agent turn never blocks on memory.
    ///
    /// Returns empty string when the query is a trivial greeting ("hi",
    /// "thanks") or when no provider returns content.
    /// Recall factual evidence and internal response guidance independently.
    /// Quotas are applied before both channels are rendered under one shared
    /// token budget; policy memories never enter the factual channel.
    pub async fn recall_for_context(
        &self,
        query: impl Into<String>,
        config: MemoryConfig,
        budget_tokens: usize,
    ) -> Result<crate::agent_context::RecallBundle> {
        let q = query.into();
        if is_trivial_prompt(&q) {
            return Ok(crate::agent_context::RecallBundle {
                factual: crate::agent_context::RecallChannel {
                    results: Vec::new(),
                    quota: 5,
                    abstention_reason: Some("trivial prompt".into()),
                },
                guidance: crate::agent_context::RecallChannel {
                    results: Vec::new(),
                    quota: 3,
                    abstention_reason: Some("trivial prompt".into()),
                },
                budget_tokens,
            });
        }
        let mut factual_results = {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner
                .hybrid_search_by_class(
                    &q,
                    Some(
                        config
                            .namespace
                            .clone()
                            .unwrap_or_else(|| self.default_namespace.clone()),
                    ),
                    5,
                    true,
                    MemoryClass::Knowledge,
                )
                .await?
        };
        if let Some(min_importance) = config.min_importance {
            factual_results.retain(|result| result.memory.importance >= min_importance);
        }
        let best_factual = factual_results
            .iter()
            .map(|result| result.score)
            .fold(0.0_f32, f32::max);
        let (factual, factual_reason) = if factual_results.is_empty() {
            (
                Vec::new(),
                Some("no factual memories matched the query".to_string()),
            )
        } else if best_factual < DEFAULT_ABSTENTION_THRESHOLD {
            (
                Vec::new(),
                Some(format!(
                    "best factual match score {:.2} below abstention threshold {:.2}",
                    best_factual, DEFAULT_ABSTENTION_THRESHOLD
                )),
            )
        } else {
            (factual_results, None)
        };
        // Policies are global and selected independently. Their source turns
        // must still be live, and anchor matching never calls an LLM.
        let guidance = {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner.search_interaction_policies(&q, 3).await?
        };
        let guidance_reason = if guidance.is_empty() {
            Some("no eligible anchored policy matched the query".to_string())
        } else {
            None
        };
        Ok(crate::agent_context::RecallBundle {
            factual: crate::agent_context::RecallChannel {
                results: factual.into_iter().take(5).collect(),
                quota: 5,
                abstention_reason: factual_reason,
            },
            guidance: crate::agent_context::RecallChannel {
                results: guidance,
                quota: 3,
                abstention_reason: guidance_reason,
            },
            budget_tokens,
        })
    }

    pub async fn prefetch(&self, query: impl Into<String>) -> String {
        self.prefetch_with_config(query, MemoryConfig::new()).await
    }

    /// Prefetch context with namespace and result-count configuration.
    pub async fn prefetch_with_config(
        &self,
        query: impl Into<String>,
        config: MemoryConfig,
    ) -> String {
        let q = query.into();
        if is_trivial_prompt(&q) {
            return String::new();
        }
        // One-shot recall: pull top results and join their content into
        // a single string to feed back into the model as context.
        let limit = config.max_results.unwrap_or(5);
        match self.recall_with_config(&q, limit, config).await {
            Ok(hits) => {
                let parts: Vec<String> = hits
                    .into_iter()
                    .filter(|r| r.score > 0.0)
                    .map(|r| {
                        format!(
                            "[relevance={:.2}] {}\n---",
                            r.score,
                            r.memory.content.trim()
                        )
                    })
                    .collect();
                parts.join("\n\n")
            }
            Err(_) => String::new(),
        }
    }

    /// Like [`prefetch`] but includes the `<memory-context>` fence block
    /// so the output is directly injectable into the user message.
    ///
    /// The fenced block isolates memory context from user input so
    /// the model treats it as reference data, not new conversation.
    pub fn build_context_block(&self, prefetched_text: impl AsRef<str>) -> String {
        build_memory_context_block(prefetched_text.as_ref())
    }

    /// Recall and render the bounded dual-channel fenced context block.
    pub async fn prefetch_context_block(
        &self,
        query: impl Into<String>,
        config: MemoryConfig,
        budget_tokens: usize,
    ) -> Result<String> {
        let bundle = self
            .recall_for_context(query, config, budget_tokens)
            .await?;
        Ok(crate::agent_context::build_memory_context_block(
            crate::agent_context::render_recall_bundle(&bundle),
        ))
    }

    /// Sync a completed turn to memory — store a user+assistant exchange
    /// so the agent's persistent memory captures what happened this turn.
    ///
    /// Mirrors `MemoryManager::sync_all` in hermes-agent.  The exchange is
    /// stored as a single memory note tagged `\"turn_sync\"` in the agent's
    /// default namespace.  Enrichment (LLM summary/embedding) is skipped for
    /// speed — call `mnemosyne embed` or run evolution to backfill later.
    ///
    /// Errors are returned (not swallowed) so the agent can decide, but the
    /// call is cheap: it is a single INSERT.
    pub async fn sync(&self, user_text: &str, assistant_text: &str) -> Result<MemoryId> {
        self.sync_with_config(user_text, assistant_text, MemoryConfig::new())
            .await
    }

    /// Sync a completed turn with an optional namespace, tags, and memory type.
    pub async fn sync_with_config(
        &self,
        user_text: &str,
        assistant_text: &str,
        config: MemoryConfig,
    ) -> Result<MemoryId> {
        let ns = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let content = format!(
            "User: {}\n\nAssistant: {}",
            user_text.trim(),
            assistant_text.trim()
        );
        let now = chrono::Utc::now();
        let note = MemoryNote {
            id: MemoryId::new(),
            namespace: ns.clone(),
            created_at: now,
            updated_at: now,
            content: content.clone(),
            summary: crate::utils::string::truncate_at_char_boundary(&content, 200),
            keywords: Vec::new(),
            tags: {
                let mut tags = vec!["turn_sync".to_string()];
                tags.extend(config.tags);
                tags
            },
            context: "Agent turn sync".to_string(),
            memory_type: config
                .memory_type
                .unwrap_or(crate::types::MemoryType::Insight),
            // A raw turn is always factual source material; derived policy
            // notes use InteractionPolicy explicitly.
            memory_class: MemoryClass::Knowledge,
            provenance: None,
            importance: 5,
            confidence: 0.7,
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
        };

        let note_id = note.id;
        {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            <crate::storage::libsql::LibsqlStorage as StorageBackend>::store_memory(inner, &note)
                .await?;
        }

        Ok(note_id)
    }

    /// Synchronize a completed turn, then perform one strict LLM extraction.
    /// The raw turn is durable even when authentication, transport, or parsing
    /// fails; failed extraction returns a retryable status and writes no
    /// derived memory.
    pub async fn sync_and_learn(
        &self,
        user_text: &str,
        assistant_text: &str,
    ) -> Result<TurnLearningResult> {
        self.sync_and_learn_with_config_metadata(
            user_text,
            assistant_text,
            MemoryConfig::new(),
            None,
            None,
        )
        .await
    }

    /// Variant that attaches caller-provided session and turn identifiers.
    pub async fn sync_and_learn_with_metadata(
        &self,
        user_text: &str,
        assistant_text: &str,
        session_id: Option<&str>,
        turn_id: Option<&str>,
    ) -> Result<TurnLearningResult> {
        self.sync_and_learn_with_config_metadata(
            user_text,
            assistant_text,
            MemoryConfig::new(),
            session_id,
            turn_id,
        )
        .await
    }

    /// Learn a turn in an explicit namespace without changing legacy `sync`.
    pub async fn sync_and_learn_with_config(
        &self,
        user_text: &str,
        assistant_text: &str,
        config: MemoryConfig,
    ) -> Result<TurnLearningResult> {
        self.sync_and_learn_with_config_metadata(user_text, assistant_text, config, None, None)
            .await
    }

    fn completed_learning_result(
        source_memory_id: MemoryId,
        existing: Vec<(MemoryId, MemoryClass)>,
    ) -> Option<TurnLearningResult> {
        if existing.is_empty() {
            return None;
        }
        let (derived_ids, policy_ids): (Vec<_>, Vec<_>) = existing
            .into_iter()
            .partition(|(_, class)| *class == MemoryClass::Knowledge);
        Some(TurnLearningResult {
            source_memory_id,
            derived_ids: derived_ids.into_iter().map(|(id, _)| id).collect(),
            policy_ids: policy_ids.into_iter().map(|(id, _)| id).collect(),
            extraction_status: ExtractionStatus::Succeeded,
        })
    }

    async fn sync_and_learn_with_config_metadata(
        &self,
        user_text: &str,
        assistant_text: &str,
        config: MemoryConfig,
        session_id: Option<&str>,
        turn_id: Option<&str>,
    ) -> Result<TurnLearningResult> {
        let target_namespace = config
            .namespace
            .unwrap_or_else(|| self.default_namespace.clone());
        let now = chrono::Utc::now();
        // Keep the exact payload used by extraction. Trimming here would let
        // a quoted boundary pass validation but disappear from the durable
        // source record.
        let raw_content = format!("User: {}\n\nAssistant: {}", user_text, assistant_text);
        // Session/turn metadata is an idempotency key when both values are
        // supplied. A retry after a transient LLM failure reuses the durable
        // raw source instead of creating a second copy of the conversation.
        let mut existing_source_id = match (session_id, turn_id) {
            (Some(session_id), Some(turn_id)) if !session_id.is_empty() && !turn_id.is_empty() => {
                let guard = self.storage.lock().await;
                let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
                inner
                    .find_turn_source_memory(&target_namespace, session_id, turn_id)
                    .await?
            }
            _ => None,
        };
        let mut source_memory_id = existing_source_id.unwrap_or_else(MemoryId::new);
        if let Some(existing_source_id) = existing_source_id {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            let source = inner.get_memory(existing_source_id).await?;
            if source.is_archived {
                return Err(crate::error::MnemosyneError::ValidationError(
                    "cannot retry learning an archived turn".into(),
                ));
            }
            if source.content != raw_content {
                return Err(crate::error::MnemosyneError::ValidationError(
                    "session/turn identity already exists with different content".into(),
                ));
            }
            let existing = inner
                .derived_memories_for_source(existing_source_id)
                .await?;
            if let Some(result) = Self::completed_learning_result(existing_source_id, existing) {
                return Ok(result);
            }
        }
        let raw_note = MemoryNote {
            id: source_memory_id,
            namespace: target_namespace.clone(),
            created_at: now,
            updated_at: now,
            content: raw_content.clone(),
            summary: crate::utils::string::truncate_at_char_boundary(&raw_content, 200),
            keywords: Vec::new(),
            tags: {
                let mut tags = vec!["turn_sync".into()];
                tags.extend(config.tags.clone());
                tags
            },
            context: "Agent turn sync".into(),
            memory_type: config.memory_type.unwrap_or(MemoryType::Insight),
            memory_class: MemoryClass::Knowledge,
            provenance: Some(crate::types::MemoryProvenance {
                source_kind: crate::types::ProvenanceSourceKind::Turn,
                source_memory_id: None,
                session_id: session_id.map(str::to_owned),
                turn_id: turn_id.map(str::to_owned),
                source_role: crate::types::ProvenanceSourceRole::Unknown,
                observed_at: now,
                evidence_quote: raw_content.chars().take(2_000).collect(),
                extractor_model: None,
                extraction_schema_version: None,
            }),
            importance: 5,
            confidence: 0.7,
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
        };
        // The raw turn is committed before contacting the LLM. It is the
        // durable retry anchor when authentication, transport, or validation
        // fails, and it is never rolled back with derived writes.
        if existing_source_id.is_none() {
            let store_result = {
                let guard = self.storage.lock().await;
                let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
                <crate::storage::libsql::LibsqlStorage as StorageBackend>::store_memory(
                    inner, &raw_note,
                )
                .await
            };
            if let Err(error) = store_result {
                // The unique partial index is the atomic claim. If another
                // retry won the race, adopt its source after checking that it
                // represents the same payload; unrelated write failures still
                // propagate unchanged.
                let winner = match (session_id, turn_id) {
                    (Some(session_id), Some(turn_id))
                        if !session_id.is_empty() && !turn_id.is_empty() =>
                    {
                        let guard = self.storage.lock().await;
                        let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
                        inner
                            .find_turn_source_memory(&target_namespace, session_id, turn_id)
                            .await?
                    }
                    _ => None,
                };
                let Some(winner_id) = winner else {
                    return Err(error);
                };
                let guard = self.storage.lock().await;
                let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
                let winner_memory = inner.get_memory(winner_id).await?;
                if winner_memory.is_archived {
                    return Err(crate::error::MnemosyneError::ValidationError(
                        "cannot retry learning an archived turn".into(),
                    ));
                }
                if winner_memory.content != raw_content {
                    return Err(crate::error::MnemosyneError::ValidationError(
                        "session/turn identity already exists with different content".into(),
                    ));
                }
                source_memory_id = winner_id;
                existing_source_id = Some(winner_id);
                let existing = inner.derived_memories_for_source(winner_id).await?;
                if let Some(result) = Self::completed_learning_result(winner_id, existing) {
                    return Ok(result);
                }
            }
        }

        let messages = vec![
            SessionMessage::new("user", user_text),
            SessionMessage::new("assistant", assistant_text),
        ];
        let extraction = match crate::services::LlmService::with_default() {
            Ok(service) => match service.extract_turn(&messages).await {
                Ok(extraction) => extraction,
                Err(error) => {
                    return Ok(TurnLearningResult {
                        source_memory_id,
                        derived_ids: Vec::new(),
                        policy_ids: Vec::new(),
                        extraction_status: ExtractionStatus::FailedRetryable {
                            error: error.to_string(),
                        },
                    })
                }
            },
            Err(error) => {
                return Ok(TurnLearningResult {
                    source_memory_id,
                    derived_ids: Vec::new(),
                    policy_ids: Vec::new(),
                    extraction_status: ExtractionStatus::FailedRetryable {
                        error: error.to_string(),
                    },
                })
            }
        };

        // Build and validate the complete derived batch in memory first. No
        // derived row is written until every candidate and entity is ready.
        // Resolve exact/near duplicates before materializing new rows. This
        // intentionally uses a bounded lexical pass: the strict extractor is
        // still the source of candidate meaning, while duplicate suppression
        // remains deterministic and does not require another LLM call.
        let existing_knowledge = {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner
                .list_memories(
                    Some(target_namespace.clone()),
                    256,
                    crate::storage::MemorySortOrder::Recent,
                )
                .await?
                .into_iter()
                .filter(|memory| {
                    memory.memory_class == MemoryClass::Knowledge
                        && !memory.is_archived
                        && memory.superseded_by.is_none()
                })
                .collect::<Vec<_>>()
        };
        let mut accepted_contents = Vec::new();
        let mut items = Vec::new();
        let mut derived_ids = Vec::new();
        for candidate in extraction.candidates {
            let duplicate = accepted_contents.iter().any(|content: &String| {
                lexical_similarity(content, &candidate.content) >= SKIP_THRESHOLD
            }) || existing_knowledge.iter().any(|memory| {
                lexical_similarity(&memory.content, &candidate.content) >= SKIP_THRESHOLD
            });
            if duplicate {
                continue;
            }
            accepted_contents.push(candidate.content.clone());
            let id = MemoryId::new();
            let candidate_kind = candidate.kind.clone();
            let entities: Vec<crate::types::MemoryEntity> = candidate
                .entities
                .iter()
                .map(|entity| crate::types::MemoryEntity {
                    display_name: entity.display_name.clone(),
                    normalized_name: entity.normalized_key.clone(),
                    role: entity.role.clone(),
                    confidence: entity.confidence,
                })
                .collect();
            let note = MemoryNote {
                id,
                namespace: target_namespace.clone(),
                created_at: now,
                updated_at: now,
                content: candidate.content.clone(),
                summary: crate::utils::string::truncate_at_char_boundary(&candidate.content, 200),
                keywords: Vec::new(),
                tags: vec!["extracted".into()],
                context: candidate_kind.clone(),
                memory_type: match candidate_kind.as_str() {
                    "preference" => MemoryType::Preference,
                    "constraint" => MemoryType::Constraint,
                    "decision" => MemoryType::ArchitectureDecision,
                    _ => MemoryType::Insight,
                },
                memory_class: MemoryClass::Knowledge,
                provenance: Some(crate::types::MemoryProvenance {
                    source_kind: crate::types::ProvenanceSourceKind::Turn,
                    source_memory_id: Some(source_memory_id),
                    session_id: session_id.map(str::to_owned),
                    turn_id: turn_id.map(str::to_owned),
                    source_role: match candidate.source_role.as_str() {
                        "user" => crate::types::ProvenanceSourceRole::User,
                        "assistant" => crate::types::ProvenanceSourceRole::Assistant,
                        _ => crate::types::ProvenanceSourceRole::System,
                    },
                    observed_at: now,
                    evidence_quote: candidate.evidence_quote,
                    extractor_model: Some("configured-anthropic".into()),
                    extraction_schema_version: Some(
                        crate::session_extract::EXTRACTION_SCHEMA_VERSION.into(),
                    ),
                }),
                importance: 5,
                confidence: candidate.confidence,
                links: vec![MemoryLink {
                    target_id: source_memory_id,
                    link_type: crate::types::LinkType::References,
                    strength: 1.0,
                    reason: "extracted from completed turn".into(),
                    created_at: now,
                    last_traversed_at: None,
                    user_created: false,
                }],
                related_files: Vec::new(),
                related_entities: entities
                    .iter()
                    .map(|entity| entity.display_name.clone())
                    .collect(),
                access_count: 0,
                last_accessed_at: now,
                expires_at: None,
                is_archived: false,
                superseded_by: None,
                embedding: None,
                embedding_model: String::new(),
            };
            derived_ids.push(id);
            items.push(crate::storage::libsql::LearningMemory {
                memory: note,
                entities,
            });
        }

        let mut policy_ids = Vec::new();
        let mut policy_update = None;
        let mut superseded_policy = None;
        if let Some(feedback) = extraction
            .response_feedback
            .filter(|feedback| feedback.is_actionable())
        {
            let polarity = if feedback.polarity == "avoid" {
                crate::types::PolicyPolarity::Avoid
            } else {
                crate::types::PolicyPolarity::Prefer
            };
            let signal = match feedback.signal.as_str() {
                "correction" => crate::types::PolicySignalKind::Correction,
                "dissatisfaction" => crate::types::PolicySignalKind::Dissatisfaction,
                "approval" => crate::types::PolicySignalKind::Approval,
                _ => crate::types::PolicySignalKind::DirectPreference,
            };
            let evidence = crate::types::MemoryProvenance {
                source_kind: crate::types::ProvenanceSourceKind::Turn,
                source_memory_id: Some(source_memory_id),
                session_id: session_id.map(str::to_owned),
                turn_id: turn_id.map(str::to_owned),
                source_role: crate::types::ProvenanceSourceRole::User,
                observed_at: now,
                evidence_quote: feedback.evidence_quote.clone(),
                extractor_model: Some("configured-anthropic".into()),
                extraction_schema_version: Some(
                    crate::session_extract::EXTRACTION_SCHEMA_VERSION.into(),
                ),
            };
            let policy_provenance = evidence.clone();
            let mut policy = crate::types::InteractionPolicy {
                polarity,
                guidance: feedback.guidance.clone(),
                applicability: feedback.applicability.clone(),
                signal,
                confidence: feedback.confidence,
                anchors: feedback.anchors.clone(),
                evidence: vec![evidence],
            };
            let existing = {
                let guard = self.storage.lock().await;
                let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
                inner.list_interaction_policies().await?
            };
            let matching = existing
                .into_iter()
                .filter(|(memory, old)| {
                    if memory.is_archived
                        || memory.superseded_by.is_some()
                        || old.evidence.is_empty()
                    {
                        return false;
                    }
                    let anchors_overlap = old.anchors.iter().any(|old_anchor| {
                        policy
                            .anchors
                            .iter()
                            .any(|anchor| old_anchor.eq_ignore_ascii_case(anchor))
                    });
                    let same_condition = crate::session_extract::lexical_similarity(
                        &old.applicability,
                        &policy.applicability,
                    ) >= 0.5;
                    (anchors_overlap && same_condition)
                        || (crate::session_extract::lexical_similarity(
                            &old.guidance,
                            &policy.guidance,
                        ) >= 0.85
                            && same_condition)
                })
                .max_by_key(|(memory, _)| memory.updated_at);
            let id = if let Some((old_memory, old_policy)) = matching {
                let same_condition = crate::session_extract::lexical_similarity(
                    &old_policy.applicability,
                    &policy.applicability,
                ) >= 0.5;
                let same_guidance = same_condition
                    && crate::session_extract::lexical_similarity(
                        &old_policy.guidance,
                        &policy.guidance,
                    ) >= 0.85
                    && old_policy.polarity == policy.polarity;
                if same_guidance {
                    // A guidance merge must preserve the applicability surface
                    // of both observations. Otherwise a coding policy merged
                    // with an otherwise identical writing policy would lose
                    // one of its anchors.
                    for old_anchor in old_policy.anchors {
                        if !policy
                            .anchors
                            .iter()
                            .any(|anchor| anchor.eq_ignore_ascii_case(&old_anchor))
                        {
                            policy.anchors.push(old_anchor);
                        }
                    }
                    policy.evidence.splice(0..0, old_policy.evidence);
                    policy.confidence = policy.confidence.max(old_policy.confidence);
                    old_memory.id
                } else if old_policy.polarity != policy.polarity {
                    let new_id = MemoryId::new();
                    superseded_policy = Some((old_memory.id, new_id));
                    new_id
                } else {
                    MemoryId::new()
                }
            } else {
                MemoryId::new()
            };
            policy_ids.push(id);
            if !items
                .iter()
                .any(|item: &crate::storage::libsql::LearningMemory| item.memory.id == id)
            {
                let entities = policy
                    .anchors
                    .iter()
                    .map(|anchor| crate::types::MemoryEntity {
                        display_name: anchor.clone(),
                        normalized_name: anchor.to_lowercase(),
                        role: "anchor".into(),
                        confidence: 1.0,
                    })
                    .collect::<Vec<_>>();
                let note = MemoryNote {
                    id,
                    namespace: Namespace::Global,
                    created_at: now,
                    updated_at: now,
                    content: policy.guidance.clone(),
                    summary: policy.guidance.clone(),
                    keywords: Vec::new(),
                    tags: vec!["interaction_policy".into()],
                    context: policy.applicability.clone(),
                    memory_type: MemoryType::Preference,
                    memory_class: MemoryClass::InteractionPolicy,
                    provenance: Some(policy_provenance),
                    importance: 5,
                    confidence: policy.confidence,
                    links: vec![MemoryLink {
                        target_id: source_memory_id,
                        link_type: crate::types::LinkType::References,
                        strength: 1.0,
                        reason: "policy evidence from completed turn".into(),
                        created_at: now,
                        last_traversed_at: None,
                        user_created: false,
                    }],
                    related_files: Vec::new(),
                    related_entities: policy.anchors.clone(),
                    access_count: 0,
                    last_accessed_at: now,
                    expires_at: None,
                    is_archived: false,
                    superseded_by: None,
                    embedding: None,
                    embedding_model: String::new(),
                };
                items.push(crate::storage::libsql::LearningMemory {
                    memory: note,
                    entities,
                });
            }
            policy_update = Some((id, policy));
        }

        {
            let guard = self.storage.lock().await;
            let inner: &crate::storage::libsql::LibsqlStorage = &*guard;
            inner
                .store_learning_batch(&items, policy_update, superseded_policy)
                .await?;
        }
        Ok(TurnLearningResult {
            source_memory_id,
            derived_ids,
            policy_ids,
            extraction_status: ExtractionStatus::Succeeded,
        })
    }

    // -------------------------------------------------------------------- best-effort wrappers

    /// Best-effort recall: never propagates errors.
    ///
    /// Agent code can call this without defensive `try/except` wrappers.
    pub async fn recall_best_effort(
        &self,
        query: impl Into<String>,
        limit: usize,
    ) -> Vec<SearchResult> {
        self.recall(query, limit).await.unwrap_or_default()
    }

    /// Best-effort forget: logs and swallows any error.
    pub async fn forget_best_effort(&self, id: &MemoryId) {
        if let Err(e) = self.forget(id).await {
            tracing::debug!("forget_best_effort: ignoring error for {}: {}", id, e);
        }
    }
}

/// Wrap prefetched memory context in a `<memory-context>` fence block.
///
/// Re-exported from [`crate::agent_context`] for backward compatibility.
pub use crate::agent_context::build_memory_context_block;

// ===================================================================== tests

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn mn_mgr_new_sets_agent_id() {
        let mgr = MemoryManager::new("t").await.unwrap();
        assert_eq!(mgr.agent_id(), "t");
    }

    #[tokio::test]
    async fn mn_mgr_rejects_path_traversal_agent_ids() {
        let err = MemoryManager::new("../outside").await.unwrap_err();
        assert!(matches!(err, MnemosyneError::ValidationError(_)));
    }

    #[tokio::test]
    async fn mn_mgr_defaults_to_agent_namespace() {
        let mgr = MemoryManager::new("ns").await.unwrap();
        assert!(matches!(mgr.default_namespace, Namespace::Agent { .. } if mgr.agent_id() == "ns"));
    }

    #[tokio::test]
    async fn mn_mgr_store_recall_round_trip() {
        let aid = format!("rt-{}", uuid::Uuid::new_v4());
        let mgr = MemoryManager::new(&aid).await.unwrap();
        mgr.store("Cats have five toes front, four back.")
            .await
            .unwrap();
        let hits = mgr.recall("How many cat toes?", 5).await.unwrap();
        assert!(!hits.is_empty(), "need >= 1 result, got {:?}", hits);
        assert!(hits.iter().any(|r| r.score >= 0.0));
    }

    #[tokio::test]
    async fn mn_mgr_namespace_isolation() {
        let a = MemoryManager::new(format!("ia-{}", uuid::Uuid::new_v4()))
            .await
            .unwrap();
        let b = MemoryManager::new(format!("ib-{}", uuid::Uuid::new_v4()))
            .await
            .unwrap();
        a.store("secret A").await.unwrap();
        b.store("secret B").await.unwrap();
        let r = a.recall("secret A", 10).await.unwrap();
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].memory.summary, "secret A");
    }

    #[tokio::test]
    async fn mn_mgr_list_and_tag_filter() {
        let aid = format!("lst-{}", uuid::Uuid::new_v4());
        let mgr = MemoryManager::new(&aid).await.unwrap();
        mgr.store("alpha").await.unwrap();
        mgr.store_with_config("beta", MemoryConfig::new().tags(vec!["t".into()]))
            .await
            .unwrap();
        let all = mgr.list(10, MemorySortOrder::Recent).await.unwrap();
        assert!(all.len() >= 2, "need >= 2, got {}", all.len());
        let tagged = mgr
            .list_with_config(
                10,
                MemorySortOrder::Recent,
                MemoryConfig::new().tags(vec!["t".into()]),
            )
            .await
            .unwrap();
        assert_eq!(tagged.len(), 1);
        assert!(tagged[0].tags.contains(&"t".to_string()));
    }

    #[tokio::test]
    async fn mn_mgr_forget_removes_from_recall() {
        let aid = format!("fg-{}", uuid::Uuid::new_v4());
        let mgr = MemoryManager::new(&aid).await.unwrap();
        let id = mgr.store("to forget").await.unwrap();
        mgr.forget(&id).await.unwrap();
        assert!(mgr.recall("to forget", 10).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn mn_mgr_update_changes_fields() {
        let aid = format!("upd-{}", uuid::Uuid::new_v4());
        let mgr = MemoryManager::new(&aid).await.unwrap();
        let id = mgr.store("orig").await.unwrap();
        let u = mgr
            .update(&id, Some("revised".into()), Some(9), None)
            .await
            .unwrap();
        assert_eq!(u.importance, 9);
        assert!(u.content.contains("revised"));
    }

    #[tokio::test]
    async fn mn_mgr_get_missing_returns_none() {
        assert!(MemoryManager::new("gn")
            .await
            .unwrap()
            .get(&MemoryId::new())
            .await
            .unwrap()
            .is_none());
    }

    // ── Tests for the agent-usability API added for hermes-agent parity ──

    #[tokio::test]
    async fn mn_mgr_prefetch_returns_text() {
        let aid = format!("pf-{}", uuid::Uuid::new_v4());
        let mgr = MemoryManager::new(&aid).await.unwrap();
        mgr.store_with_config(
            "Cats have five toes on front paws.",
            MemoryConfig::new().skip_enrich(),
        )
        .await
        .unwrap();
        let text = mgr.prefetch("cat toes").await;
        assert!(
            text.contains("Cats"),
            "prefetch should return text content, got: {text}"
        );
    }

    #[tokio::test]
    async fn mn_mgr_prefetch_trivial_returns_empty() {
        let mgr = MemoryManager::new("trivial").await.unwrap();
        assert_eq!(mgr.prefetch("hi").await, "");
        assert_eq!(mgr.prefetch("thanks").await, "");
        assert_eq!(mgr.prefetch("ok").await, "");
    }

    #[tokio::test]
    async fn mn_mgr_context_block_wraps_text() {
        let mgr = MemoryManager::new("cb").await.unwrap();
        let block = mgr.build_context_block("The user prefers dark mode.");
        assert!(block.contains("<memory-context>"));
        assert!(block.contains("dark mode"));
        assert!(block.contains("System note"));
    }

    #[tokio::test]
    async fn mn_mgr_context_block_strips_prewrapped() {
        // When the prefetched text already contains a context block,
        // build_context_block calls sanitize_context to strip it, preventing
        // a double-wrapped <memory-context> block. The sanitized content
        // (the "Hello" that was *between* the tags) is preserved.
        let mgr = MemoryManager::new("strip").await.unwrap();
        let input = "<memory-context>\n[System note: old]\nHello\n</memory-context>";
        let block = mgr.build_context_block(input);
        // After sanitize_context strips the block, the remaining text is
        // "\nHello\n" (leading content before <memory-context> minus
        // the block). Since input *starts* with the tag, sanitize_context
        // removes it entirely. The result is stripped and then re-wrapped.
        assert!(block.starts_with("<memory-context>"));
        assert!(block.ends_with("</memory-context>"));
        // Should contain exactly one opening and one closing tag (no double-wrap)
        assert_eq!(block.matches("<memory-context>").count(), 1);
        assert_eq!(block.matches("</memory-context>").count(), 1);
    }

    #[tokio::test]
    async fn mn_mgr_recall_best_effort_never_errors() {
        let mgr = MemoryManager::new("bef").await.unwrap();
        // Even with no DB / no memories, should return empty vec, not panic.
        let hits = mgr.recall_best_effort("nothing here", 5).await;
        assert!(hits.is_empty());
    }

    #[tokio::test]
    async fn mn_mgr_forget_best_effort_never_errors() {
        let mgr = MemoryManager::new("fbf").await.unwrap();
        // Forgetting a non-existent ID should not panic.
        mgr.forget_best_effort(&MemoryId::new()).await;
    }

    #[tokio::test]
    async fn mn_mgr_sync_stores_exchange() {
        let aid = format!("sync-{}", uuid::Uuid::new_v4());
        let mgr = MemoryManager::new(&aid).await.unwrap();
        let id = mgr
            .sync("What is the project status?", "The project is on track.")
            .await
            .unwrap();
        // The synced exchange should be recallable.
        let hits = mgr.recall("project status", 5).await.unwrap();
        assert!(
            hits.iter().any(|r| r.memory.id == id),
            "sync result should be found in recall, got: {:?}",
            hits
        );
    }
}
