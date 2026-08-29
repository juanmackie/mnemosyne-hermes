//! Core data types for the Mnemosyne memory system
//!
//! This module defines the fundamental data structures used throughout mnemosyne,
//! including memories, namespaces, links, and search queries. These types form the
//! foundation of the project-aware agentic memory system.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Unique identifier for memories
///
/// Wraps a UUID to provide type safety and prevent mixing memory IDs
/// with other UUID-based identifiers in the system.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct MemoryId(pub Uuid);

impl MemoryId {
    /// Create a new random memory ID
    pub fn new() -> Self {
        Self(Uuid::new_v4())
    }

    /// Parse a memory ID from a string
    pub fn from_string(s: &str) -> Result<Self, uuid::Error> {
        Ok(Self(Uuid::parse_str(s)?))
    }
}

impl Default for MemoryId {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Display for MemoryId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Namespace hierarchy: Global > Project > Session
///
/// Namespaces provide project-aware isolation while allowing global knowledge sharing.
/// Priority determines retrieval order (Session > Project > Global).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "type")]
pub enum Namespace {
    /// Global memories accessible across all projects
    Global,

    /// Project-scoped memories tied to a specific codebase
    Project {
        /// Project name (typically from git root directory name)
        name: String,
    },

    /// Session-scoped memories for temporary context
    Session {
        /// Parent project name
        project: String,

        /// Unique session identifier
        session_id: String,
    },

    /// Agent-scoped memories for personal AI agents
    /// Memories stored here are private to this agent instance
    Agent {
        /// Unique agent identifier (e.g. "hermes-local", "claude-code")
        agent_id: String,
    },
}

impl Namespace {
    /// Parse the canonical CLI/API namespace notation without silently
    /// converting malformed input to the global scope.
    pub fn parse(value: &str) -> crate::error::Result<Self> {
        if value == "global" {
            return Ok(Self::Global);
        }
        if let Some(agent_id) = value
            .strip_prefix("agent:")
            .or_else(|| value.strip_prefix("profile:"))
        {
            if !agent_id.is_empty() && !agent_id.contains(':') {
                return Ok(Self::Agent {
                    agent_id: agent_id.to_string(),
                });
            }
        }
        if let Some(project) = value.strip_prefix("project:") {
            if !project.is_empty() && !project.contains(':') {
                return Ok(Self::Project {
                    name: project.to_string(),
                });
            }
        }
        if let Some(rest) = value.strip_prefix("session:") {
            let parts: Vec<&str> = rest.split(':').collect();
            if parts.len() == 1 && !parts[0].is_empty() {
                return Ok(Self::Session {
                    project: parts[0].to_string(),
                    session_id: parts[0].to_string(),
                });
            }
            if parts.len() == 2 && parts.iter().all(|part| !part.is_empty()) {
                return Ok(Self::Session {
                    project: parts[0].to_string(),
                    session_id: parts[1].to_string(),
                });
            }
        }
        Err(crate::error::MnemosyneError::InvalidNamespace(
            value.to_string(),
        ))
    }

    /// Get namespace priority for retrieval ordering
    /// Higher priority = searched first (Agent is most specific)
    pub fn priority(&self) -> u8 {
        match self {
            Namespace::Agent { .. } => 4,
            Namespace::Session { .. } => 3,
            Namespace::Project { .. } => 2,
            Namespace::Global => 1,
        }
    }

    /// Check if this namespace is a session
    pub fn is_session(&self) -> bool {
        matches!(self, Namespace::Session { .. })
    }

    /// Check if this namespace is project-scoped or higher
    pub fn is_project_or_higher(&self) -> bool {
        !matches!(self, Namespace::Global)
    }

    /// Check if this namespace is agent-scoped
    pub fn is_agent(&self) -> bool {
        matches!(self, Namespace::Agent { .. })
    }

