//! Storage layer for Mnemosyne memory system
//!
//! Provides abstractions and implementations for persistent storage of memories,
//! embeddings, links, and audit logs.

pub mod libsql;
// Legacy vector storage using rusqlite + sqlite-vec.
// Gated behind the `legacy-vector-store` feature: this module bundles a second
// copy of SQLite that conflicts with libsql's (duplicate symbols, and the
// linker can silently resolve to the copy WITHOUT libsql vector functions).
// libsql has native vector32()/vector_distance_cos() support, which is what
// the rest of the storage layer uses.
#[cfg(feature = "legacy-vector-store")]
pub mod vectors;

#[cfg(test)]
pub mod test_utils;

#[cfg(test)]
pub mod libsql_workitem_tests;

use crate::agents::access_control::{ModificationLog, ModificationType};
use crate::agents::AgentRole;
use crate::error::Result;
use crate::types::{MemoryClass, MemoryId, MemoryNote, Namespace, SearchResult};
use async_trait::async_trait;

/// Storage backend trait defining all required operations
#[async_trait]
pub trait StorageBackend: Send + Sync {
    /// Store a new memory
    async fn store_memory(&self, memory: &MemoryNote) -> Result<()>;

    /// Retrieve a memory by ID
    async fn get_memory(&self, id: MemoryId) -> Result<MemoryNote>;

    /// Update an existing memory
    async fn update_memory(&self, memory: &MemoryNote) -> Result<()>;

    /// Archive a memory (soft delete)
    async fn archive_memory(&self, id: MemoryId) -> Result<()>;

    /// Vector similarity search
    async fn vector_search(
        &self,
        embedding: &[f32],
        limit: usize,
        namespace: Option<Namespace>,
    ) -> Result<Vec<SearchResult>>;

    /// Keyword search using FTS5
    async fn keyword_search(
        &self,
        query: &str,
        namespace: Option<Namespace>,
    ) -> Result<Vec<SearchResult>>;

    /// Graph traversal from seed memories
    async fn graph_traverse(
        &self,
        seed_ids: &[MemoryId],
        max_hops: usize,
        namespace: Option<Namespace>,
    ) -> Result<Vec<MemoryNote>>;

    /// Bounded graph traversal for agent-facing result surfaces.
    ///
    /// Backends can override this to push the bound into the query. The
    /// default preserves compatibility for alternate backends while ensuring
    /// callers never receive more than the requested number of memories.
    async fn graph_traverse_bounded(
        &self,
        seed_ids: &[MemoryId],
        max_hops: usize,
        namespace: Option<Namespace>,
        max_results: usize,
    ) -> Result<Vec<MemoryNote>> {
        let mut memories = self.graph_traverse(seed_ids, max_hops, namespace).await?;
        memories.truncate(max_results);
        Ok(memories)
    }

    /// Find consolidation candidates (similar memories)
    async fn find_consolidation_candidates(
        &self,
        namespace: Option<Namespace>,
    ) -> Result<Vec<(MemoryNote, MemoryNote)>>;

    /// Increment access counter
    async fn increment_access(&self, id: MemoryId) -> Result<()>;

    /// Get memory count by namespace
    async fn count_memories(&self, namespace: Option<Namespace>) -> Result<usize>;

    /// Hybrid search combining keyword matching, graph traversal, and the
    /// backend's available ranking signals. The MCP layer may add vector
    /// results separately when an embedding service is configured.
    async fn hybrid_search(
        &self,
        query: &str,
        namespace: Option<Namespace>,
        max_results: usize,
        expand_graph: bool,
    ) -> Result<Vec<SearchResult>>;

    /// Hybrid search restricted to one orthogonal memory class.
    ///
    /// The default implementation preserves compatibility for alternate
    /// backends. Backends with class-aware SQL can override it; fetching a
    /// bounded supersized candidate set prevents policies from crowding out
    /// factual results in the common implementation.
    async fn hybrid_search_by_class(
        &self,
        query: &str,
        namespace: Option<Namespace>,
        max_results: usize,
        expand_graph: bool,
        memory_class: MemoryClass,
    ) -> Result<Vec<SearchResult>> {
        let fetch_limit = max_results.saturating_mul(4).max(max_results);
        let mut results = self
            .hybrid_search(query, namespace, fetch_limit, expand_graph)
            .await?;
        results.retain(|result| result.memory.memory_class == memory_class);
        results.truncate(max_results);
        Ok(results)
    }

    /// Search global interaction guidance independently from factual recall.
    /// Alternate backends may return an empty list until policy storage is
    /// implemented; the default keeps the existing trait backwards compatible.
    async fn interaction_policy_search(
        &self,
        _query: &str,
        _max_results: usize,
    ) -> Result<Vec<SearchResult>> {
        Ok(Vec::new())
    }

    /// List approved project constraints for the bounded bootstrap path.
    /// Alternate backends can return an empty list until they support the
    /// durable constraint-proposal table.
    async fn list_approved_constraints(
        &self,
        _namespace: &Namespace,
        _limit: usize,
    ) -> Result<Vec<crate::storage::libsql::ConstraintProposalRecord>> {
        Ok(Vec::new())
    }

    /// List recent or important memories
    async fn list_memories(
        &self,
        namespace: Option<Namespace>,
        limit: usize,
        sort_by: MemorySortOrder,
    ) -> Result<Vec<MemoryNote>>;

    /// Store a modification log entry in the audit trail
    async fn store_modification_log(&self, log: &ModificationLog) -> Result<()>;

    /// Get the audit trail for a specific memory
    ///
    /// Returns modification logs ordered by timestamp descending (newest first)
    async fn get_audit_trail(&self, memory_id: MemoryId) -> Result<Vec<ModificationLog>>;

    /// Get modification statistics for an agent
    ///
    /// Returns counts of different modification types performed by the agent
    async fn get_modification_stats(
        &self,
        agent_role: AgentRole,
    ) -> Result<Vec<(ModificationType, u32)>>;

    // Work Item Persistence (for cross-session resilience)

    /// Store a work item
    async fn store_work_item(&self, item: &crate::orchestration::state::WorkItem) -> Result<()>;

    /// Load a work item by ID
    async fn load_work_item(
        &self,
        id: &crate::orchestration::state::WorkItemId,
    ) -> Result<crate::orchestration::state::WorkItem>;

    /// Update an existing work item
    async fn update_work_item(&self, item: &crate::orchestration::state::WorkItem) -> Result<()>;

    /// Load work items by state (for recovery)
    async fn load_work_items_by_state(
        &self,
        state: crate::orchestration::state::AgentState,
    ) -> Result<Vec<crate::orchestration::state::WorkItem>>;

    /// Load work items by multiple states in a single query (for bootstrap)
    ///
    /// This is more efficient than calling load_work_items_by_state multiple times
    /// since it uses a single database connection and query.
    async fn load_work_items_by_states(
        &self,
        states: &[crate::orchestration::state::AgentState],
    ) -> Result<Vec<crate::orchestration::state::WorkItem>>;

    /// Delete a work item (when permanently completed)
    async fn delete_work_item(&self, id: &crate::orchestration::state::WorkItemId) -> Result<()>;
}

/// Sort order for listing memories
#[derive(Debug, Clone, Copy)]
pub enum MemorySortOrder {
    Recent,
    Importance,
    AccessCount,
}
