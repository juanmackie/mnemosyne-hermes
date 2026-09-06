//! Memory entry creation for artifacts
//!
//! Phase 1: Basic memory creation without complex linking.
//! Link functionality will be added in Phase 2 when implementing slash commands.
//!
//! A-MEM post-insert hook (arxiv 2502.12110): at insert time, find the k nearest
//! existing memories and have the LLM (a) propose cross-links between the new and
//! old memories. This makes the memory graph denser and fresher immediately,
//! without waiting for a periodic scheduler run. It is cost-gated by
//! `evolution.on_insert.enabled` (off by default).

use crate::error::Result;
use crate::evolution::config::OnInsertConfig;
use crate::services::llm::LlmService;
use crate::storage::StorageBackend;
use crate::types::{MemoryId, MemoryLink, MemoryNote, MemoryType, Namespace};
use async_trait::async_trait;
use std::sync::Arc;

/// Strategy for proposing cross-links between a newly-inserted memory and a set
/// of existing candidate memories.
///
/// Abstracted so the post-insert hook can be unit-tested deterministically
/// without hitting a live LLM API.
#[async_trait]
pub trait LinkProposer: Send + Sync {
    /// Return proposed links whose `source` is `new_memory` and whose `target_id`
    /// references one of `candidates`. Implementations should set
    /// `user_created = false` so link decay can later prune weak auto-links.
    async fn propose(
        &self,
        new_memory: &MemoryNote,
        candidates: &[MemoryNote],
    ) -> Result<Vec<MemoryLink>>;
}

/// Real A-MEM proposer backed by `LlmService::generate_links`.
pub struct LlmLinkProposer {
    llm: Arc<LlmService>,
}

impl LlmLinkProposer {
    /// Create a new LLM-backed proposer.
    pub fn new(llm: Arc<LlmService>) -> Self {
        Self { llm }
    }
}

#[async_trait]
impl LinkProposer for LlmLinkProposer {
    async fn propose(
        &self,
        new_memory: &MemoryNote,
        candidates: &[MemoryNote],
    ) -> Result<Vec<MemoryLink>> {
        self.llm.generate_links(new_memory, candidates).await
    }
}

/// Memory linker for creating artifact memory entries
///
/// Optionally runs the A-MEM post-insert link-proposal hook after storing a new
/// memory. When the hook is disabled (the default), behavior is identical to a
/// plain insert.
pub struct MemoryLinker {
    storage: Arc<dyn StorageBackend>,
    proposer: Option<Arc<dyn LinkProposer>>,
    on_insert: OnInsertConfig,
}

impl MemoryLinker {
    /// Create a new memory linker without the post-insert hook enabled.
    ///
    /// Equivalent to `MemoryLinker::with_insert_hook(storage, None,
    /// OnInsertConfig::default())`; the hook is inert until enabled via
    /// [`MemoryLinker::with_insert_hook`].
    pub fn new(storage: Arc<dyn StorageBackend>) -> Self {
        Self {
            storage,
            proposer: None,
            on_insert: OnInsertConfig::default(),
        }
    }

    /// Create a memory linker that will run the A-MEM post-insert hook when
    /// `on_insert.enabled` is `true`.
    ///
    /// `proposer` supplies the link-proposal strategy (typically an
    /// [`LlmLinkProposer`] wrapping the existing `LlmService`).
    pub fn with_insert_hook(
        storage: Arc<dyn StorageBackend>,
        proposer: Option<Arc<dyn LinkProposer>>,
        on_insert: OnInsertConfig,
    ) -> Self {
        Self {
            storage,
            proposer,
            on_insert,
        }
    }

    /// Create a memory entry for an artifact, then (if the A-MEM insert hook is
    /// enabled) propose and persist cross-links to the k nearest memories.
    pub async fn create_artifact_memory(
        &self,
        memory_type: MemoryType,
        content: String,
        namespace: Namespace,
        artifact_path: String,
        importance: u8,
        tags: Vec<String>,
    ) -> Result<MemoryId> {
        let now = chrono::Utc::now();
        let memory = MemoryNote {
            id: MemoryId::new(),
            namespace,
            created_at: now,
            updated_at: now,
            content: content.clone(),
            summary: format!("Artifact: {}", artifact_path),
            keywords: Vec::new(),
            tags,
            context: format!("Specification artifact stored at {}", artifact_path),
            memory_type,
            importance,
            confidence: 1.0,
            links: Vec::new(),
            related_files: Vec::new(),
            related_entities: Vec::new(),
            access_count: 0,
            last_accessed_at: now,
            expires_at: None,
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: "none".to_string(),
            memory_class: crate::types::MemoryClass::Knowledge,
            provenance: None,
        };

        self.storage.store_memory(&memory).await?;

        // A-MEM post-insert hook (cost-gated, best-effort). Any failure degrades
        // to a plain insert — the memory itself is already persisted.
        if self.on_insert.enabled && self.proposer.is_some() {
            let _ = self.run_insert_hook(memory.id).await;
        }

        Ok(memory.id)
    }

    /// Run the A-MEM post-insert link-proposal hook for `new_memory_id`.
    ///
    /// 1. Fetch the freshly-inserted memory and its embedding.
    /// 2. Find the k nearest existing memories (vector search), excluding self.
    /// 3. Ask the proposer (LLM) which of those deserve a cross-link.
    /// 4. Append the proposed links to the new memory and persist them.
    ///
    /// Returns the number of links proposed and written. Returns `0` without
    /// error whenever the hook is disabled, no proposer/LlmService is configured,
    /// or the new memory has no embedding to seed the kNN query.
    pub async fn run_insert_hook(&self, new_memory_id: MemoryId) -> Result<usize> {
        if !self.on_insert.enabled {
            return Ok(0);
        }
        let Some(proposer) = &self.proposer else {
            return Ok(0);
        };

        let new_memory = self.storage.get_memory(new_memory_id).await?;
        let Some(embedding) = &new_memory.embedding else {
            tracing::debug!(
                "Insert hook: no embedding for {}, skipping kNN",
                new_memory_id
            );
            return Ok(0);
        };

        // kNN over existing memories. The trait `vector_search` returns full
        // MemoryNotes; it is config-gated on `enable_vector_search`, so if that
        // is disabled it returns no candidates and the hook degrades to a no-op.
        let candidates = self
            .storage
            .vector_search(embedding, self.on_insert.k, Some(new_memory.namespace.clone()))
            .await?
            .into_iter()
            .map(|result| result.memory)
            .filter(|note| note.id != new_memory_id)
            .take(self.on_insert.k)
            .collect::<Vec<_>>();

        if candidates.is_empty() {
            return Ok(0);
        }

        let proposed = proposer.propose(&new_memory, &candidates).await?;
        if proposed.is_empty() {
            return Ok(0);
        }

        // Persist: append the proposed links to the new memory's outgoing set and
        // write it. `update_memory` writes both directions and marks them
        // `user_created = false` (already set by the proposer), so link decay can
        // later prune weak auto-links.
        let num_proposed = proposed.len();
        let mut updated = new_memory.clone();
        updated.links.extend(proposed);
        self.storage.update_memory(&updated).await?;

        Ok(num_proposed)
    }

    /// Accessor exposing the storage backend (used by tests / adapters).
    pub fn storage(&self) -> &Arc<dyn StorageBackend> {
        &self.storage
    }
}