    /// Get the project name if applicable
    pub fn project_name(&self) -> Option<&str> {
        match self {
            Namespace::Project { name } => Some(name),
            Namespace::Session { project, .. } => Some(project),
            Namespace::Agent { agent_id } => Some(agent_id),
            Namespace::Global => None,
        }
    }
}

impl std::fmt::Display for Namespace {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Namespace::Global => write!(f, "global"),
            Namespace::Project { name } => write!(f, "project:{}", name),
            Namespace::Session {
                project,
                session_id,
            } => {
                write!(f, "session:{}:{}", project, session_id)
            }
            Namespace::Agent { agent_id } => write!(f, "agent:{}", agent_id),
        }
    }
}

/// Distinguishes factual knowledge from internal response guidance.
///
/// This is deliberately orthogonal to [`MemoryType`], so existing callers can
/// continue using their current classification while policy memories are kept
/// out of factual recall.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum MemoryClass {
    #[default]
    Knowledge,
    InteractionPolicy,
}

/// Where a memory's evidence originated.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvenanceSourceKind {
    Turn,
    Import,
    Manual,
}

/// Role that supplied a provenance quote.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvenanceSourceRole {
    User,
    Assistant,
    System,
    Unknown,
}

/// Typed provenance for an observed or derived memory.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryProvenance {
    pub source_kind: ProvenanceSourceKind,
    pub source_memory_id: Option<MemoryId>,
    pub session_id: Option<String>,
    pub turn_id: Option<String>,
    pub source_role: ProvenanceSourceRole,
    pub observed_at: DateTime<Utc>,
    pub evidence_quote: String,
    pub extractor_model: Option<String>,
    pub extraction_schema_version: Option<String>,
}

impl MemoryProvenance {
    /// Validate bounded metadata and ensure evidence is non-empty.
    pub fn validate(&self) -> crate::error::Result<()> {
        if self.evidence_quote.trim().is_empty() || self.evidence_quote.chars().count() > 2_000 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "provenance evidence_quote must contain 1..=2000 characters".into(),
            ));
        }
        for (name, value, max) in [
            ("session_id", self.session_id.as_deref(), 256),
            ("turn_id", self.turn_id.as_deref(), 256),
            ("extractor_model", self.extractor_model.as_deref(), 128),
            (
                "extraction_schema_version",
                self.extraction_schema_version.as_deref(),
                64,
            ),
        ] {
            if value.is_some_and(|v| v.chars().count() > max) {
                return Err(crate::error::MnemosyneError::ValidationError(format!(
                    "provenance {} exceeds {} characters",
                    name, max
                )));
            }
        }
        Ok(())
    }
}

/// An entity indexed for anchored retrieval.
///
/// `MemoryNote::related_entities` remains the compact public summary; this
/// typed representation preserves the display spelling, role, and confidence
/// in the indexed relation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryEntity {
    pub display_name: String,
    pub normalized_name: String,
    pub role: String,
    pub confidence: f32,
}

impl MemoryEntity {
    pub fn validate(&self) -> crate::error::Result<()> {
        if self.display_name.trim().is_empty() || self.display_name.chars().count() > 256 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "entity display_name must contain 1..=256 characters".into(),
            ));
        }
        if self.normalized_name.trim().is_empty() || self.normalized_name.chars().count() > 256 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "entity normalized_name must contain 1..=256 characters".into(),
            ));
        }
        if self.role.trim().is_empty() || self.role.chars().count() > 64 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "entity role must contain 1..=64 characters".into(),
            ));
        }
        if !self.confidence.is_finite() || !(0.0..=1.0).contains(&self.confidence) {
            return Err(crate::error::MnemosyneError::ValidationError(
                "entity confidence must be between 0 and 1".into(),
            ));
        }
        Ok(())
    }
}

/// Polarity of actionable response guidance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicyPolarity {
    Prefer,
    Avoid,
}

/// Explicit signal that justified an interaction policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PolicySignalKind {
    DirectPreference,
    Correction,
    Dissatisfaction,
    Approval,
}

/// Evidence-backed guidance about how the agent should respond.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InteractionPolicy {
    pub polarity: PolicyPolarity,
    pub guidance: String,
    pub applicability: String,
    pub signal: PolicySignalKind,
    pub confidence: f32,
    pub anchors: Vec<String>,
    pub evidence: Vec<MemoryProvenance>,
}

impl InteractionPolicy {
    pub fn validate(&self) -> crate::error::Result<()> {
        if self.guidance.trim().is_empty() || self.guidance.chars().count() > 1_000 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "interaction policy guidance must contain 1..=1000 characters".into(),
            ));
        }
        if self.applicability.chars().count() > 500 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "interaction policy applicability exceeds 500 characters".into(),
            ));
        }
        if !self.confidence.is_finite() || !(0.0..=1.0).contains(&self.confidence) {
            return Err(crate::error::MnemosyneError::ValidationError(
                "interaction policy confidence must be between 0 and 1".into(),
            ));
        }
        if self.evidence.is_empty() {
            return Err(crate::error::MnemosyneError::ValidationError(
                "interaction policy requires evidence".into(),
            ));
        }
        if self.anchors.len() > 16 {
            return Err(crate::error::MnemosyneError::ValidationError(
                "interaction policy has too many anchors".into(),
            ));
        }
        for anchor in &self.anchors {
            if anchor.trim().is_empty() || anchor.chars().count() > 256 {
                return Err(crate::error::MnemosyneError::ValidationError(
                    "interaction policy anchors must contain 1..=256 characters".into(),
                ));
            }
        }
        for evidence in &self.evidence {
            evidence.validate()?;
        }
        Ok(())
    }
}

/// Memory type classification for organizational and filtering purposes
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryType {
    /// Architectural decisions and system design choices
    ArchitectureDecision,

    /// Code patterns and implementation approaches
    CodePattern,

    /// Bug fixes and their solutions
    BugFix,

    /// Configuration settings and preferences
    Configuration,

    /// Constraints and requirements that must be satisfied
    Constraint,

    /// Domain entities and business concepts
    Entity,

    /// Insights and learnings
    Insight,

    /// References to external resources
    Reference,

    /// User preferences and settings
    Preference,

    /// Task or action item
    Task,

    /// Agent coordination events for orchestration
    AgentEvent,

    // Specification Workflow Types
    /// Project constitution defining principles and quality gates
    Constitution,

    /// Feature specification with user scenarios and requirements
    FeatureSpec,

    /// Implementation plan with architecture and technical design
    ImplementationPlan,

    /// Task breakdown with dependencies and parallelization markers
    TaskBreakdown,

    /// Quality checklist for validation and acceptance criteria
    QualityChecklist,

    /// Clarification resolving ambiguities in specs or requirements
    Clarification,
}

impl MemoryType {
    /// Get the type factor for importance calculations
    /// Different memory types have different base value
    pub fn type_factor(&self) -> f32 {
        match self {
            // Core architectural types (highest value)
            MemoryType::Constitution => 1.3, // Project principles
            MemoryType::ArchitectureDecision => 1.2,
            MemoryType::Constraint => 1.1,

            // Workflow and coordination types
            MemoryType::FeatureSpec => 1.1, // Feature requirements
            MemoryType::ImplementationPlan => 1.0, // Technical design
            MemoryType::AgentEvent => 1.0,  // Orchestration events
            MemoryType::CodePattern => 1.0,

            // Execution and validation types
            MemoryType::TaskBreakdown => 0.9,    // Task lists
            MemoryType::QualityChecklist => 0.9, // Validation criteria
            MemoryType::BugFix => 0.9,
            MemoryType::Insight => 0.9,
            MemoryType::Task => 0.9,

            // Reference and clarification types
            MemoryType::Clarification => 0.8, // Resolved ambiguities
            _ => 0.8,
        }
    }
}

/// Relationship types between memories for knowledge graph construction
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkType {
    /// B builds upon or extends A
    Extends,

    /// B builds upon A (alias for workflow relationships)
    BuildsUpon,

    /// B contradicts or invalidates A
    Contradicts,

    /// B implements the concept described in A
    Implements,

    /// B references or cites A
    References,

    /// B is referenced by A (inverse of References)
    ReferencedBy,

    /// B clarifies ambiguities in A
    Clarifies,

    /// B replaces or supersedes A
    Supersedes,
}

/// Memory link with typed relationship and metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryLink {
    /// Target memory ID
    pub target_id: MemoryId,

    /// Type of relationship
    pub link_type: LinkType,

    /// Link strength (0.0 - 1.0), evolves based on co-access patterns
    pub strength: f32,

    /// Human-readable explanation of the relationship
    pub reason: String,

    /// When the link was created
    pub created_at: DateTime<Utc>,

    /// When the link was last traversed (for decay tracking)
    pub last_traversed_at: Option<DateTime<Utc>>,

    /// Whether link was manually created by user (user links don't decay)
    pub user_created: bool,
}

/// Complete memory note structure with all metadata
///
/// This is the core data structure representing a single memory in the system.
/// It includes content, classification, relationships, and lifecycle information.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryNote {
    // === Identity ===
    /// Unique identifier
    pub id: MemoryId,

    /// Namespace (global, project, or session)
    pub namespace: Namespace,

    /// Creation timestamp
    pub created_at: DateTime<Utc>,

    /// Last update timestamp
    pub updated_at: DateTime<Utc>,

    // === Content (human-readable) ===
    /// Full memory content
    pub content: String,

    /// Concise 1-2 sentence summary (LLM-generated)
    pub summary: String,

    /// Key terms for keyword search (LLM-extracted)
    pub keywords: Vec<String>,

    /// Categorization tags (LLM-suggested + user-added)
    pub tags: Vec<String>,

    /// Context about when/why this is relevant (LLM-generated)
    pub context: String,

    // === Classification ===
    /// Memory type
    pub memory_type: MemoryType,

    /// Orthogonal class used to keep interaction guidance out of factual recall.
    #[serde(default)]
    pub memory_class: MemoryClass,

    /// Optional typed source/evidence metadata.
    #[serde(default)]
    pub provenance: Option<MemoryProvenance>,

    /// Importance level (1-10, higher = more important)
    pub importance: u8,

    /// Confidence in the information (0.0-1.0)
    pub confidence: f32,

    // === Relationships ===
    /// Semantic links to other memories
    pub links: Vec<MemoryLink>,

    /// Related file paths in the codebase
    pub related_files: Vec<String>,

    /// Related entities (components, services, etc.)
    pub related_entities: Vec<String>,

    // === Lifecycle ===
    /// Number of times this memory has been accessed
    pub access_count: u32,

    /// Last access timestamp
    pub last_accessed_at: DateTime<Utc>,

    /// Optional expiration timestamp
    pub expires_at: Option<DateTime<Utc>>,

    /// Whether this memory has been archived
    pub is_archived: bool,

    /// If superseded, the ID of the superseding memory
    pub superseded_by: Option<MemoryId>,

    // === Computational ===
    /// Embedding vector (not serialized to JSON, stored separately)
    #[serde(skip)]
    pub embedding: Option<Vec<f32>>,

    /// Model used to generate the embedding
    pub embedding_model: String,
}

impl MemoryNote {
    /// Calculate decayed importance based on age, access patterns, and type
    ///
    /// This implements the FEEDBACK phase of the OODA loop, adjusting memory
    /// importance over time based on usage patterns.
    pub fn decayed_importance(&self) -> f32 {
        let base = self.importance as f32;
        let recency_factor = self.recency_factor();
        let type_factor = self.memory_type.type_factor();
        let access_bonus = (self.access_count as f32).ln().max(0.0) * 0.1;

        base * recency_factor * type_factor * (1.0 + access_bonus)
    }

    /// Calculate recency factor (exponential decay with 6-month half-life)
    fn recency_factor(&self) -> f32 {
        let age_days = (Utc::now() - self.updated_at).num_days() as f32;
        (-age_days / 180.0).exp() // Half-life of 6 months
    }

    /// Check if this memory should be archived
    pub fn should_archive(&self, threshold_days: u32, min_importance: f32) -> bool {
        let age_days = (Utc::now() - self.updated_at).num_days() as u32;
        age_days > threshold_days && self.decayed_importance() < min_importance
    }
}

/// Search query with filters for memory retrieval
///
/// Supports the OBSERVE and ORIENT phases of the OODA loop by enabling
/// targeted memory recall with multiple filter dimensions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchQuery {
    /// Search query string (semantic or keyword)
    pub query: String,

    /// Optional namespace filter
    pub namespace: Option<Namespace>,

    /// Filter by memory types
    pub memory_types: Vec<MemoryType>,

    /// Optional orthogonal class filter.
    #[serde(default)]
    pub memory_class: Option<MemoryClass>,

    /// Filter by tags
    pub tags: Vec<String>,

    /// Minimum importance threshold
    pub min_importance: Option<u8>,

    /// Maximum number of results to return
    pub max_results: usize,

    /// Whether to include archived memories
    pub include_archived: bool,
}

impl Default for SearchQuery {
    fn default() -> Self {
        Self {
            query: String::new(),
            namespace: None,
            memory_types: Vec::new(),
            memory_class: None,
            tags: Vec::new(),
            min_importance: None,
            max_results: 10,
            include_archived: false,
        }
    }
}

/// Search result with relevance score and match explanation
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    /// The memory that matched
    pub memory: MemoryNote,

    /// Relevance score (0.0 - 1.0, higher = more relevant)
    pub score: f32,

    /// Explanation of why this memory matched
    pub match_reason: String,
}

/// Updates to apply to an existing memory
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct MemoryUpdates {
    /// New content (triggers re-embedding)
    pub content: Option<String>,

    /// New importance level
    pub importance: Option<u8>,

    /// New tags (replaces existing)
    pub tags: Option<Vec<String>>,

    /// Additional tags (appends to existing)
    pub add_tags: Option<Vec<String>>,
}

/// Consolidation decision from LLM analysis
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "decision")]
pub enum ConsolidationDecision {
    /// Merge two memories into one
    Merge {
        /// Which memory ID to keep
        into: MemoryId,

        /// Merged content
        content: String,
    },

    /// One memory supersedes the other
    Supersede {
        /// Memory to keep
        kept: MemoryId,

        /// Memory to archive
        superseded: MemoryId,
    },

    /// Keep both memories (they're distinct)
    KeepBoth,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_memory_id_creation() {
        let id1 = MemoryId::new();
        let id2 = MemoryId::new();
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_namespace_priority() {
        let global = Namespace::Global;
        let project = Namespace::Project {
            name: "test".to_string(),
        };
        let session = Namespace::Session {
            project: "test".to_string(),
            session_id: "abc123".to_string(),
        };

        assert_eq!(global.priority(), 1);
        assert_eq!(project.priority(), 2);
        assert_eq!(session.priority(), 3);
    }

    #[test]
    fn test_memory_type_factors() {
        assert!(MemoryType::ArchitectureDecision.type_factor() > 1.0);
        assert!(MemoryType::Constraint.type_factor() > 1.0);
        assert_eq!(MemoryType::CodePattern.type_factor(), 1.0);
    }

    #[test]
    fn test_decayed_importance() {
        let mut memory = MemoryNote {
            id: MemoryId::new(),
            namespace: Namespace::Global,
            created_at: Utc::now(),
            updated_at: Utc::now(),
            content: "test".to_string(),
            summary: "test".to_string(),
            keywords: vec![],
            tags: vec![],
            context: "test".to_string(),
            memory_type: MemoryType::CodePattern,
            importance: 8,
            confidence: 0.9,
            links: vec![],
            related_files: vec![],
            related_entities: vec![],
            access_count: 0,
            last_accessed_at: Utc::now(),
            expires_at: None,
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: "test".to_string(),
            memory_class: crate::types::MemoryClass::Knowledge,
            provenance: None,
        };

        // Fresh memory should have high importance
        let fresh_importance = memory.decayed_importance();
        assert!(fresh_importance >= 7.0);

        // Accessed memory should have bonus
        memory.access_count = 50;
        let accessed_importance = memory.decayed_importance();
        assert!(accessed_importance > fresh_importance);
    }
}
