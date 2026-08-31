//! LibSQL storage backend implementation
//!
//! Provides persistent storage using Turso/libSQL with native vector search,
//! FTS5 for keyword search, and efficient indexing for graph traversal.

use crate::embeddings::EmbeddingService;
use crate::error::{MnemosyneError, Result};
use crate::reasoning::{
    ReasoningExperience, ReasoningLessonKind, ReasoningMemory, ReasoningSearchHit, TaskOutcome,
};
use crate::storage::StorageBackend;
use crate::types::{
    MemoryClass, MemoryEntity, MemoryId, MemoryLink, MemoryNote, Namespace, SearchResult,
};
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use libsql::{params, Builder, Connection, Database};
use once_cell::sync::Lazy;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::Arc;
use tracing::{debug, info, warn};
use uuid::Uuid;

const MAX_GRAPH_SEEDS: usize = 1_000;
const NEAR_DUPLICATE_THRESHOLD: f32 = 0.92;
const INTEGRITY_SCAN_LIMIT: usize = 10_000;

/// A normalized structured fact used by the integrity gate. Facts sharing a
/// subject and predicate are mutually exclusive when their objects differ.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct StructuredFact {
    pub memory_id: MemoryId,
    pub subject: String,
    pub predicate: String,
    pub object: String,
    pub confidence: f32,
    pub observed_at: DateTime<Utc>,
}

/// Exact projection counts produced by bounded orphan repair.
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize)]
pub struct OrphanRepairReport {
    pub embeddings_removed: u64,
    pub vector_rows_removed: u64,
    pub graph_links_removed: u64,
    pub fts_rows_removed: u64,
    pub provenance_rows_removed: u64,
    pub provenance_sources_cleared: u64,
    pub entity_rows_removed: u64,
    pub policy_rows_removed: u64,
    pub policy_evidence_rows_removed: u64,
    pub fact_rows_removed: u64,
}

fn canonical_content(content: &str) -> String {
    content
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn content_hash(content: &str) -> String {
    format!(
        "{:x}",
        Sha256::digest(canonical_content(content).as_bytes())
    )
}

fn term_fingerprint(term: &str) -> String {
    format!("{:x}", Sha256::digest(term.as_bytes()))
}

fn lexical_similarity(left: &str, right: &str) -> f32 {
    let left_content = canonical_content(left);
    let right_content = canonical_content(right);
    let left: HashSet<_> = left_content.split_whitespace().collect();
    let right: HashSet<_> = right_content.split_whitespace().collect();
    if left.is_empty() || right.is_empty() {
        return 0.0;
    }
    left.intersection(&right).count() as f32 / left.union(&right).count() as f32
}

fn cosine_similarity(left: &[f32], right: &[f32]) -> Option<f32> {
    if left.len() != right.len() || left.is_empty() {
        return None;
    }
    // Constant vectors are common test/fallback placeholders and have no
    // discriminating power even though their cosine is 1.0.
    let left_range = left
        .iter()
        .fold((f32::INFINITY, f32::NEG_INFINITY), |(min, max), value| {
            (min.min(*value), max.max(*value))
        });
    let right_range = right
        .iter()
        .fold((f32::INFINITY, f32::NEG_INFINITY), |(min, max), value| {
            (min.min(*value), max.max(*value))
        });
    if left_range.1 - left_range.0 < f32::EPSILON || right_range.1 - right_range.0 < f32::EPSILON {
        return None;
    }
    let (dot, left_norm, right_norm) = left
        .iter()
        .zip(right)
        .fold((0.0, 0.0, 0.0), |(dot, ln, rn), (a, b)| {
            (dot + a * b, ln + a * a, rn + b * b)
        });
    if left_norm == 0.0 || right_norm == 0.0 {
        None
    } else {
        Some((dot / (left_norm.sqrt() * right_norm.sqrt())).clamp(-1.0, 1.0))
    }
}

fn extracted_entities(content: &str, related: &[String]) -> Vec<MemoryEntity> {
    let mut names = related.to_vec();
    // Deterministic fallback: preserve technical/proper-name tokens without a
    // model call. It is intentionally bounded so arbitrary imports cannot
    // create an unbounded entity projection.
    for token in content.split(|c: char| !c.is_alphanumeric() && !"_:/.-".contains(c)) {
        let token = token.trim_matches('.');
        if token.chars().count() >= 2
            && (token.chars().any(|c| c.is_uppercase()) || token.contains(['_', '/', ':']))
        {
            names.push(token.to_string());
        }
        if names.len() >= 32 {
            break;
        }
    }
    let mut seen = HashSet::new();
    let mut entities = names
        .into_iter()
        .filter_map(|display_name| {
            let normalized_name = normalize_entity_name(&display_name);
            if normalized_name.is_empty() || !seen.insert(normalized_name.clone()) {
                return None;
            }
            Some(MemoryEntity {
                display_name,
                normalized_name,
                role: "related".into(),
                confidence: 1.0,
            })
        })
        .collect::<Vec<_>>();
    if entities.is_empty() {
        if let Some(token) = canonical_content(content).split_whitespace().next() {
            entities.push(MemoryEntity {
                display_name: token.to_string(),
                normalized_name: token.to_string(),
                role: "topic".into(),
                confidence: 0.5,
            });
        }
    }
    entities
}

fn parse_structured_fact(memory: &MemoryNote) -> Option<StructuredFact> {
    // The triples MCP tool stores explicit slots as tags; accept those as the
    // canonical structured representation as well as the human-readable pipe
    // form used by imports and tests.
    let tagged = |prefix: &str| {
        memory
            .tags
            .iter()
            .find_map(|tag| tag.strip_prefix(prefix).map(str::to_owned))
    };
    let (subject, predicate, object) = match (
        tagged("triple-subject:"),
        tagged("triple-predicate:"),
        tagged("triple-object:"),
    ) {
        (Some(subject), Some(predicate), Some(object)) => (subject, predicate, object),
        _ => {
            let line = memory
                .content
                .lines()
                .find(|line| line.split('|').count() == 3)?;
            let mut parts = line.split('|').map(str::trim);
            (
                parts.next()?.to_owned(),
                parts.next()?.to_owned(),
                parts.next()?.to_owned(),
            )
        }
    };
    if subject.trim().is_empty() || predicate.trim().is_empty() || object.trim().is_empty() {
        return None;
    }
    Some(StructuredFact {
        memory_id: memory.id,
        subject: subject.trim().to_lowercase(),
        predicate: predicate.trim().to_lowercase(),
        object: object.trim().to_string(),
        confidence: memory.confidence,
        observed_at: memory.updated_at,
    })
}

fn content_revision(content: &str, updated_at: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(updated_at.as_bytes());
    digest.update([0]);
    digest.update(content.as_bytes());
    format!("{:x}", digest.finalize())
}

fn normalize_entity_name(name: &str) -> String {
    name.trim()
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Match an anchor on token boundaries, including multi-word phrases.
///
/// Substring matching makes a short anchor such as `go` match unrelated text
/// such as `golf`. Entity and policy anchors are names, not arbitrary search
/// substrings, so compare normalized token windows instead.
fn decode_embedding_from_row(row: &libsql::Row, index: i32) -> Result<Vec<f32>> {
    match row.column_type(index).unwrap_or(libsql::ValueType::Blob) {
        libsql::ValueType::Blob => {
            let bytes: Vec<u8> = row.get(index)?;
            if bytes.len() % 4 != 0 {
                return Err(MnemosyneError::Database(
                    "embedding blob length is not divisible by four".into(),
                ));
            }
            Ok(bytes
                .chunks_exact(4)
                .map(|chunk| f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]))
                .collect())
        }
        libsql::ValueType::Text => Ok(serde_json::from_str(&row.get::<String>(index)?)?),
        _ => Err(MnemosyneError::Database(
            "embedding column has an unsupported type".into(),
        )),
    }
}

async fn mark_proposal_failed(
    tx: &libsql::Transaction,
    proposal_id: &str,
    target_memory_id: &str,
    error_message: &str,
) -> Result<()> {
    let now = Utc::now().to_rfc3339();
    tx.execute(
        "UPDATE memory_change_proposals SET status = 'failed', error_message = ?, applied_at = ? WHERE id = ? AND status = 'accepted'",
        params![error_message, now, proposal_id],
    )
    .await?;
    tx.execute(
        "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
        params![
            target_memory_id,
            serde_json::json!({
                "event": "memory_proposal_failed",
                "proposal_id": proposal_id,
                "reason": error_message,
            })
            .to_string(),
        ],
    )
    .await?;
    Ok(())
}

async fn mark_policy_proposal_failed(
    tx: &libsql::Transaction,
    proposal_id: &str,
    source_memory_id: &str,
    error_message: &str,
) -> Result<()> {
    let now = Utc::now().to_rfc3339();
    tx.execute(
        "UPDATE interaction_policy_proposals SET status = 'failed', error_message = ?, applied_at = ? WHERE id = ? AND status = 'accepted'",
        params![error_message, now, proposal_id],
    )
    .await?;
    tx.execute(
        "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
        params![
            source_memory_id,
            serde_json::json!({
                "event": "interaction_policy_proposal_failed",
                "proposal_id": proposal_id,
                "reason": error_message,
            })
            .to_string(),
        ],
    )
    .await?;
    Ok(())
}

async fn connection_has_column(conn: &Connection, table: &str, column: &str) -> Result<bool> {
    let sql = format!("PRAGMA table_info({table})");
    let mut rows = conn.query(&sql, params![]).await?;
    while let Some(row) = rows.next().await? {
        if row.get::<String>(1)? == column {
            return Ok(true);
        }
    }
    Ok(false)
}

async fn scalar_count(conn: &Connection, sql: &str) -> Result<u64> {
    let mut rows = conn.query(sql, params![]).await?;
    Ok(rows
        .next()
        .await?
        .map(|row| row.get::<i64>(0).unwrap_or(0).max(0) as u64)
        .unwrap_or(0))
}

fn parse_datetime_from_row(row: &libsql::Row, index: i32) -> Option<chrono::DateTime<Utc>> {
    match row.column_type(index).ok()? {
        libsql::ValueType::Integer => chrono::DateTime::from_timestamp(row.get(index).ok()?, 0),
        libsql::ValueType::Real => {
            chrono::DateTime::from_timestamp(row.get::<f64>(index).ok()? as i64, 0)
        }
        libsql::ValueType::Text => {
            chrono::DateTime::parse_from_rfc3339(&row.get::<String>(index).ok()?)
                .ok()
                .map(|value| value.with_timezone(&Utc))
        }
        _ => None,
    }
}

fn query_contains_entity_phrase(query: &str, anchor: &str) -> bool {
    let query_words: Vec<String> = query
        .split(|c: char| !c.is_alphanumeric() && c != '_' && c != '-')
        .filter(|word| !word.is_empty())
        .map(|word| word.to_lowercase())
        .collect();
    let anchor_words: Vec<String> = normalize_entity_name(anchor)
        .split_whitespace()
        .map(str::to_owned)
        .collect();
    !anchor_words.is_empty()
        && anchor_words.len() <= query_words.len()
        && query_words
            .windows(anchor_words.len())
            .any(|window| window == anchor_words.as_slice())
}

/// Compute a bounded search-time freshness signal.
///
/// Creation freshness preserves the existing behavior for newly learned facts;
/// access freshness adds a small reinforcement for memories that are still
/// being used. This is deliberately a ranking bias, not expiry or archival.
fn bounded_recency_score(
    now: chrono::DateTime<Utc>,
    created_at: chrono::DateTime<Utc>,
    last_accessed_at: chrono::DateTime<Utc>,
) -> f32 {
    const HALF_LIFE_DAYS: f32 = 30.0;
    let age_days = now.signed_duration_since(created_at).num_seconds().max(0) as f32 / 86_400.0;
    let idle_days = now
        .signed_duration_since(last_accessed_at)
        .num_seconds()
        .max(0) as f32
        / 86_400.0;
    let creation_freshness = (-age_days / HALF_LIFE_DAYS).exp();
    let access_freshness = (-idle_days / HALF_LIFE_DAYS).exp();
    (0.75 * creation_freshness + 0.25 * access_freshness).clamp(0.0, 1.0)
}

// ---------------------------------------------------------------------------
// Migration SQL embedded at compile time via include_str!
// Eliminates runtime file I/O during database initialization — critical for
// test performance where 30+ tests each create a fresh LibsqlStorage.
// ---------------------------------------------------------------------------

/// Migration file names for LibSQL schema
static LIBSQL_MIGRATION_NAMES: &[&str] = &[
    "001_initial_schema.sql",
    "002_add_indexes.sql",
    "003_audit_trail.sql",
    "011_work_items.sql",
    "012_requirement_tracking.sql",
    "015_version_check_cache.sql",
    "017_text_memory_learning.sql",
    "019_memory_maintenance_runs.sql",
    "020_memory_change_proposals.sql",
    "022_interaction_policy_proposals.sql",
    "023_reasoning_experiences.sql",
    "024_constraint_proposals.sql",
    "025_session_transcripts.sql",
    "026_retrieval_evaluation.sql",
    "027_memory_integrity.sql",
    "028_retrieval_trace_namespace.sql",
];

/// Migration file names for StandardSQLite schema
static SQLITE_MIGRATION_NAMES: &[&str] = &[
    "001_initial_schema.sql",
    "002_add_indexes.sql",
    "003_fix_fts_triggers.sql",
    "011_work_items.sql",
    "012_requirement_tracking.sql",
    "013_add_task_and_agent_event_types.sql",
    "014_add_specification_workflow_types.sql",
    "015_fix_audit_log_schema.sql",
    "016_version_check_cache.sql",
    "017_text_memory_learning.sql",
    "019_memory_maintenance_runs.sql",
    "020_memory_change_proposals.sql",
    "021_expand_memory_type_constraint.sql",
    "022_interaction_policy_proposals.sql",
    "023_reasoning_experiences.sql",
    "024_constraint_proposals.sql",
    "025_session_transcripts.sql",
    "026_retrieval_evaluation.sql",
    "027_memory_integrity.sql",
    "028_retrieval_trace_namespace.sql",
];

/// (filename, SQL content) pairs for LibSQL migrations — SQL embedded at
/// compile time so no file I/O is needed at runtime.
static LIBSQL_MIGRATIONS: &[(&str, &str)] = &[
    (
        "001_initial_schema.sql",
        include_str!("../../migrations/libsql/001_initial_schema.sql"),
    ),
    (
        "002_add_indexes.sql",
        include_str!("../../migrations/libsql/002_add_indexes.sql"),
    ),
    (
        "003_audit_trail.sql",
        include_str!("../../migrations/libsql/003_audit_trail.sql"),
    ),
    (
        "011_work_items.sql",
        include_str!("../../migrations/libsql/011_work_items.sql"),
    ),
    (
        "012_requirement_tracking.sql",
        include_str!("../../migrations/libsql/012_requirement_tracking.sql"),
    ),
    (
        "015_version_check_cache.sql",
        include_str!("../../migrations/libsql/015_version_check_cache.sql"),
    ),
    (
        "017_text_memory_learning.sql",
        include_str!("../../migrations/libsql/017_text_memory_learning.sql"),
    ),
    (
        "019_memory_maintenance_runs.sql",
        include_str!("../../migrations/libsql/019_memory_maintenance_runs.sql"),
    ),
    (
        "020_memory_change_proposals.sql",
        include_str!("../../migrations/libsql/020_memory_change_proposals.sql"),
    ),
    (
        "022_interaction_policy_proposals.sql",
        include_str!("../../migrations/libsql/022_interaction_policy_proposals.sql"),
    ),
    (
        "023_reasoning_experiences.sql",
        include_str!("../../migrations/libsql/023_reasoning_experiences.sql"),
    ),
    (
        "024_constraint_proposals.sql",
        include_str!("../../migrations/libsql/024_constraint_proposals.sql"),
    ),
    (
        "025_session_transcripts.sql",
        include_str!("../../migrations/libsql/025_session_transcripts.sql"),
    ),
    (
        "026_retrieval_evaluation.sql",
        include_str!("../../migrations/libsql/026_retrieval_evaluation.sql"),
    ),
    (
        "027_memory_integrity.sql",
        include_str!("../../migrations/libsql/027_memory_integrity.sql"),
    ),
    (
        "028_retrieval_trace_namespace.sql",
        include_str!("../../migrations/libsql/028_retrieval_trace_namespace.sql"),
    ),
];

/// (filename, SQL content) pairs for StandardSQLite migrations
static SQLITE_MIGRATIONS: &[(&str, &str)] = &[
    (
        "001_initial_schema.sql",
        include_str!("../../migrations/sqlite/001_initial_schema.sql"),
    ),
    (
        "002_add_indexes.sql",
        include_str!("../../migrations/sqlite/002_add_indexes.sql"),
    ),
    (
        "003_fix_fts_triggers.sql",
        include_str!("../../migrations/sqlite/003_fix_fts_triggers.sql"),
    ),
    (
        "011_work_items.sql",
        include_str!("../../migrations/sqlite/011_work_items.sql"),
    ),
    (
        "012_requirement_tracking.sql",
        include_str!("../../migrations/sqlite/012_requirement_tracking.sql"),
    ),
    (
        "013_add_task_and_agent_event_types.sql",
        include_str!("../../migrations/sqlite/013_add_task_and_agent_event_types.sql"),
    ),
    (
        "014_add_specification_workflow_types.sql",
        include_str!("../../migrations/sqlite/014_add_specification_workflow_types.sql"),
    ),
    (
        "015_fix_audit_log_schema.sql",
        include_str!("../../migrations/sqlite/015_fix_audit_log_schema.sql"),
    ),
    (
        "016_version_check_cache.sql",
        include_str!("../../migrations/sqlite/016_version_check_cache.sql"),
    ),
    (
        "017_text_memory_learning.sql",
        include_str!("../../migrations/sqlite/017_text_memory_learning.sql"),
    ),
    (
        "019_memory_maintenance_runs.sql",
        include_str!("../../migrations/sqlite/019_memory_maintenance_runs.sql"),
    ),
    (
        "020_memory_change_proposals.sql",
        include_str!("../../migrations/sqlite/020_memory_change_proposals.sql"),
    ),
    (
        "021_expand_memory_type_constraint.sql",
        include_str!("../../migrations/sqlite/021_expand_memory_type_constraint.sql"),
    ),
    (
        "022_interaction_policy_proposals.sql",
        include_str!("../../migrations/sqlite/022_interaction_policy_proposals.sql"),
    ),
    (
        "023_reasoning_experiences.sql",
        include_str!("../../migrations/sqlite/023_reasoning_experiences.sql"),
    ),
    (
        "024_constraint_proposals.sql",
        include_str!("../../migrations/sqlite/024_constraint_proposals.sql"),
    ),
    (
        "025_session_transcripts.sql",
        include_str!("../../migrations/sqlite/025_session_transcripts.sql"),
    ),
    (
        "026_retrieval_evaluation.sql",
        include_str!("../../migrations/sqlite/026_retrieval_evaluation.sql"),
    ),
    (
        "027_memory_integrity.sql",
        include_str!("../../migrations/sqlite/027_memory_integrity.sql"),
    ),
    (
        "028_retrieval_trace_namespace.sql",
        include_str!("../../migrations/sqlite/028_retrieval_trace_namespace.sql"),
    ),
];

/// Pre-parsed and pre-batched migration SQL for fresh databases.
/// Includes: CREATE TABLE IF NOT EXISTS _migrations_applied + all migration SQL
/// + INSERT records. Computed once on first access via Lazy, then reused for
/// every subsequent fresh database creation (including in tests).
static LIBSQL_FRESH_SQL: Lazy<String> = Lazy::new(|| {
    let now = Utc::now().timestamp();
    let mut parts: Vec<String> = Vec::new();
    // Create migrations tracking table
    parts.push("CREATE TABLE IF NOT EXISTS _migrations_applied (migration_name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)".to_string());
    // Add all migration SQL
    for (_, sql) in LIBSQL_MIGRATIONS {
        let statements = parse_sql_statements(sql);
        for stmt in statements {
            let trimmed = stmt.trim();
            if !trimmed.is_empty() {
                parts.push(trimmed.to_string());
            }
        }
    }
    // Record all migrations as applied
    for (name, _) in LIBSQL_MIGRATIONS {
        parts.push(format!(
            "INSERT INTO _migrations_applied (migration_name, applied_at) VALUES ('{}', {})",
            name, now
        ));
    }
    parts.join(";\n")
});

/// Pre-parsed and pre-batched migration SQL for fresh StandardSQLite databases.
static SQLITE_FRESH_SQL: Lazy<String> = Lazy::new(|| {
    let now = Utc::now().timestamp();
    let mut parts: Vec<String> = Vec::new();
    // Create migrations tracking table
    parts.push("CREATE TABLE IF NOT EXISTS _migrations_applied (migration_name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)".to_string());
    // Add all migration SQL
    for (_, sql) in SQLITE_MIGRATIONS {
        let statements = parse_sql_statements(sql);
        for stmt in statements {
            let trimmed = stmt.trim();
            if !trimmed.is_empty() {
                parts.push(trimmed.to_string());
            }
        }
    }
    // Record all migrations as applied
    for (name, _) in SQLITE_MIGRATIONS {
        parts.push(format!(
            "INSERT INTO _migrations_applied (migration_name, applied_at) VALUES ('{}', {})",
            name, now
        ));
    }
    parts.join(";\n")
});

/// PRAGMA-prefixed fresh migration SQL for in-memory databases.
/// Merges speed-optimized PRAGMAs into the migration batch so a single
/// execute_batch call sets PRAGMAs AND creates the schema, eliminating
/// a separate connection + execute_batch round-trip per in-memory DB creation.
static LIBSQL_FRESH_SQL_MEM: Lazy<String> = Lazy::new(|| {
    format!(
        "PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-65536; {}",
        &*LIBSQL_FRESH_SQL
    )
});

/// Same as above for StandardSQLite schema.
static SQLITE_FRESH_SQL_MEM: Lazy<String> = Lazy::new(|| {
    format!(
        "PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-65536; {}",
        &*SQLITE_FRESH_SQL
    )
});

/// Serialize schema upgrades within a process. This is especially important
/// for the integrity migration, which rebuilds one legacy CHECK-constrained
/// table and cannot safely be run concurrently by two agent instances.
static MIGRATION_LOCK: Lazy<tokio::sync::Mutex<()>> =
    Lazy::new(|| tokio::sync::Mutex::const_new(()));

/// Look up pre-compiled migration SQL by file name
fn lookup_migration_sql(schema_type: SchemaType, file_name: &str) -> &'static str {
    let migrations = match schema_type {
        SchemaType::LibSQL => LIBSQL_MIGRATIONS,
        SchemaType::StandardSQLite => SQLITE_MIGRATIONS,
    };
    migrations
        .iter()
        .find(|(name, _)| *name == file_name)
        .map(|(_, sql)| *sql)
        .unwrap_or("")
}

/// Parse SQL file into individual statements, handling multi-line constructs like triggers
fn parse_sql_statements(sql: &str) -> Vec<String> {
    let mut statements = Vec::new();
    let mut current = String::new();
    let mut depth: i32 = 0; // Track BEGIN/END nesting depth

    for line in sql.lines() {
        let trimmed = line.trim();

        // Skip comment-only and empty lines when not building a statement
        if current.is_empty() && (trimmed.is_empty() || trimmed.starts_with("--")) {
            continue;
        }

        // Add line to current statement
        if !current.is_empty() {
            current.push('\n');
        }
        current.push_str(line);

        // Track BEGIN/END depth for triggers
        let upper = trimmed.to_uppercase();
        if upper.starts_with("BEGIN") || upper.contains(" BEGIN") {
            depth += 1;
        }
        if upper.starts_with("END") {
            depth = depth.saturating_sub(1);
        }

        // Statement is complete when we hit ; and depth is 0
        if trimmed.ends_with(';') && depth == 0 {
            statements.push(current.clone());
            current.clear();
        }
    }

    // Add any remaining statement
    if !current.trim().is_empty() {
        statements.push(current);
    }

    statements
}

/// Database schema type
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum SchemaType {
    /// Standard SQLite (embeddings in separate table)
    StandardSQLite,
    /// LibSQL/Turso (embeddings as F32_BLOB in memories table)
    LibSQL,
}

/// Report of what was removed by a true-delete purge ([`LibsqlStorage::purge_memory`]).
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize)]
pub struct PurgeReport {
    /// The memory that was purged
    pub memory_id: String,
    /// Number of links (both directions) removed from the graph
    pub links_removed: u64,
    /// Whether a vector embedding row was removed from `memory_vectors`
    pub embedding_removed: bool,
    /// Whether the FTS index entry was removed (implicit via row delete trigger)
    pub fts_removed: bool,
    /// Number of audit-trail rows removed for this memory
    pub audit_rows_removed: u64,
    /// Number of `superseded_by` back-references cleared on other memories
    pub supersession_refs_cleared: u64,
}

/// LibSQL storage backend
pub struct LibsqlStorage {
    db: Database,
    embedding_service: Option<Arc<dyn EmbeddingService>>,
    search_config: crate::config::SearchConfig,
    schema_type: SchemaType,
    db_path: String,
    temporary_path: Option<std::path::PathBuf>,
}

impl Drop for LibsqlStorage {
    fn drop(&mut self) {
        if let Some(path) = &self.temporary_path {
            let _ = std::fs::remove_file(path);
        }
    }
}

/// Shared in-memory storage cache for test builds.
///
/// Many evolution/evaluation tests create a LibsqlStorage solely to construct
/// job structs (e.g., ArchivalJob::new(storage)) whose pure-computation methods
/// (should_archive, calculate_importance, etc.) never access the storage.
/// Creating 24+ separate in-memory DBs adds ~10ms each.
/// This caches a single in-memory DB; pure-computation tests reuse it via
/// `shared_test_storage()`. Tests that write data keep their own isolated DBs.
#[cfg(test)]
use std::sync::OnceLock;

#[cfg(test)]
static SHARED_TEST_STORAGE: OnceLock<Arc<LibsqlStorage>> = OnceLock::new();

/// Database connection mode
#[derive(Debug, Clone)]
pub enum ConnectionMode {
    /// Local file-based database
    Local(String),
    /// Local file-based database (read-only mode)
    ///
    /// Used when database file has read-only permissions.
    /// Automatically switches to journal_mode=DELETE instead of WAL
    /// since WAL requires write access to -wal and -shm files.
    LocalReadOnly(String),
    /// In-memory database (for testing)
    InMemory,
    /// Remote database (Turso Cloud)
    Remote { url: String, token: String },
    /// Embedded replica with sync
    EmbeddedReplica {
        path: String,
        url: String,
        token: String,
    },
}

impl LibsqlStorage {
    /// Return a stable, schema-aware memory projection.
    ///
    /// The migration that adds `memory_class` is intentionally additive, so
    /// relying on `SELECT *` would make row decoding depend on the physical
    /// column order of each schema family. All decoding paths use this
    /// projection, with `memory_class` in a stable position.
    fn memory_columns(&self, alias: &str) -> String {
        let prefix = if alias.is_empty() {
            String::new()
        } else {
            format!("{}.", alias)
        };
        let mut columns = vec![
            format!("{}id", prefix),
            format!("{}namespace", prefix),
            format!("{}created_at", prefix),
            format!("{}updated_at", prefix),
            format!("{}content", prefix),
            format!("{}summary", prefix),
            format!("{}keywords", prefix),
            format!("{}tags", prefix),
            format!("{}context", prefix),
            format!("{}memory_type", prefix),
            format!("{}memory_class", prefix),
            format!("{}importance", prefix),
            format!("{}confidence", prefix),
            format!("{}related_files", prefix),
            format!("{}related_entities", prefix),
            format!("{}access_count", prefix),
            format!("{}last_accessed_at", prefix),
            format!("{}expires_at", prefix),
            format!("{}is_archived", prefix),
            format!("{}superseded_by", prefix),
            format!("{}embedding_model", prefix),
        ];
        if self.schema_type == SchemaType::LibSQL {
            columns.push(format!("{}embedding", prefix));
        }
        columns.join(", ")
    }

    fn memory_column_count(&self) -> i32 {
        if self.schema_type == SchemaType::LibSQL {
            22
        } else {
            21
        }
    }

    fn knowledge_predicate(&self, alias: &str) -> String {
        let prefix = if alias.is_empty() {
            String::new()
        } else {
            format!("{}.", alias)
        };
        format!(
            "{}memory_class = 'knowledge' AND {}tags NOT LIKE '%\"turn_sync\"%'",
            prefix, prefix
        )
    }

    /// Number of migrations registered for this schema family.
    pub fn registered_migration_count(&self) -> usize {
        match self.schema_type {
            SchemaType::LibSQL => LIBSQL_MIGRATIONS.len(),
            SchemaType::StandardSQLite => SQLITE_MIGRATIONS.len(),
        }
    }

    /// Check if a file is writable
    ///
    /// Returns true if the file can be written to, false otherwise.
    /// Uses Unix metadata to check file permissions.
    fn is_file_writable(db_path: &str) -> bool {
        use std::fs;
        use std::path::Path;

        let path = Path::new(db_path);

        // If file doesn't exist, check if parent directory is writable
        if !path.exists() {
            if let Some(parent) = path.parent() {
                return parent.exists()
                    && fs::metadata(parent)
                        .map(|m| !m.permissions().readonly())
                        .unwrap_or(false);
            }
            return false;
        }

        // File exists - check if it's writable
        fs::metadata(path)
            .map(|metadata| !metadata.permissions().readonly())
            .unwrap_or(false)
    }

    /// Validate database file before opening
    ///
    /// Checks:
    /// 1. Database file exists (for local SQLite paths)
    /// 2. Database is not corrupted (basic SQLite header check)
    /// 3. File is readable
    ///
    /// # Arguments
    /// * `db_path` - Path to the database file
    /// * `must_exist` - If true, error if database doesn't exist. If false, skip existence check.
    ///
    /// # Returns
    /// * `Ok(true)` if database exists and is valid
    /// * `Ok(false)` if database doesn't exist and must_exist=false
    /// * `Err(MnemosyneError)` with actionable message if validation fails
    fn validate_database_file(db_path: &str, must_exist: bool) -> Result<bool> {
        use std::fs;
        use std::path::Path;

        let path = Path::new(db_path);

        // Check if database file exists
        if !path.exists() {
            if must_exist {
                return Err(MnemosyneError::Database(format!(
                    "Database file not found at '{}'. Please run 'mnemosyne init' first or check your DATABASE_URL configuration.",
                    db_path
                )));
            } else {
                // Database doesn't exist, but that's ok - caller will create it
                return Ok(false);
            }
        }

        // Database exists - validate it's a valid SQLite database
        // SQLite files start with "SQLite format 3\0" (16 bytes)
        match fs::read(path) {
            Ok(bytes) => {
                if bytes.len() < 16 {
                    return Err(MnemosyneError::Database(format!(
                        "Database file at '{}' is corrupted or invalid (file too small). Please delete it and run 'mnemosyne init' to reinitialize.",
                        db_path
                    )));
                }

                let header = &bytes[0..16];
                let expected_header = b"SQLite format 3\0";

                if header != expected_header {
                    return Err(MnemosyneError::Database(format!(
                        "Database file at '{}' is corrupted or not a valid SQLite database. Please delete it and run 'mnemosyne init' to reinitialize.",
                        db_path
                    )));
                }

                debug!("Database file validation passed: {}", db_path);
                Ok(true)
            }
            Err(e) => {
                // Check if it's a permission error
                let error_msg = e.to_string();
                if error_msg.contains("permission") || error_msg.contains("Permission") {
                    Err(MnemosyneError::Database(format!(
                        "Cannot read database file at '{}': Permission denied. Please check file permissions.",
                        db_path
                    )))
                } else {
                    Err(MnemosyneError::Database(format!(
                        "Cannot read database file at '{}': {}. The file may be corrupted or inaccessible.",
                        db_path, e
                    )))
                }
            }
        }
    }

    /// Detect the database schema type by checking if embedding column exists in memories table
    ///
    /// Returns:
    /// - SchemaType::LibSQL if embedding column exists (native F32_BLOB in memories table)
    /// - SchemaType::StandardSQLite if embedding column doesn't exist (separate memory_embeddings table)
    ///
    /// For fresh databases (memories table doesn't exist), defaults to LibSQL schema.
    async fn detect_schema_type(db: &Database) -> Result<SchemaType> {
        let conn = db.connect().map_err(|e| {
            MnemosyneError::Database(format!("Failed to connect for schema detection: {}", e))
        })?;

        // First, check if memories table exists
        let mut table_exists = false;
        let mut tables = conn
            .query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'",
                params![],
            )
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to query tables: {}", e)))?;

        if tables
            .next()
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to read table list: {}", e)))?
            .is_some()
        {
            table_exists = true;
        }

        // If memories table doesn't exist, this is a fresh database
        // Default to LibSQL schema for new databases (native F32_BLOB support)
        if !table_exists {
            debug!("Fresh database detected - defaulting to LibSQL schema (native vector support)");
            return Ok(SchemaType::LibSQL);
        }

        // Query table schema using PRAGMA table_info
        let mut rows = conn
            .query("PRAGMA table_info(memories)", params![])
            .await
            .map_err(|e| {
                MnemosyneError::Database(format!("Failed to query table schema: {}", e))
            })?;

        // Check if 'embedding' column exists
        while let Some(row) = rows
            .next()
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to read schema row: {}", e)))?
        {
            let column_name: String = row.get(1).map_err(|e| {
                MnemosyneError::Database(format!("Failed to read column name: {}", e))
            })?;

            if column_name == "embedding" {
                debug!("Detected LibSQL schema (embedding column found in memories table)");
                return Ok(SchemaType::LibSQL);
            }
        }

        debug!("Detected StandardSQLite schema (no embedding column in memories table)");
        Ok(SchemaType::StandardSQLite)
    }

    /// Create a new LibSQL storage backend with validation
    ///
    /// # Arguments
    /// * `mode` - Connection mode (local, in-memory, remote, or replica)
    /// * `create_if_missing` - If true, create database if it doesn't exist. If false, error on missing database.
    ///
    /// # Example
    /// ```ignore
    /// // Normal use (database must exist)
    /// let storage = LibsqlStorage::new_with_validation(ConnectionMode::Local("mnemosyne.db".into()), false).await?;
    ///
    /// // Init mode (create if missing)
    /// let storage = LibsqlStorage::new_with_validation(ConnectionMode::Local("mnemosyne.db".into()), true).await?;
    /// ```
    pub async fn new_with_validation(
        mode: ConnectionMode,
        create_if_missing: bool,
    ) -> Result<Self> {
        debug!(
            "Connecting to LibSQL database: {:?} (create_if_missing: {})",
            mode, create_if_missing
        );

        // Auto-detect read-only mode for Local connections
        let mode = match mode {
            ConnectionMode::Local(ref path) => {
                let exists = std::path::Path::new(path).exists();
                if exists && !Self::is_file_writable(path) {
                    info!(
                        "Database is read-only, switching to read-only mode: {}",
                        path
                    );
                    ConnectionMode::LocalReadOnly(path.clone())
                } else {
                    mode
                }
            }
            _ => mode,
        };

        // Track whether this is a fresh database (file didn't exist before creation).
        // Fresh databases can skip schema detection and health checks.
        let mut is_fresh = false;

        // Validate database before connecting (for local paths only)
        match &mode {
            ConnectionMode::Local(ref path) => {
                // Validate database file
                let exists = Self::validate_database_file(path, !create_if_missing)?;
                is_fresh = create_if_missing && !exists;

                // If creating and doesn't exist, create parent directory
                if create_if_missing && !exists {
                    if let Some(parent) = std::path::Path::new(path).parent() {
                        if !parent.exists() {
                            std::fs::create_dir_all(parent).map_err(|e| {
                                MnemosyneError::Database(format!(
                                    "Failed to create database directory '{}': {}",
                                    parent.display(),
                                    e
                                ))
                            })?;
                            info!("Created database directory: {}", parent.display());
                        }
                    }
                }
            }
            ConnectionMode::LocalReadOnly(ref path) => {
                // Validate read-only database file
                let _exists = Self::validate_database_file(path, true)?; // Must exist for read-only
                info!("Opening database in read-only mode: {}", path);
            }
            ConnectionMode::EmbeddedReplica { ref path, .. } => {
                // Validate replica database file
                let exists = Self::validate_database_file(path, !create_if_missing)?;
                is_fresh = create_if_missing && !exists;

                // If creating and doesn't exist, create parent directory
                if create_if_missing && !exists {
                    if let Some(parent) = std::path::Path::new(path).parent() {
                        if !parent.exists() {
                            std::fs::create_dir_all(parent).map_err(|e| {
                                MnemosyneError::Database(format!(
                                    "Failed to create database directory '{}': {}",
                                    parent.display(),
                                    e
                                ))
                            })?;
                            info!("Created database directory: {}", parent.display());
                        }
                    }
                }
            }
            ConnectionMode::InMemory | ConnectionMode::Remote { .. } => {
                // In-memory databases are always fresh
                if matches!(mode, ConnectionMode::InMemory) {
                    is_fresh = true;
                }
                // Skip validation for remote databases
                // Remote validation happens server-side
            }
        }

        let mut temporary_path = None;
        let db = match mode {
            ConnectionMode::Local(ref path) => {
                // Create parent directory only if create_if_missing is true
                if create_if_missing {
                    if let Some(parent) = std::path::Path::new(path).parent() {
                        std::fs::create_dir_all(parent).map_err(|e| {
                            MnemosyneError::Database(format!(
                                "Failed to create database directory {}: {}",
                                parent.display(),
                                e
                            ))
                        })?;
                    }
                }

                Builder::new_local(path).build().await.map_err(|e| {
                    MnemosyneError::Database(format!("Failed to create local database: {}", e))
                })?
            }
            ConnectionMode::LocalReadOnly(ref path) => {
                // Open in read-only mode
                // Note: libsql doesn't have explicit read-only builder API,
                // but we'll configure journal_mode after opening
                Builder::new_local(path).build().await.map_err(|e| {
                    MnemosyneError::Database(format!("Failed to open read-only database: {}", e))
                })?
            }
            ConnectionMode::InMemory => {
                // LibSQL creates a fresh database for each `connect()` call
                // when using `:memory:`, while this backend obtains a
                // connection per operation. A private temporary file keeps
                // the documented in-memory lifecycle without losing schema
                // visibility between operations.
                let path = std::env::temp_dir().join(format!(
                    "mnemosyne_inmemory_{}_{}.db",
                    std::process::id(),
                    Uuid::new_v4()
                ));
                temporary_path = Some(path.clone());
                Builder::new_local(path).build().await.map_err(|e| {
                    MnemosyneError::Database(format!("Failed to create in-memory database: {}", e))
                })?
            }
            ConnectionMode::Remote { ref url, ref token } => {
                Builder::new_remote(url.clone(), token.clone())
                    .build()
                    .await
                    .map_err(|e| {
                        MnemosyneError::Database(format!("Failed to create remote database: {}", e))
                    })?
            }
            ConnectionMode::EmbeddedReplica {
                ref path,
                ref url,
                ref token,
            } => {
                // Create parent directory only if create_if_missing is true
                if create_if_missing {
                    if let Some(parent) = std::path::Path::new(path).parent() {
                        std::fs::create_dir_all(parent).map_err(|e| {
                            MnemosyneError::Database(format!(
                                "Failed to create replica directory {}: {}",
                                parent.display(),
                                e
                            ))
                        })?;
                    }
                }

                Builder::new_remote_replica(path, url.clone(), token.clone())
                    .build()
                    .await
                    .map_err(|e| {
                        MnemosyneError::Database(format!(
                            "Failed to create embedded replica: {}",
                            e
                        ))
                    })?
            }
        };

        debug!("LibSQL database connection established");

        // For in-memory databases, PRAGMAs are now merged into the pre-compiled
        // migration batch SQL (LIBSQL_FRESH_SQL_MEM / SQLITE_FRESH_SQL_MEM) in
        // run_migrations(), eliminating the need for a separate connection here.
        // journal_mode=MEMORY, synchronous=OFF, temp_store=MEMORY, cache_size=64MB
        // are applied as part of the single migration execute_batch call.

        // Detect schema type by checking if embedding column exists in memories table
        // LibSQL schema: embedding stored as F32_BLOB in memories table (native vector support)
        // StandardSQLite schema: embeddings stored in separate memory_embeddings table
        // Skip detection for fresh databases — default to LibSQL schema (new databases
        // always use native F32_BLOB support).
        let schema_type = if is_fresh {
            debug!("Fresh database — defaulting to LibSQL schema (skipping detection)");
            SchemaType::LibSQL
        } else {
            Self::detect_schema_type(&db).await?
        };
        debug!(
            "Detected database schema: {:?} (embedding column {} in memories table)",
            schema_type,
            if schema_type == SchemaType::LibSQL {
                "present"
            } else {
                "absent"
            }
        );

        // Extract database path for health checks
        let db_path = match &mode {
            ConnectionMode::Local(path) | ConnectionMode::LocalReadOnly(path) => path.clone(),
            ConnectionMode::EmbeddedReplica { path, .. } => path.clone(),
            ConnectionMode::InMemory => temporary_path
                .as_ref()
                .map(|path| path.to_string_lossy().into_owned())
                .unwrap_or_else(|| ":memory:".to_string()),
            ConnectionMode::Remote { url, .. } => url.clone(),
        };

        let storage = Self {
            db,
            embedding_service: None,
            search_config: crate::config::SearchConfig::default(),
            schema_type,
            db_path,
            temporary_path,
        };

        // Verify database health and run migrations (skip for read-only databases)
        match &mode {
            ConnectionMode::LocalReadOnly(_) => {
                info!("Skipping health check and migrations for read-only database");
                // Just verify basic connectivity with a read-only query
                let conn = storage.get_conn()?;
                conn.query("SELECT 1", params![]).await.map_err(|e| {
                    MnemosyneError::Database(format!(
                        "Read-only database connectivity test failed: {}",
                        e
                    ))
                })?;
            }
            _ => {
                // Skip health check for fresh databases — they were just
                // created and haven't been corrupted yet. Only verify
                // existing databases that could be in a bad state.
                if !is_fresh {
                    storage.verify_database_health().await?;
                    // Ensure columns needed by compatibility migrations before
                    // a later migration rebuilds legacy tables.
                    storage.ensure_maintenance_columns().await?;
                }
                storage.run_migrations(is_fresh).await?;
                storage.ensure_maintenance_columns().await?;
                storage.ensure_integrity_columns().await?;
            }
        }

        // Verify database file exists for local modes
        match &mode {
            ConnectionMode::Local(path)
            | ConnectionMode::LocalReadOnly(path)
            | ConnectionMode::EmbeddedReplica { path, .. } => {
                if !std::path::Path::new(path).exists() {
                    return Err(MnemosyneError::Database(format!(
                        "Database file not created after initialization: {}",
                        path
                    )));
                }
                debug!("Verified database file exists: {}", path);
            }
            _ => {} // In-memory and remote don't have local files
        }

        Ok(storage)
    }

    /// Create a new LibSQL storage backend
    ///
    /// Default behavior: requires database to exist (secure by default).
    /// Returns clear error if database not found or corrupted.
    ///
    /// For explicit database creation, use `new_with_validation(..., true)`.
    ///
    /// # Arguments
    /// * `mode` - Connection mode (local, in-memory, remote, or replica)
    ///
    /// # Example
    /// ```ignore
    /// // Normal use (requires database to exist)
    /// let storage = LibsqlStorage::new(ConnectionMode::Local("mnemosyne.db".into())).await?;
    /// ```
    pub async fn new(mode: ConnectionMode) -> Result<Self> {
        // Default behavior: database must exist (secure by default, clear errors)
        // This prevents accidental database creation and ensures explicit initialization
        // Use new_with_validation(..., true) for database creation (init/serve commands)
        Self::new_with_validation(mode, false).await
    }

    /// Create a new local file-based storage (convenience method)
    ///
    /// # Arguments
    /// * `path` - Path to the database file
    ///
    /// # Example
    /// ```ignore
    /// let storage = LibsqlStorage::new_local("mnemosyne.db").await?;
    /// ```
    pub async fn new_local(path: &str) -> Result<Self> {
        Self::new(ConnectionMode::Local(path.to_string())).await
    }

    /// Get a shared file-backed storage for test-only use.
    ///
    /// LibSQL's `:memory:` connections are not shared across calls to
    /// `Database::connect`, so a temporary file is used to keep this cache
    /// reliable while retaining the one-database-per-test-process behavior.
    #[cfg(test)]
    pub async fn shared_test_storage() -> Result<Arc<LibsqlStorage>> {
        if let Some(cached) = SHARED_TEST_STORAGE.get() {
            return Ok(cached.clone());
        }
        let path = std::env::temp_dir().join(format!(
            "mnemosyne_shared_test_{}_{}.db",
            std::process::id(),
            Uuid::new_v4()
        ));
        let storage = Arc::new(
            Self::new_with_validation(
                ConnectionMode::Local(path.to_string_lossy().into_owned()),
                true,
            )
            .await?,
        );
        let _ = SHARED_TEST_STORAGE.set(storage.clone());
        Ok(SHARED_TEST_STORAGE.get().cloned().unwrap_or(storage))
    }

    /// Synchronous version of `shared_test_storage()` that doesn't require a tokio
    /// runtime. Uses `OnceLock::get()` for cached lookups (O(1) atomic load) and
    /// only creates a temporary runtime for the one-time initialization on first call.
    ///
    /// This allows pure-computation tests to use `#[test]` instead of `#[tokio::test]`,
    /// avoiding the ~0.15ms tokio runtime creation overhead per test.
    #[cfg(test)]
    pub fn shared_test_storage_sync() -> Result<Arc<LibsqlStorage>> {
        if let Some(cached) = SHARED_TEST_STORAGE.get() {
            return Ok(cached.clone());
        }

        // First call: block on async initialization. Only happens once per process.
        let rt = tokio::runtime::Runtime::new().map_err(|e| {
            MnemosyneError::Database(format!("Failed to create test runtime: {}", e))
        })?;
        let path = std::env::temp_dir().join(format!(
            "mnemosyne_shared_test_{}_{}.db",
            std::process::id(),
            Uuid::new_v4()
        ));
        let storage = Arc::new(rt.block_on(Self::new_with_validation(
            ConnectionMode::Local(path.to_string_lossy().into_owned()),
            true,
        ))?);
        let _ = SHARED_TEST_STORAGE.set(storage.clone());
        rt.shutdown_background();
        Ok(SHARED_TEST_STORAGE.get().cloned().unwrap_or(storage))
    }

    /// Create from string path (backward compatibility)
    ///
    /// Parses database path and creates appropriate connection mode:
    /// - ":memory:" → InMemory
    /// - "libsql://..." → Remote (requires token in environment)
    /// - Other → Local file path
    pub async fn from_path(database_url: &str) -> Result<Self> {
        let mode = if database_url == ":memory:" {
            ConnectionMode::InMemory
        } else if database_url.starts_with("libsql://") {
            let token = std::env::var("TURSO_AUTH_TOKEN")
                .map_err(|_| MnemosyneError::Other("TURSO_AUTH_TOKEN not found".into()))?;
            ConnectionMode::Remote {
                url: database_url.to_string(),
                token,
            }
        } else {
            ConnectionMode::Local(database_url.to_string())
        };

        Self::new(mode).await
    }

    /// Create LibsqlStorage directly from a Database instance (for tests)
    ///
    /// This bypasses the normal initialization and migration process,
    /// useful when you need to set up a custom schema for testing.
    #[allow(dead_code)]
    pub(crate) fn from_database(db: Database) -> Self {
        Self {
            db,
            embedding_service: None,
            search_config: crate::config::SearchConfig::default(),
            schema_type: SchemaType::LibSQL, // Use LibSQL schema (F32_BLOB support)
            db_path: ":memory:".to_string(), // Test databases typically use in-memory
            temporary_path: None,
        }
    }

    /// Verify database health before operations
    async fn verify_database_health(&self) -> Result<()> {
        let conn = self.get_conn()?;

        // Test 1: Basic query to detect corruption
        let test_query = "SELECT 1";
        conn.query(test_query, params![]).await.map_err(|e| {
            MnemosyneError::Database(format!(
                "Database corruption detected or invalid database file: {}",
                e
            ))
        })?;

        // Test 2: Check if database is writable
        // Try to create a test table and drop it
        let write_test = r#"
            CREATE TABLE IF NOT EXISTS _health_check (id INTEGER PRIMARY KEY);
            DROP TABLE IF EXISTS _health_check;
        "#;

        if let Err(e) = conn.execute_batch(write_test).await {
            // Check if it's a read-only error
            let error_msg = e.to_string().to_lowercase();
            if error_msg.contains("read") && error_msg.contains("only")
                || error_msg.contains("readonly")
                || error_msg.contains("permission")
            {
                return Err(MnemosyneError::Database(format!(
                    "Database is read-only or lacks write permissions: {}",
                    e
                )));
            }
            // Other write errors
            return Err(MnemosyneError::Database(format!(
                "Database write test failed: {}",
                e
            )));
        }

        debug!("Database health check passed");
        Ok(())
    }

    /// Run database migrations
    pub async fn run_migrations(&self, is_fresh: bool) -> Result<()> {
        debug!("Running database migrations...");
        let _migration_guard = MIGRATION_LOCK.lock().await;

        // Get a connection for migrations
        let conn = self.get_conn()?;

        // For fresh databases, skip the separate CREATE TABLE call —
        // the pre-compiled batch SQL already includes it, reducing DB round-trips.
        if !is_fresh {
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _migrations_applied (
                    migration_name TEXT PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                )",
                params![],
            )
            .await
            .map_err(|e| {
                MnemosyneError::Migration(format!("Failed to create migrations table: {}", e))
            })?;
        }

        // For fresh databases, we know the _migrations_applied table is empty,
        // so we can skip the COUNT(*) query and batch all migrations in a single
        // execute_batch call.
        if is_fresh {
            debug!("Fresh database — using pre-compiled migration SQL (no file I/O)");

            // Use pre-compiled migration SQL embedded at build time via include_str!.
            // Lazy<String> ensures SQL is parsed once per process, not per test.
            // For in-memory databases, include PRAGMA optimizations at the start of
            // the batch so a single execute_batch call sets PRAGMAs + creates schema.
            let is_mem = self.db_path == ":memory:";
            let (batch_sql, migration_names) = match (self.schema_type, is_mem) {
                (SchemaType::LibSQL, true) => (&*LIBSQL_FRESH_SQL_MEM, LIBSQL_MIGRATION_NAMES),
                (SchemaType::LibSQL, false) => (&*LIBSQL_FRESH_SQL, LIBSQL_MIGRATION_NAMES),
                (SchemaType::StandardSQLite, true) => {
                    (&*SQLITE_FRESH_SQL_MEM, SQLITE_MIGRATION_NAMES)
                }
                (SchemaType::StandardSQLite, false) => (&*SQLITE_FRESH_SQL, SQLITE_MIGRATION_NAMES),
            };

            // Execute all migration SQL + record inserts in a single batch
            conn.execute_batch(batch_sql).await.map_err(|e| {
                MnemosyneError::Migration(format!("Failed to execute combined migrations: {}", e))
            })?;

            #[cfg(not(test))]
            for name in migration_names {
                debug!("Executed migration: {}", name);
            }

            debug!("Database migrations completed (single batch, pre-compiled)");
            self.ensure_maintenance_columns_on(&conn).await?;
            if self.db_path != ":memory:" {
                self.check_text_learning_integrity().await?;
            }
            return Ok(());
        }

        // Non-fresh database: check which migrations are already applied
        let mut count_rows = conn
            .query("SELECT COUNT(*) FROM _migrations_applied", params![])
            .await
            .map_err(|e| {
                MnemosyneError::Migration(format!("Failed to check migrations count: {}", e))
            })?;
        let has_applied_migrations = count_rows
            .next()
            .await
            .map_err(|e| {
                MnemosyneError::Migration(format!("Failed to read migration count: {}", e))
            })?
            .map(|row| {
                let count: i64 = row.get(0).unwrap_or(0);
                count > 0
            })
            .unwrap_or(false);
        drop(count_rows);

        if has_applied_migrations {
            debug!("Existing migrations found - will check each file individually");
        } else {
            debug!("No migrations applied yet - skipping per-file checks for fresh database");
        }

        // Use pre-compiled migration SQL (no file I/O) — different
        // files for each schema type, looked up via lookup_migration_sql()
        let migration_files: &[&str] = match self.schema_type {
            SchemaType::LibSQL => LIBSQL_MIGRATION_NAMES,
            SchemaType::StandardSQLite => SQLITE_MIGRATION_NAMES,
        };

        debug!(
            "Running {} migrations (schema type: {:?})",
            migration_files.len(),
            self.schema_type
        );

        for migration_file in migration_files {
            // Check if migration already applied
            if has_applied_migrations {
                let mut rows = conn
                    .query(
                        "SELECT COUNT(*) FROM _migrations_applied WHERE migration_name = ?",
                        params![migration_file],
                    )
                    .await?;

                let already_applied = if let Some(row) = rows.next().await? {
                    row.get::<i64>(0).unwrap_or(0)
                } else {
                    0
                };

                if already_applied > 0 {
                    debug!("Skipping already applied migration: {}", migration_file);
                    continue;
                }
            }
            debug!("Executing migration: {}", migration_file);

            // Migration 015 rebuilds audit_log from either legacy `details`
            // or current `metadata`. Add whichever compatibility column is
            // absent so both details-only and metadata-only databases can run
            // the same embedded migration safely.
            if self.schema_type == SchemaType::StandardSQLite
                && *migration_file == "015_fix_audit_log_schema.sql"
            {
                self.ensure_audit_migration_columns(&conn).await?;
            }
            if *migration_file == "027_memory_integrity.sql" {
                self.recover_integrity_migration_state(&conn).await?;
            }
            // Migration 028's ALTER TABLE is not itself repeatable if a
            // process crashed after adding the column but before recording
            // the migration. Finish its index/bookkeeping idempotently.
            if *migration_file == "028_retrieval_trace_namespace.sql"
                && connection_has_column(&conn, "retrieval_traces", "namespace").await?
            {
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_retrieval_traces_namespace_time ON retrieval_traces(namespace, created_at DESC)",
                    params![],
                )
                .await?;
                let now = Utc::now().timestamp();
                conn.execute(
                    "INSERT INTO _migrations_applied (migration_name, applied_at) VALUES (?, ?)",
                    params![migration_file, now],
                )
                .await?;
                info!("Recovered migration: {}", migration_file);
                continue;
            }

            // Use pre-compiled SQL from include_str! (no file I/O)
            let sql = lookup_migration_sql(self.schema_type, migration_file);

            // Parse SQL statements properly, handling multi-line statements like triggers
            let statements = parse_sql_statements(sql);
            debug!(
                "Parsed {} statements from {}",
                statements.len(),
                migration_file
            );
            // Execute all non-empty statements in a single batch call.
            // This reduces database round-trips from N (one per statement) to 1.
            let batch_sql: String = statements
                .iter()
                .map(|s| s.trim())
                .filter(|s| !s.is_empty())
                .map(|s| s.to_string())
                .collect::<Vec<_>>()
                .join(";\n");

            // Apply each upgrade and its bookkeeping atomically. This is
            // important for table-rebuild migrations: a crash cannot leave a
            // replacement schema without its copied rows while marking the
            // migration as complete.
            conn.execute("BEGIN", params![]).await?;
            if !batch_sql.is_empty() {
                if let Err(error) = conn.execute_batch(&batch_sql).await {
                    let _ = conn.execute("ROLLBACK", params![]).await;
                    return Err(MnemosyneError::Migration(format!(
                        "Failed to execute migration {}: {}\nSQL: {}",
                        migration_file,
                        error,
                        &batch_sql[..batch_sql.len().min(500)]
                    )));
                }
            }

            // Record migration as applied inside the same transaction.
            let now = Utc::now().timestamp();
            if let Err(error) = conn
                .execute(
                    "INSERT INTO _migrations_applied (migration_name, applied_at) VALUES (?, ?)",
                    params![migration_file, now],
                )
                .await
            {
                let _ = conn.execute("ROLLBACK", params![]).await;
                return Err(MnemosyneError::Migration(format!(
                    "Failed to record migration: {}",
                    error
                )));
            }
            conn.execute("COMMIT", params![]).await?;

            info!("Executed migration: {}", migration_file);
        }

        debug!("Database migrations completed");
        if self.db_path != ":memory:" {
            self.check_text_learning_integrity().await?;
        }
        Ok(())
    }

    /// Ensure compatibility columns used by the existing evolution scans are
    /// present without making migration 019 fail on databases that already
    /// received the legacy evolution schema.
    async fn ensure_maintenance_columns(&self) -> Result<()> {
        if self.db_path == ":memory:" {
            return Ok(());
        }
        let conn = self.get_conn()?;
        self.ensure_maintenance_columns_on(&conn).await
    }

    /// Complete the integrity upgrade idempotently. Keeping the column add and
    /// hash backfill here makes recovery safe when an older database was
    /// interrupted between migration statements.
    async fn recover_integrity_migration_state(&self, conn: &Connection) -> Result<()> {
        let mut rows = conn
            .query(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('memory_maintenance_runs', 'memory_maintenance_runs_027_old')",
                params![],
            )
            .await?;
        let mut has_current = false;
        let mut has_backup = false;
        while let Some(row) = rows.next().await? {
            match row.get::<String>(0)?.as_str() {
                "memory_maintenance_runs" => has_current = true,
                "memory_maintenance_runs_027_old" => has_backup = true,
                _ => {}
            }
        }
        drop(rows);
        if has_backup {
            if has_current {
                // If a crash happened after the replacement table was created
                // but before its copy completed, restore the backup rather
                // than silently discarding maintenance history. Otherwise the
                // replacement is complete and the renamed table is stale.
                let current_count =
                    scalar_count(conn, "SELECT COUNT(*) FROM memory_maintenance_runs").await?;
                let backup_count =
                    scalar_count(conn, "SELECT COUNT(*) FROM memory_maintenance_runs_027_old")
                        .await?;
                if current_count < backup_count {
                    conn.execute("DROP TABLE memory_maintenance_runs", params![])
                        .await?;
                    conn.execute(
                        "ALTER TABLE memory_maintenance_runs_027_old RENAME TO memory_maintenance_runs",
                        params![],
                    )
                    .await?;
                } else {
                    conn.execute("DROP TABLE memory_maintenance_runs_027_old", params![])
                        .await?;
                }
            } else {
                // Recover the old table before retrying the migration so its
                // advisory history is retained.
                conn.execute(
                    "ALTER TABLE memory_maintenance_runs_027_old RENAME TO memory_maintenance_runs",
                    params![],
                )
                .await?;
            }
        }
        Ok(())
    }

    async fn ensure_integrity_columns(&self) -> Result<()> {
        let _migration_guard = MIGRATION_LOCK.lock().await;
        let conn = self.get_conn()?;
        if !connection_has_column(&conn, "memories", "content_hash").await? {
            conn.execute(
                "ALTER TABLE memories ADD COLUMN content_hash TEXT",
                params![],
            )
            .await?;
        }
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_namespace_content_hash ON memories(namespace, content_hash)", params![]).await?;
        conn.execute_batch("CREATE TABLE IF NOT EXISTS memory_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0), observed_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)), superseded_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(memory_id, subject, predicate, object), FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE, FOREIGN KEY(superseded_by) REFERENCES memory_facts(id) ON DELETE SET NULL); CREATE INDEX IF NOT EXISTS idx_memory_facts_lookup ON memory_facts(subject, predicate, is_active); CREATE INDEX IF NOT EXISTS idx_memory_facts_memory ON memory_facts(memory_id);").await?;
        let mut rows = conn.query("SELECT id, content FROM memories WHERE content_hash IS NULL OR content_hash = '' LIMIT ?", params![INTEGRITY_SCAN_LIMIT as i64]).await?;
        let mut pending = Vec::new();
        while let Some(row) = rows.next().await? {
            pending.push((row.get::<String>(0)?, row.get::<String>(1)?));
        }
        drop(rows);
        for (id, content) in pending {
            conn.execute("UPDATE memories SET content_hash = ? WHERE id = ? AND (content_hash IS NULL OR content_hash = '')", params![content_hash(&content), id]).await?;
        }
        Ok(())
    }

    async fn ensure_audit_migration_columns(&self, conn: &Connection) -> Result<()> {
        let has_metadata = connection_has_column(conn, "audit_log", "metadata").await?;
        let has_details = connection_has_column(conn, "audit_log", "details").await?;
        if !has_metadata {
            conn.execute("ALTER TABLE audit_log ADD COLUMN metadata TEXT", params![])
                .await?;
        }
        if !has_details {
            conn.execute("ALTER TABLE audit_log ADD COLUMN details TEXT", params![])
                .await?;
        }
        Ok(())
    }

    async fn ensure_maintenance_columns_on(&self, conn: &Connection) -> Result<()> {
        let mut rows = conn.query("PRAGMA table_info(memories)", params![]).await?;
        let mut has_archived_at = false;
        while let Some(row) = rows.next().await? {
            let name: String = row.get(1)?;
            has_archived_at |= name == "archived_at";
        }
        drop(rows);
        if !has_archived_at {
            conn.execute(
                "ALTER TABLE memories ADD COLUMN archived_at INTEGER",
                params![],
            )
            .await?;
        }

        let mut rows = conn
            .query("PRAGMA table_info(memory_links)", params![])
            .await?;
        let mut has_last_traversed_at = false;
        let mut has_user_created = false;
        while let Some(row) = rows.next().await? {
            let name: String = row.get(1)?;
            has_last_traversed_at |= name == "last_traversed_at";
            has_user_created |= name == "user_created";
        }
        drop(rows);
        if !has_last_traversed_at {
            conn.execute(
                "ALTER TABLE memory_links ADD COLUMN last_traversed_at INTEGER",
                params![],
            )
            .await?;
        }
        if !has_user_created {
            conn.execute(
                "ALTER TABLE memory_links ADD COLUMN user_created INTEGER NOT NULL DEFAULT 0",
                params![],
            )
            .await?;
        }
        Ok(())
    }

    /// Verify that additive turn-learning metadata has no dangling rows.
    pub async fn check_text_learning_integrity(&self) -> Result<()> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT kind, id FROM text_learning_orphans LIMIT 1",
                params![],
            )
            .await
            .map_err(|error| {
                MnemosyneError::Migration(format!(
                    "text-learning integrity check failed: {}",
                    error
                ))
            })?;
        if let Some(row) = rows.next().await? {
            let kind: String = row.get(0)?;
            let id: String = row.get(1)?;
            return Err(MnemosyneError::Migration(format!(
                "orphaned text-learning {} row: {}",
                kind, id
            )));
        }
        Ok(())
    }

    /// Get a connection from the database
    pub(crate) fn get_conn(&self) -> Result<Connection> {
        self.db
            .connect()
            .map_err(|e| MnemosyneError::Database(format!("Failed to get connection: {}", e)))
    }

    /// Check if database is healthy and operational
    ///
    /// Performs basic health checks:
    /// - Can establish connection
    /// - Can execute simple query
    /// - Database is not corrupted
    ///
    /// Returns Ok(()) if healthy, Err with diagnostic info if not
    pub async fn check_database_health(&self) -> Result<()> {
        debug!("Checking database health...");

        // Try to get a connection
        let conn = self.get_conn().map_err(|e| {
            MnemosyneError::Database(format!(
                "Health check failed: cannot establish connection: {}",
                e
            ))
        })?;

        // Try a simple query to verify database is operational
        match conn.query("SELECT 1", ()).await {
            Ok(_) => {
                debug!("Database health check passed");
                Ok(())
            }
            Err(e) => {
                let error_msg = e.to_string();
                if error_msg.contains("readonly") || error_msg.contains("permission") {
                    Err(MnemosyneError::Database(
                        "Database is read-only or permission denied. Check file permissions."
                            .to_string(),
                    ))
                } else if error_msg.contains("corrupt") || error_msg.contains("malformed") {
                    Err(MnemosyneError::Database(
                        "Database appears to be corrupted. Consider restoring from backup."
                            .to_string(),
                    ))
                } else {
                    Err(MnemosyneError::Database(format!(
                        "Health check failed: {}",
                        error_msg
                    )))
                }
            }
        }
    }

    /// Attempt to recover from database errors
    ///
    /// Tries to recover from common error conditions:
    /// - Stale lock files
    /// - Permission issues
    /// - Connection pool exhaustion
    ///
    /// Returns Ok(()) if recovery successful or not needed
    pub async fn recover_from_error(&self) -> Result<()> {
        debug!("Attempting database recovery...");

        // First check if database is healthy
        match self.check_database_health().await {
            Ok(()) => {
                debug!("Database is healthy, no recovery needed");
                return Ok(());
            }
            Err(e) => {
                debug!("Database health check failed: {}, attempting recovery", e);
                // Continue with recovery attempt
            }
        }

        // Get a fresh connection for recovery operations
        let conn = self.get_conn().map_err(|e| {
            MnemosyneError::Database(format!("Cannot establish connection for recovery: {}", e))
        })?;

        // Step 1: Try to checkpoint WAL to clear pending writes
        debug!("Attempting WAL checkpoint to recover from stale state...");
        match conn.execute("PRAGMA wal_checkpoint(TRUNCATE)", ()).await {
            Ok(_) => {
                info!("WAL checkpoint successful - database recovered");
                return Ok(());
            }
            Err(e) => {
                debug!("WAL checkpoint failed: {}, trying alternative recovery", e);
            }
        }

        // Step 2: Try to reinitialize WAL mode
        debug!("Attempting to reinitialize WAL mode...");
        match conn.execute("PRAGMA journal_mode=WAL", ()).await {
            Ok(_) => {
                info!("WAL mode reinitialized - database recovered");

                // Verify recovery with a simple query
                match conn.execute("SELECT 1", ()).await {
                    Ok(_) => {
                        debug!("Database is now operational after recovery");
                        Ok(())
                    }
                    Err(e) => Err(MnemosyneError::Database(format!(
                        "Recovery partially successful but database still not operational: {}. \
                            Manual intervention may be required: delete .db-wal and .db-shm files.",
                        e
                    ))),
                }
            }
            Err(e) => Err(MnemosyneError::Database(format!(
                "Recovery failed: {}. Manual intervention required: \
                    1. Check file permissions on database and WAL files (.db-wal, .db-shm) \
                    2. If permissions are correct, delete stale WAL files and retry \
                    3. As a last resort, restore from backup",
                e
            ))),
        }
    }

    /// Get the database file path
    pub fn db_path(&self) -> PathBuf {
        PathBuf::from(&self.db_path)
    }

    /// Check database integrity using PRAGMA integrity_check
    pub async fn check_integrity(&self) -> Result<bool> {
        let conn = self.get_conn()?;

        match conn.query("PRAGMA integrity_check", ()).await {
            Ok(mut rows) => {
                if let Some(row) = rows.next().await? {
                    let result: String = row.get(0)?;
                    Ok(result == "ok")
                } else {
                    Ok(false)
                }
            }
            Err(e) => Err(MnemosyneError::Database(format!(
                "Integrity check failed: {}",
                e
            ))),
        }
    }

    /// Check if a table exists in the database
    pub async fn table_exists(&self, table_name: &str) -> Result<bool> {
        let conn = self.get_conn()?;

        let query = "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?";
        let mut rows = conn.query(query, [table_name]).await?;

        if let Some(row) = rows.next().await? {
            let count: i64 = row.get(0)?;
            Ok(count > 0)
        } else {
            Ok(false)
        }
    }

    /// Get list of applied migrations from _migrations_applied table
    pub async fn get_applied_migrations(&self) -> Result<Vec<String>> {
        let conn = self.get_conn()?;

        // Check if migrations table exists first
        if !self.table_exists("_migrations_applied").await? {
            return Ok(Vec::new());
        }

        let query = "SELECT migration_name FROM _migrations_applied ORDER BY applied_at";
        let mut rows = conn.query(query, ()).await?;

        let mut migrations = Vec::new();
        while let Some(row) = rows.next().await? {
            let name: String = row.get(0)?;
            migrations.push(name);
        }

        Ok(migrations)
    }

    /// Get importance distribution as a HashMap<importance_level, count>
    pub async fn get_importance_distribution(
        &self,
    ) -> Result<std::collections::HashMap<u8, usize>> {
        let conn = self.get_conn()?;

        // Check if archived_at column exists (added in later migrations)
        // If it doesn't exist, just get all memories
        let has_archived = match conn
            .query(
                "SELECT COUNT(*) FROM pragma_table_info('memories') WHERE name='archived_at'",
                (),
            )
            .await
        {
            Ok(mut rows) => {
                if let Some(row) = rows.next().await? {
                    let count: i64 = row.get(0)?;
                    count > 0
                } else {
                    false
                }
            }
            Err(_) => false,
        };

        let query = if has_archived {
            r#"
                SELECT
                    CAST(importance AS INTEGER) as imp_level,
                    COUNT(*) as count
                FROM memories
                WHERE archived_at IS NULL
                GROUP BY imp_level
                ORDER BY imp_level
            "#
        } else {
            r#"
                SELECT
                    CAST(importance AS INTEGER) as imp_level,
                    COUNT(*) as count
                FROM memories
                GROUP BY imp_level
                ORDER BY imp_level
            "#
        };

        let mut rows = conn.query(query, ()).await?;
        let mut distribution = std::collections::HashMap::new();

        while let Some(row) = rows.next().await? {
            let importance: i64 = row.get(0)?;
            let count: i64 = row.get(1)?;
            distribution.insert(importance as u8, count as usize);
        }

        Ok(distribution)
    }

    async fn load_provenance(
        &self,
        id: MemoryId,
    ) -> Result<Option<crate::types::MemoryProvenance>> {
        let conn = self.get_conn()?;
        let mut rows = match conn
            .query(
                "SELECT source_kind, source_memory_id, session_id, turn_id, source_role, observed_at, evidence_quote, extractor_model, extraction_schema_version FROM memory_provenance WHERE memory_id = ?",
                params![id.to_string()],
            )
            .await
        {
            Ok(rows) => rows,
            // Read-only callers may open a pre-migration database. Treat the
            // optional additive metadata as absent rather than breaking old
            // factual recall.
            Err(_) => return Ok(None),
        };
        let Some(row) = rows.next().await? else {
            return Ok(None);
        };
        let source_kind: String = row.get(0)?;
        let source_role: String = row.get(4)?;
        let observed_at: String = row.get(5)?;
        let observed_at = chrono::DateTime::parse_from_rfc3339(&observed_at)
            .map_err(|e| MnemosyneError::Other(format!("Invalid provenance timestamp: {}", e)))?
            .with_timezone(&Utc);
        Ok(Some(crate::types::MemoryProvenance {
            source_kind: match source_kind.as_str() {
                "turn" => crate::types::ProvenanceSourceKind::Turn,
                "import" => crate::types::ProvenanceSourceKind::Import,
                _ => crate::types::ProvenanceSourceKind::Manual,
            },
            source_memory_id: row
                .get::<Option<String>>(1)?
                .and_then(|value| MemoryId::from_string(&value).ok()),
            session_id: row.get(2)?,
            turn_id: row.get(3)?,
            source_role: match source_role.as_str() {
                "user" => crate::types::ProvenanceSourceRole::User,
                "assistant" => crate::types::ProvenanceSourceRole::Assistant,
                "system" => crate::types::ProvenanceSourceRole::System,
                _ => crate::types::ProvenanceSourceRole::Unknown,
            },
            observed_at,
            evidence_quote: row.get(6)?,
            extractor_model: row.get(7)?,
            extraction_schema_version: row.get(8)?,
        }))
    }

    /// Convert a stable memory projection to a MemoryNote.
    async fn row_to_memory(&self, row: &libsql::Row) -> Result<MemoryNote> {
        // Extract all fields from row
        let id_str: String = row.get(0)?;
        let id = MemoryId::from_string(&id_str)?;

        let namespace_json: String = row.get(1)?;
        let namespace: Namespace = serde_json::from_str(&namespace_json)?;

        let created_at: String = row.get(2)?;
        let created_at = chrono::DateTime::parse_from_rfc3339(&created_at)
            .map_err(|e| MnemosyneError::Other(format!("Invalid timestamp: {}", e)))?
            .with_timezone(&chrono::Utc);

        let updated_at: String = row.get(3)?;
        let updated_at = chrono::DateTime::parse_from_rfc3339(&updated_at)
            .map_err(|e| MnemosyneError::Other(format!("Invalid timestamp: {}", e)))?
            .with_timezone(&chrono::Utc);

        let content: String = row.get(4)?;
        let summary: String = row.get(5)?;

        let keywords_json: String = row.get(6)?;
        let keywords: Vec<String> = serde_json::from_str(&keywords_json)?;

        let tags_json: String = row.get(7)?;
        let tags: Vec<String> = serde_json::from_str(&tags_json)?;

        let context: String = row.get(8)?;

        let memory_type_str: String = row.get(9)?;
        let memory_type = match memory_type_str.as_str() {
            "architecture_decision" => crate::types::MemoryType::ArchitectureDecision,
            "code_pattern" => crate::types::MemoryType::CodePattern,
            "bug_fix" => crate::types::MemoryType::BugFix,
            "configuration" => crate::types::MemoryType::Configuration,
            "constraint" => crate::types::MemoryType::Constraint,
            "entity" => crate::types::MemoryType::Entity,
            "insight" => crate::types::MemoryType::Insight,
            "reference" => crate::types::MemoryType::Reference,
            "preference" => crate::types::MemoryType::Preference,
            "task" => crate::types::MemoryType::Task,
            "agent_event" => crate::types::MemoryType::AgentEvent,
            "constitution" => crate::types::MemoryType::Constitution,
            "feature_spec" => crate::types::MemoryType::FeatureSpec,
            "implementation_plan" => crate::types::MemoryType::ImplementationPlan,
            "task_breakdown" => crate::types::MemoryType::TaskBreakdown,
            "quality_checklist" => crate::types::MemoryType::QualityChecklist,
            "clarification" => crate::types::MemoryType::Clarification,
            _ => {
                return Err(MnemosyneError::Other(format!(
                    "Unknown memory type: {}",
                    memory_type_str
                )))
            }
        };

        // `memory_class` is part of the stable projection at column 10, even
        // though the physical migration appends it to the table.
        let memory_class_str: String = row.get(10).unwrap_or_else(|_| "knowledge".to_string());
        let memory_class = match memory_class_str.as_str() {
            "interaction_policy" => MemoryClass::InteractionPolicy,
            _ => MemoryClass::Knowledge,
        };

        let importance: i64 = row.get(11)?;
        let confidence: f64 = row.get(12)?;

        let related_files_json: String = row.get(13)?;
        let related_files: Vec<String> = serde_json::from_str(&related_files_json)?;

        let related_entities_json: String = row.get(14)?;
        let related_entities: Vec<String> = serde_json::from_str(&related_entities_json)?;

        let access_count: i64 = row.get(15)?;

        let last_accessed_str: String = row.get(16)?;
        let last_accessed_at = chrono::DateTime::parse_from_rfc3339(&last_accessed_str)
            .map_err(|e| MnemosyneError::Other(format!("Invalid timestamp: {}", e)))?
            .with_timezone(&chrono::Utc);

        let expires_at: Option<String> = row.get(17)?;
        let expires_at = expires_at
            .map(|s| chrono::DateTime::parse_from_rfc3339(&s))
            .transpose()
            .map_err(|e| MnemosyneError::Other(format!("Invalid timestamp: {}", e)))?
            .map(|dt| dt.with_timezone(&chrono::Utc));

        let is_archived: i64 = row.get(18)?;
        let is_archived = is_archived != 0;

        let superseded_by: Option<String> = row.get(19)?;
        let superseded_by = superseded_by.and_then(|s| MemoryId::from_string(&s).ok());

        let embedding_model: String = row.get(20)?;

        // Get embedding from column 21 (F32_BLOB type) when present.
        //
        // Vector/keyword projections may append a numeric score after the
        // stable memory columns. Reading a non-BLOB column as Vec<u8> can
        // panic inside libsql, so check the projected embedding type first.
        let embedding: Option<Vec<f32>> = if self.schema_type == SchemaType::LibSQL
            && matches!(row.column_type(21), Ok(libsql::ValueType::Blob))
        {
            row.get::<Option<Vec<u8>>>(21)
                .ok()
                .flatten()
                .and_then(|bytes| {
                    // F32_BLOB is stored as raw f32 bytes in little-endian
                    if bytes.len() % 4 != 0 {
                        return None;
                    }
                    Some(
                        bytes
                            .chunks_exact(4)
                            .map(|chunk| {
                                f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]])
                            })
                            .collect(),
                    )
                })
        } else {
            None
        };

        let provenance = self.load_provenance(id).await?;

        Ok(MemoryNote {
            id,
            namespace,
            created_at,
            updated_at,
            content,
            summary,
            keywords,
            tags,
            context,
            memory_type,
            memory_class,
            provenance,
            importance: importance as u8,
            confidence: confidence as f32,
            links: Vec::new(), // Will be populated separately via graph traversal
            related_files,
            related_entities,
            access_count: access_count as u32,
            last_accessed_at,
            expires_at,
            is_archived,
            superseded_by,
            embedding_model,
            embedding,
        })
    }

    /// Log an audit event
    async fn log_audit(
        &self,
        operation: &str,
        memory_id: Option<MemoryId>,
        metadata: serde_json::Value,
    ) -> Result<()> {
        let conn = self.get_conn()?;

        let memory_id_str = memory_id.map(|id| id.to_string());
        let metadata_json = metadata.to_string();

        conn.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES (?, ?, ?)",
            params![operation, memory_id_str, metadata_json],
        )
        .await?;

        Ok(())
    }

    /// Select graph-expansion seeds in a stable order.
    ///
    /// HashMap iteration is intentionally randomized per process. Using its
    /// first entries made the same query expand from different memories on
    /// different CLI invocations, which is both noisy and incorrect. Direct
    /// keyword/vector signal determines relevance; the ID tie-break makes
    /// equal-score candidates deterministic.
    fn select_graph_seed_ids(
        memory_scores: &HashMap<MemoryId, (f32, f32, f32, f32)>,
        limit: usize,
    ) -> Vec<MemoryId> {
        let mut candidates: Vec<(MemoryId, f32)> = memory_scores
            .iter()
            .map(|(id, (keyword, vector, _, _))| (*id, *keyword + *vector))
            .collect();
        candidates.sort_by(|(left_id, left_score), (right_id, right_score)| {
            right_score
                .partial_cmp(left_score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| left_id.to_string().cmp(&right_id.to_string()))
        });
        candidates
            .into_iter()
            .take(limit)
            .map(|(id, _)| id)
            .collect()
    }

    /// Build a safe, relevance-focused FTS5 query from natural language.
    fn build_fts_query(query: &str) -> String {
        crate::utils::retrieval::rewrite_fts_query(query).fts_query
    }

    /// Escape one FTS5 query token as a literal term.
    ///
    /// FTS5 treats punctuation and words such as `AND`, `OR`, and `NOT` as
    /// syntax. Quoting every token both preserves natural-language queries
    /// (including apostrophes and trailing punctuation) and prevents query
    /// text from injecting FTS operators. FTS5 still tokenizes quoted text,
    /// so the configured porter tokenizer can stem the term normally.
    fn escape_fts5_query(term: &str) -> String {
        let escaped = term.replace('"', "\"\"");
        format!("\"{}\"", escaped)
    }

    /// Set the embedding service for this storage backend
    pub fn set_embedding_service(&mut self, service: Arc<dyn EmbeddingService>) {
        self.embedding_service = Some(service);
    }

    async fn is_raw_turn(&self, memory_id: &MemoryId) -> Result<bool> {
        let conn = self.get_conn()?;
        // Tags cover legacy/raw callers that predate typed provenance.
        let mut tagged = conn
            .query(
                "SELECT 1 FROM memories WHERE id = ? AND tags LIKE '%\"turn_sync\"%' LIMIT 1",
                params![memory_id.to_string()],
            )
            .await?;
        if tagged.next().await?.is_some() {
            return Ok(true);
        }
        let mut rows = match conn.query(
            "SELECT 1 FROM memory_provenance WHERE memory_id = ? AND source_kind = 'turn' AND source_memory_id IS NULL LIMIT 1",
            params![memory_id.to_string()],
        ).await {
            Ok(rows) => rows,
            // Compatibility with databases created before provenance was added.
            Err(_) => return Ok(false),
        };
        Ok(rows.next().await?.is_some())
    }

    /// Generate and store embedding for a memory
    ///
    /// This method generates an embedding for the given memory content and stores it
    /// in the memory_vectors table. If embeddings are disabled (no service), this is a no-op.
    ///
    /// # Arguments
    /// * `memory_id` - The ID of the memory to embed
    /// * `content` - The text content to embed
    ///
    /// # Returns
    /// * `Ok(())` - Embedding generated and stored successfully (or disabled)
    /// * `Err(MnemosyneError)` - If embedding generation or storage fails
    pub async fn generate_and_store_embedding(
        &self,
        memory_id: &MemoryId,
        content: &str,
    ) -> Result<()> {
        // Raw captured turns are transcript anchors, never ranked vectors.
        if self.is_raw_turn(memory_id).await? {
            return Ok(());
        }
        // Skip if no embedding service configured
        let service = match &self.embedding_service {
            Some(s) => s,
            None => {
                debug!(
                    "Embedding service not configured, skipping embedding for {}",
                    memory_id
                );
                return Ok(());
            }
        };

        // Generate embedding
        debug!("Generating embedding for memory: {}", memory_id);
        let embedding = service.embed(content).await?;

        // Store in memory_vectors table
        self.store_embedding(memory_id, &embedding).await?;

        info!(
            "Successfully generated and stored embedding for memory: {}",
            memory_id
        );
        Ok(())
    }

    /// Store an embedding vector in the memory_vectors table
    ///
    /// This is a low-level method that directly stores a pre-computed embedding.
    /// Use generate_and_store_embedding() for the high-level workflow.
    ///
    /// # Arguments
    /// * `memory_id` - The ID of the memory
    /// * `embedding` - The embedding vector (must match configured dimensions)
    pub async fn store_embedding(&self, memory_id: &MemoryId, embedding: &[f32]) -> Result<()> {
        // Keep the low-level path safe too: CLI/backfill callers must not
        // accidentally embed a raw turn.
        if self.is_raw_turn(memory_id).await? {
            return Ok(());
        }
        let conn = self.get_conn()?;
        let mut memory_rows = conn
            .query(
                "SELECT 1 FROM memories WHERE id = ? LIMIT 1",
                params![memory_id.to_string()],
            )
            .await?;
        if memory_rows.next().await?.is_none() {
            return Err(MnemosyneError::MemoryNotFound(memory_id.to_string()));
        }
        drop(memory_rows);
        if self.schema_type == SchemaType::LibSQL {
            let bytes: Vec<u8> = embedding
                .iter()
                .flat_map(|value| value.to_le_bytes())
                .collect();
            conn.execute(
                "UPDATE memories SET embedding = ? WHERE id = ?",
                params![bytes, memory_id.to_string()],
            )
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to store embedding: {}", e)))?;
        } else {
            // StandardSQLite stores the raw f32 bytes plus their dimension in
            // its companion table.
            let bytes: Vec<u8> = embedding
                .iter()
                .flat_map(|value| value.to_le_bytes())
                .collect();
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, dimension) VALUES (?, ?, ?)",
                params![memory_id.to_string(), bytes, embedding.len() as i64],
            )
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to store embedding: {}", e)))?;
        }
        Ok(())
    }

    /// Retrieve embedding for a memory
    ///
    /// # Arguments
    /// * `memory_id` - The ID of the memory
    ///
    /// # Returns
    /// * `Ok(Some(Vec<f32>))` - The embedding vector if it exists
    /// * `Ok(None)` - If no embedding exists for this memory
    /// * `Err(MnemosyneError)` - If retrieval fails
    pub async fn get_embedding(&self, memory_id: &MemoryId) -> Result<Option<Vec<f32>>> {
        if self.schema_type == SchemaType::LibSQL {
            return Ok(StorageBackend::get_memory(self, *memory_id)
                .await?
                .embedding);
        }
        let conn = self.get_conn()?;
        let row = conn
            .query(
                "SELECT embedding FROM memory_embeddings WHERE memory_id = ?",
                params![memory_id.to_string()],
            )
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to retrieve embedding: {}", e)))?
            .next()
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to get embedding row: {}", e)))?;
        match row {
            Some(row) => Ok(Some(decode_embedding_from_row(&row, 0)?)),
            None => Ok(None),
        }
    }

    /// Delete embedding for a memory
    ///
    /// # Arguments
    /// * `memory_id` - The ID of the memory
    pub async fn delete_embedding(&self, memory_id: &MemoryId) -> Result<()> {
        let conn = self.get_conn()?;
        let (sql, error) = if self.schema_type == SchemaType::LibSQL {
            (
                "UPDATE memories SET embedding = NULL WHERE id = ?",
                "Failed to delete embedding from memories",
            )
        } else {
            (
                "DELETE FROM memory_embeddings WHERE memory_id = ?",
                "Failed to delete embedding from memory_embeddings",
            )
        };
        conn.execute(sql, params![memory_id.to_string()])
            .await
            .map_err(|e| MnemosyneError::Database(format!("{}: {}", error, e)))?;
        Ok(())
    }

    /// Set the search configuration
    pub fn set_search_config(&mut self, config: crate::config::SearchConfig) {
        self.search_config = config;
    }

    /// True delete: purge a memory from the store, embeddings, link graph,
    /// FTS index, and audit trail. Unlike [`archive_memory`](Self::archive_memory)
    /// this is unrecoverable — "your memory is yours" means forget must mean forget.
    pub async fn purge_memory(&self, memory_id: &MemoryId) -> Result<PurgeReport> {
        let id_str = memory_id.to_string();
        debug!("Purging memory {} (true delete)", id_str);
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut report = PurgeReport {
            memory_id: id_str.clone(),
            ..Default::default()
        };

        // Foreign-key enforcement is connection-local and older databases may
        // have it disabled, so discover and remove every owned projection
        // explicitly inside this transaction.
        let mut table_rows = tx
            .query(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')",
                params![],
            )
            .await?;
        let mut tables = Vec::new();
        while let Some(row) = table_rows.next().await? {
            tables.push(row.get::<String>(0)?);
        }
        drop(table_rows);
        let has = |name: &str| tables.iter().any(|table| table == name);

        if self.schema_type == SchemaType::LibSQL && has("memories") {
            let mut rows = tx
                .query(
                    "SELECT embedding IS NOT NULL FROM memories WHERE id = ?",
                    params![id_str.as_str()],
                )
                .await?;
            if let Some(row) = rows.next().await? {
                report.embedding_removed |= row.get::<i64>(0).unwrap_or(0) != 0;
            }
        }
        if has("memory_vectors") {
            report.embedding_removed |= tx
                .execute(
                    "DELETE FROM memory_vectors WHERE memory_id = ?",
                    params![id_str.as_str()],
                )
                .await?
                > 0;
        }
        if has("memory_embeddings") {
            report.embedding_removed |= tx
                .execute(
                    "DELETE FROM memory_embeddings WHERE memory_id = ?",
                    params![id_str.as_str()],
                )
                .await?
                > 0;
        }

        if has("memory_links") {
            report.links_removed = tx
                .execute(
                    "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                    params![id_str.as_str(), id_str.as_str()],
                )
                .await?;
        }
        report.supersession_refs_cleared = tx
            .execute(
                "UPDATE memories SET superseded_by = NULL WHERE superseded_by = ?",
                params![id_str.as_str()],
            )
            .await?;

        if has("interaction_policy_evidence") {
            tx.execute(
                "DELETE FROM interaction_policy_evidence WHERE policy_memory_id = ? OR source_memory_id = ?",
                params![id_str.as_str(), id_str.as_str()],
            )
            .await?;
        }
        if has("interaction_policies") {
            tx.execute(
                "DELETE FROM interaction_policies WHERE policy_memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("memory_provenance") {
            tx.execute(
                "UPDATE memory_provenance SET source_memory_id = NULL WHERE source_memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
            tx.execute(
                "DELETE FROM memory_provenance WHERE memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("memory_entities") {
            tx.execute(
                "DELETE FROM memory_entities WHERE memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("memory_change_proposals") {
            // A proposal's evidence is stored as JSON source IDs. Remove any
            // proposal that relied on the purged memory, not only proposals
            // targeting it, so it cannot later apply orphaned provenance.
            let source_pattern = format!("%\"{}\"%", id_str);
            tx.execute(
                "DELETE FROM memory_change_proposals WHERE source_memory_ids LIKE ?",
                params![source_pattern],
            )
            .await?;
            tx.execute(
                "DELETE FROM memory_change_proposals WHERE target_memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("turn_learning_claims") {
            tx.execute(
                "DELETE FROM turn_learning_claims WHERE source_memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("turn_learning_claim_owners") {
            tx.execute(
                "DELETE FROM turn_learning_claim_owners WHERE source_memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("memory_modification_log") {
            tx.execute(
                "DELETE FROM memory_modification_log WHERE memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        // Older LibSQL databases used this legacy audit table.  It is not in
        // the current schema, but true deletion must clean it when present.
        if has("memory_modifications") {
            tx.execute(
                "DELETE FROM memory_modifications WHERE memory_id = ?",
                params![id_str.as_str()],
            )
            .await?;
        }
        if has("audit_log") {
            report.audit_rows_removed = tx
                .execute(
                    "DELETE FROM audit_log WHERE memory_id = ?",
                    params![id_str.as_str()],
                )
                .await?;
        }

        let deleted = tx
            .execute(
                "DELETE FROM memories WHERE id = ?",
                params![id_str.as_str()],
            )
            .await?;
        if deleted == 0 {
            return Err(MnemosyneError::MemoryNotFound(id_str));
        }
        tx.commit().await?;
        report.fts_removed = true;

        info!(
            "Purged memory {}: {} links, {} audit rows, embedding={}, fts=true",
            id_str, report.links_removed, report.audit_rows_removed, report.embedding_removed
        );
        Ok(report)
    }

    /// Temporal retrieval: keyword search that returns what was true as of
    /// `as_of`, using the supersedence timeline instead of the archive flag.
    ///
    /// A memory is valid at `as_of` when:
    /// - it existed then (`created_at <= as_of`),
    /// - it was not superseded by a successor already in force at `as_of`,
    /// - it was not soft-archived before `as_of` (`archived_at`).
    /// Unlike [`StorageBackend::keyword_search`] this deliberately includes
    /// archived/superseded rows — history must stay queryable.
    pub async fn keyword_search_as_of(
        &self,
        query: &str,
        namespace: &Namespace,
        as_of: chrono::DateTime<Utc>,
        limit: usize,
    ) -> Result<Vec<SearchResult>> {
        let ns_json = serde_json::to_string(namespace).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize namespace: {}", e))
        })?;
        let conn = self.get_conn()?;

        // The LibSQL schema lacks `archived_at` (added by sqlite migration 007);
        // degrade gracefully by dropping the archive-time condition there.
        let has_archived_at = match conn
            .query(
                "SELECT COUNT(*) FROM pragma_table_info('memories') WHERE name='archived_at'",
                (),
            )
            .await
        {
            Ok(mut rows) => match rows.next().await {
                Ok(Some(row)) => row.get::<i64>(0).unwrap_or(0) > 0,
                _ => false,
            },
            Err(_) => false,
        };

        let fts_filter = if query.trim().is_empty() {
            String::new()
        } else {
            let fts_query = if query.contains(' ') {
                query
                    .split_whitespace()
                    .map(Self::escape_fts5_query)
                    .collect::<Vec<String>>()
                    .join(" OR ")
            } else {
                Self::escape_fts5_query(query)
            };
            format!(
                "AND m.rowid IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH '{}') ",
                fts_query.replace('\'', "''")
            )
        };

        let archive_filter = if has_archived_at {
            "AND (m.archived_at IS NULL OR m.archived_at > ?) "
        } else {
            ""
        };
        let sql = format!(
            r#"
            SELECT {columns} FROM memories m
            WHERE m.namespace = ?
              AND {knowledge_filter}
            {fts_filter}
              AND m.created_at <= ?
              AND (
                    m.superseded_by IS NULL
                    OR EXISTS (
                        SELECT 1 FROM memories s
                        WHERE s.id = m.superseded_by AND s.created_at > ?
                    )
                  )
              {archive_filter}
            ORDER BY m.importance DESC, m.created_at DESC
            LIMIT {limit}
            "#,
            columns = self.memory_columns("m"),
            knowledge_filter = self.knowledge_predicate("m"),
            limit = limit as i64
        );

        let params_vec: Vec<libsql::Value> = if has_archived_at {
            vec![
                ns_json.clone().into(),
                as_of.to_rfc3339().into(),
                as_of.to_rfc3339().into(),
                as_of.timestamp().into(),
            ]
        } else {
            vec![
                ns_json.clone().into(),
                as_of.to_rfc3339().into(),
                as_of.to_rfc3339().into(),
            ]
        };

        let mut rows = conn.query(&sql, params_vec).await?;
        let mut results = Vec::new();
        while let Some(row) = rows.next().await? {
            let memory = self.row_to_memory(&row).await?;
            results.push(SearchResult {
                memory,
                score: 0.8,
                match_reason: "keyword_match_as_of".to_string(),
            });
        }
        Ok(results)
    }

    /// Resolve a content-hash deduplicated write to its active canonical row.
    /// This is useful to high-level callers whose legacy store API returns the
    /// requested ID even when the integrity gate merged it into a parent.
    pub async fn find_memory_by_content(
        &self,
        namespace: &Namespace,
        content: &str,
        memory_class: MemoryClass,
    ) -> Result<Option<MemoryId>> {
        let conn = self.get_conn()?;
        let namespace = serde_json::to_string(namespace)?;
        let memory_class = serde_json::to_value(memory_class)?
            .as_str()
            .unwrap_or("knowledge")
            .to_string();
        let mut rows = conn
            .query(
                "SELECT id FROM memories WHERE namespace = ? AND memory_class = ? AND content_hash = ? AND is_archived = 0 ORDER BY updated_at DESC LIMIT 1",
                params![namespace, memory_class, content_hash(content)],
            )
            .await?;
        Ok(rows
            .next()
            .await?
            .map(|row| row.get::<String>(0))
            .transpose()?
            .map(|id| MemoryId::from_string(&id))
            .transpose()?)
    }

    /// Find candidate memories for a "forget X" cascade: case-insensitive
    /// substring match against content/summary/keywords/tags/context within a
    /// namespace, INCLUDING archived and superseded rows — forgetting is about
    /// removal, not just hiding.
    pub async fn find_purge_candidates(
        &self,
        namespace: &Namespace,
        needle: &str,
        limit: usize,
    ) -> Result<Vec<MemoryId>> {
        if needle.trim().is_empty() {
            return Ok(vec![]);
        }
        let conn = self.get_conn()?;
        // Namespace is stored as its JSON serialization (see store_memory).
        let ns_json = serde_json::to_string(namespace).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize namespace: {}", e))
        })?;
        let pattern = format!("%{}%", needle.replace('%', "\\%").replace('_', "\\_"));
        let sql = format!(
            r#"
            SELECT id FROM memories
            WHERE namespace = ?
              AND (content LIKE ? ESCAPE '\'
                   OR summary LIKE ? ESCAPE '\'
                   OR keywords LIKE ? ESCAPE '\'
                   OR tags LIKE ? ESCAPE '\'
                   OR context LIKE ? ESCAPE '\')
            ORDER BY created_at DESC
            LIMIT {}
            "#,
            limit as i64
        );
        let mut rows = conn
            .query(
                &sql,
                params![
                    ns_json.as_str(),
                    pattern.as_str(),
                    pattern.as_str(),
                    pattern.as_str(),
                    pattern.as_str(),
                    pattern.as_str(),
                ],
            )
            .await?;
        let mut ids = Vec::new();
        while let Some(row) = rows.next().await? {
            let id: String = row.get(0)?;
            ids.push(MemoryId::from_string(&id)?);
        }
        Ok(ids)
    }

    /// Perform vector similarity search
    ///
    /// Searches for memories with embeddings similar to the query embedding.
    /// Uses sqlite-vec's vec_distance_cosine for similarity.
    ///
    /// # Arguments
    /// * `query_embedding` - The query embedding vector
    /// * `limit` - Maximum number of results
    /// * `namespace` - Optional namespace filter
    ///
    /// # Returns
    /// * Vector of (MemoryId, similarity_score) tuples, sorted by similarity (desc)
    pub async fn vector_search(
        &self,
        query_embedding: &[f32],
        limit: usize,
        namespace: Option<Namespace>,
    ) -> Result<Vec<(MemoryId, f32)>> {
        // Skip if vector search is disabled
        if !self.search_config.enable_vector_search {
            debug!("Vector search disabled in config");
            return Ok(Vec::new());
        }
        if self.schema_type == SchemaType::StandardSQLite {
            return Ok(self
                .standard_vector_search(query_embedding, limit, namespace)
                .await?
                .into_iter()
                .map(|result| (result.memory.id, result.score))
                .collect());
        }

        let conn = self.get_conn()?;

        // Convert query embedding to JSON for libsql vector functions
        let query_json = serde_json::to_string(query_embedding)?;

        // Build query using native libsql vector functions (no vec0 extension needed)
        // Queries the memories table's embedding column (F32_BLOB)
        let sql = if namespace.is_some() {
            r#"
            SELECT id, vector_distance_cos(embedding, vector32(?)) as distance
            FROM memories
            WHERE embedding IS NOT NULL
              AND is_archived = 0
              AND memory_class = 'knowledge'
              AND tags NOT LIKE '%\"turn_sync\"%'
              AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
              AND namespace = ?
            ORDER BY distance ASC
            LIMIT ?
            "#
            .to_string()
        } else {
            r#"
            SELECT id, vector_distance_cos(embedding, vector32(?)) as distance
            FROM memories
            WHERE embedding IS NOT NULL
              AND is_archived = 0
              AND memory_class = 'knowledge'
              AND tags NOT LIKE '%\"turn_sync\"%'
              AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
            ORDER BY distance ASC
            LIMIT ?
            "#
            .to_string()
        };

        let mut rows = if let Some(ns) = &namespace {
            let ns_json = serde_json::to_string(ns)?;
            conn.query(&sql, params![query_json, ns_json, limit as i64])
                .await?
        } else {
            conn.query(&sql, params![query_json, limit as i64]).await?
        };

        let mut results = Vec::new();
        while let Some(row) = rows.next().await? {
            let memory_id_str: String = row.get(0)?;
            let distance: f64 = row.get(1)?;

            // Convert distance to similarity (0 = identical, 2 = opposite)
            // Similarity = 1 - (distance / 2), range [0, 1]
            let similarity = 1.0 - (distance as f32 / 2.0);

            let memory_id = MemoryId(uuid::Uuid::parse_str(&memory_id_str)?);
            results.push((memory_id, similarity));
        }

        debug!("Vector search found {} results", results.len());
        Ok(results)
    }

    // ========================================================================
    // Evolution System Methods
    // ========================================================================

    /// List all active (non-archived) memories for evolution jobs
    pub async fn list_all_active(&self, limit: Option<usize>) -> Result<Vec<MemoryNote>> {
        debug!("Listing all active memories for evolution");

        let conn = self.get_conn()?;
        let sql = if let Some(lim) = limit {
            format!(
                "SELECT {} FROM memories WHERE is_archived = 0 AND memory_class = 'knowledge' AND tags NOT LIKE '%\"turn_sync\"%' AND (expires_at IS NULL OR datetime(expires_at) > datetime('now')) AND archived_at IS NULL ORDER BY created_at DESC LIMIT {}",
                self.memory_columns(""),
                lim
            )
        } else {
            format!(
                "SELECT {} FROM memories WHERE is_archived = 0 AND memory_class = 'knowledge' AND tags NOT LIKE '%\"turn_sync\"%' AND (expires_at IS NULL OR datetime(expires_at) > datetime('now')) AND archived_at IS NULL ORDER BY created_at DESC",
                self.memory_columns("")
            )
        };

        let mut rows = conn.query(&sql, params![]).await?;
        let mut memories = Vec::new();

        while let Some(row) = rows.next().await? {
            memories.push(self.row_to_memory(&row).await?);
        }

        debug!("Listed {} active memories", memories.len());
        Ok(memories)
    }

    /// Update the importance score of a memory
    pub async fn update_importance(&self, memory_id: &MemoryId, new_importance: f32) -> Result<()> {
        debug!(
            "Updating importance for memory {} to {}",
            memory_id, new_importance
        );

        let conn = self.get_conn()?;
        conn.execute(
            r#"
            UPDATE memories
            SET importance = ?,
                updated_at = ?
            WHERE id = ?
            "#,
            params![
                new_importance as f64,
                Utc::now().to_rfc3339(),
                memory_id.to_string()
            ],
        )
        .await?;

        Ok(())
    }

    /// Find memories that are candidates for archival
    pub async fn find_archival_candidates(&self, limit: usize) -> Result<Vec<MemoryNote>> {
        debug!("Finding archival candidates (limit: {})", limit);

        let conn = self.get_conn()?;

        // Use the view from migration 007
        let sql = format!(
            r#"
            SELECT {columns}
            FROM memories m
            WHERE m.archived_at IS NULL AND m.is_archived = 0
              AND (
                (m.access_count = 0 AND
                 julianday('now') - julianday(m.created_at) > 180) OR
                (m.importance < 3.0 AND
                 julianday('now') - julianday(COALESCE(m.last_accessed_at, m.created_at)) > 90) OR
                (m.importance < 2.0 AND
                 julianday('now') - julianday(COALESCE(m.last_accessed_at, m.created_at)) > 30)
              )
            ORDER BY m.importance ASC, m.access_count ASC
            LIMIT ?
        "#,
            columns = self.memory_columns("m")
        );

        let mut rows = conn.query(&sql, params![limit as i64]).await?;
        let mut candidates = Vec::new();

        while let Some(row) = rows.next().await? {
            candidates.push(self.row_to_memory(&row).await?);
        }

        debug!("Found {} archival candidates", candidates.len());
        Ok(candidates)
    }

    /// Archive a memory by setting archived_at timestamp
    pub async fn archive_memory_with_timestamp(&self, memory_id: &MemoryId) -> Result<()> {
        debug!("Archiving memory with timestamp: {}", memory_id);

        let conn = self.get_conn()?;
        let now = Utc::now();

        conn.execute(
            r#"
            UPDATE memories
            SET archived_at = ?,
                is_archived = 1,
                updated_at = ?
            WHERE id = ?
            "#,
            params![now.timestamp(), now.to_rfc3339(), memory_id.to_string()],
        )
        .await?;
        if connection_has_column(&conn, "memory_facts", "memory_id").await? {
            conn.execute(
                "UPDATE memory_facts SET is_active = 0 WHERE memory_id = ?",
                params![memory_id.to_string()],
            )
            .await?;
        }

        Ok(())
    }

    /// Unarchive a memory
    pub async fn unarchive_memory(&self, memory_id: &MemoryId) -> Result<()> {
        debug!("Unarchiving memory: {}", memory_id);

        let conn = self.get_conn()?;

        conn.execute(
            r#"
            UPDATE memories
            SET archived_at = NULL,
                is_archived = 0,
                updated_at = ?
            WHERE id = ?
            "#,
            params![Utc::now().to_rfc3339(), memory_id.to_string()],
        )
        .await?;

        Ok(())
    }

    /// Mark a memory as superseded by another memory
    ///
    /// This archives the old memory and records which memory supersedes it.
    /// Used during consolidation when multiple similar memories are merged.
    ///
    /// # Arguments
    /// * `superseded_id` - The memory being superseded (will be archived)
    /// * `superseding_id` - The memory that supersedes the old one
    pub async fn mark_superseded(
        &self,
        superseded_id: &MemoryId,
        superseding_id: &MemoryId,
    ) -> Result<()> {
        debug!(
            "Marking memory {} as superseded by {}",
            superseded_id, superseding_id
        );

        let conn = self.get_conn()?;
        let now = Utc::now();

        // Update the superseded memory: archive it and record superseding memory
        conn.execute(
            r#"
            UPDATE memories
            SET is_archived = 1,
                superseded_by = ?,
                updated_at = ?
            WHERE id = ?
            "#,
            params![
                superseding_id.to_string(),
                now.to_rfc3339(),
                superseded_id.to_string()
            ],
        )
        .await?;
        if connection_has_column(&conn, "memory_facts", "memory_id").await? {
            conn.execute(
                "UPDATE memory_facts SET is_active = 0 WHERE memory_id = ?",
                params![superseded_id.to_string()],
            )
            .await?;
        }

        // Log the consolidation in audit log
        conn.execute(
            r#"
            INSERT INTO audit_log (operation, memory_id, metadata)
            VALUES ('supersede', ?, ?)
            "#,
            params![
                superseded_id.to_string(),
                serde_json::json!({
                    "superseded_by": superseding_id.to_string(),
                    "timestamp": now.to_rfc3339()
                })
                .to_string()
            ],
        )
        .await?;

        Ok(())
    }

    /// Record link traversal for decay tracking
    pub async fn record_link_traversal(
        &self,
        source_id: &MemoryId,
        target_id: &MemoryId,
    ) -> Result<()> {
        debug!("Recording link traversal: {} -> {}", source_id, target_id);

        let conn = self.get_conn()?;
        let now = Utc::now();

        conn.execute(
            r#"
            UPDATE memory_links
            SET last_traversed_at = ?
            WHERE source_id = ? AND target_id = ?
            "#,
            params![
                now.timestamp(),
                source_id.to_string(),
                target_id.to_string()
            ],
        )
        .await?;

        Ok(())
    }

    /// Update link strength (for reinforcement or decay)
    pub async fn update_link_strength(
        &self,
        source_id: &MemoryId,
        target_id: &MemoryId,
        new_strength: f32,
    ) -> Result<()> {
        debug!(
            "Updating link strength: {} -> {} = {}",
            source_id, target_id, new_strength
        );

        let conn = self.get_conn()?;

        conn.execute(
            r#"
            UPDATE memory_links
            SET strength = ?
            WHERE source_id = ? AND target_id = ?
            "#,
            params![new_strength, source_id.to_string(), target_id.to_string()],
        )
        .await?;

        Ok(())
    }

    /// Find links that need decay (untraversed for long time)
    /// Returns (source_id, link) tuples
    pub async fn find_link_decay_candidates(
        &self,
        days_threshold: i64,
        limit: usize,
    ) -> Result<Vec<(MemoryId, MemoryLink)>> {
        debug!(
            "Finding link decay candidates (threshold: {} days, limit: {})",
            days_threshold, limit
        );

        let conn = self.get_conn()?;

        let sql = r#"
            SELECT ml.source_id, ml.target_id, ml.link_type, ml.strength, ml.created_at, ml.reason,
                   ml.last_traversed_at, ml.user_created
            FROM memory_links ml
            LEFT JOIN memories source_memory ON source_memory.id = ml.source_id
            LEFT JOIN memories target_memory ON target_memory.id = ml.target_id
            WHERE ml.user_created = 0
              AND ml.strength > 0.1
              AND (
                source_memory.id IS NULL OR
                target_memory.id IS NULL OR
                COALESCE(source_memory.is_archived, 0) != 0 OR
                COALESCE(target_memory.is_archived, 0) != 0 OR
                (ml.last_traversed_at IS NULL AND
                 julianday('now') - julianday(
                     CASE WHEN typeof(ml.created_at) IN ('integer', 'real')
                          THEN datetime(ml.created_at, 'unixepoch')
                          ELSE ml.created_at END
                 ) > ?) OR
                (ml.last_traversed_at IS NOT NULL AND
                 julianday('now') - julianday(
                     CASE WHEN typeof(ml.last_traversed_at) IN ('integer', 'real')
                          THEN datetime(ml.last_traversed_at, 'unixepoch')
                          ELSE ml.last_traversed_at END
                 ) > ?)
              )
            ORDER BY strength ASC
            LIMIT ?
        "#;

        let mut rows = conn
            .query(sql, params![days_threshold, days_threshold, limit as i64])
            .await?;

        let mut links = Vec::new();
        while let Some(row) = rows.next().await? {
            let source_id_str: String = row.get(0)?;
            let source_id = MemoryId::from_string(&source_id_str)?;

            let target_id_str: String = row.get(1)?;
            let target_id = MemoryId::from_string(&target_id_str)?;

            let link_type_str: String = row.get(2)?;
            let link_type = match link_type_str.as_str() {
                "extends" => crate::types::LinkType::Extends,
                "contradicts" => crate::types::LinkType::Contradicts,
                "implements" => crate::types::LinkType::Implements,
                "references" => crate::types::LinkType::References,
                "supersedes" => crate::types::LinkType::Supersedes,
                _ => continue,
            };

            let strength: f64 = row.get(3)?;
            let created_at_str: String = row.get(4)?;
            let created_at = chrono::DateTime::parse_from_rfc3339(&created_at_str)
                .ok()
                .map(|dt| dt.with_timezone(&Utc))
                .unwrap_or_else(Utc::now);

            let reason: String = row
                .get::<String>(5)
                .unwrap_or_else(|_| String::from("link decay candidate"));

            // Parse last_traversed_at from either legacy Unix seconds or
            // RFC3339 text; both forms exist across schema generations.
            let last_traversed_at = parse_datetime_from_row(&row, 6);

            // Parse user_created (boolean stored as integer)
            let user_created = row.get::<i64>(7).unwrap_or(0) != 0;

            links.push((
                source_id,
                MemoryLink {
                    target_id,
                    link_type,
                    strength: strength as f32,
                    reason,
                    created_at,
                    last_traversed_at,
                    user_created,
                },
            ));
        }

        debug!("Found {} link decay candidates", links.len());
        Ok(links)
    }

    /// Remove a weak link
    pub async fn remove_link(&self, source_id: &MemoryId, target_id: &MemoryId) -> Result<()> {
        debug!("Removing link: {} -> {}", source_id, target_id);

        let conn = self.get_conn()?;

        conn.execute(
            r#"
            DELETE FROM memory_links
            WHERE source_id = ? AND target_id = ?
            "#,
            params![source_id.to_string(), target_id.to_string()],
        )
        .await?;

        Ok(())
    }

    /// Count incoming links to a memory
    ///
    /// Returns the number of memories that link TO this memory.
    /// This is useful for importance scoring - memories referenced by many others are more important.
    pub async fn count_incoming_links(&self, memory_id: &MemoryId) -> Result<usize> {
        let conn = self.get_conn()?;

        let mut rows = conn
            .query(
                "SELECT COUNT(*) FROM memory_links WHERE target_id = ?",
                params![memory_id.to_string()],
            )
            .await?;

        if let Some(row) = rows.next().await? {
            let count: i64 = row.get(0)?;
            Ok(count as usize)
        } else {
            Ok(0)
        }
    }

    /// Get memory access statistics
    pub async fn get_access_stats(
        &self,
        memory_id: &MemoryId,
    ) -> Result<(u32, Option<chrono::DateTime<Utc>>)> {
        let conn = self.get_conn()?;

        let mut rows = conn
            .query(
                "SELECT access_count, last_accessed_at FROM memories WHERE id = ?",
                params![memory_id.to_string()],
            )
            .await?;

        if let Some(row) = rows.next().await? {
            let access_count: i64 = row.get(0)?;
            let last_accessed_at = if let Ok(last_accessed_str) = row.get::<String>(1) {
                chrono::DateTime::parse_from_rfc3339(&last_accessed_str)
                    .ok()
                    .map(|dt| dt.with_timezone(&Utc))
            } else {
                None
            };

            Ok((access_count as u32, last_accessed_at))
        } else {
            Err(MnemosyneError::MemoryNotFound(memory_id.to_string()))
        }
    }

    async fn graph_traverse_with_limit(
        &self,
        seed_ids: &[MemoryId],
        max_hops: usize,
        namespace: Option<Namespace>,
        max_results: Option<usize>,
    ) -> Result<Vec<MemoryNote>> {
        debug!(
            "Graph traverse from {} seeds, max {} hops, namespace: {:?}, result limit: {:?}",
            seed_ids.len(),
            max_hops,
            namespace,
            max_results
        );

        if seed_ids.is_empty() || max_hops == 0 || max_results == Some(0) {
            return Ok(vec![]);
        }
        if seed_ids.len() > MAX_GRAPH_SEEDS {
            return Err(MnemosyneError::ValidationError(format!(
                "seed_ids must not contain more than {} IDs",
                MAX_GRAPH_SEEDS
            )));
        }

        let seed_strings: Vec<String> = seed_ids.iter().map(|id| id.to_string()).collect();
        let placeholders = seed_strings
            .iter()
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(",");
        let limit_clause = if max_results.is_some() { "LIMIT ?" } else { "" };
        let traversal_limit_clause = if max_results.is_some() { "LIMIT ?" } else { "" };

        let namespace_filter = if namespace.is_some() {
            "AND m.namespace = ?"
        } else {
            ""
        };

        let sql = format!(
            r#"
            WITH RECURSIVE graph_walk(memory_id, depth) AS (
                SELECT id, 0 FROM memories WHERE id IN ({placeholders})
                UNION
                SELECT
                    CASE
                        WHEN ml.source_id = gw.memory_id THEN ml.target_id
                        ELSE ml.source_id
                    END as memory_id,
                    gw.depth + 1
                FROM graph_walk gw
                JOIN memory_links ml ON (
                    ml.source_id = gw.memory_id OR ml.target_id = gw.memory_id
                )
                WHERE gw.depth < ?
                {traversal_limit_clause}
            )
            SELECT DISTINCT {columns}
            FROM memories m
            JOIN graph_walk gw ON m.id = gw.memory_id
            WHERE gw.depth > 0
              AND m.is_archived = 0
              AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now'))
              AND {knowledge_filter} {namespace_filter}
            ORDER BY gw.depth, m.importance DESC
            {limit_clause}
            "#,
            placeholders = placeholders,
            namespace_filter = namespace_filter,
            knowledge_filter = self.knowledge_predicate("m"),
            limit_clause = limit_clause,
            traversal_limit_clause = traversal_limit_clause,
            columns = self.memory_columns("m")
        );

        let conn = self.get_conn()?;
        let mut param_values: Vec<libsql::Value> = seed_strings
            .iter()
            .map(|s| libsql::Value::Text(s.clone()))
            .collect();
        param_values.push(libsql::Value::Integer(max_hops as i64));
        if let Some(limit) = max_results {
            let traversal_budget = limit.saturating_mul(max_hops.saturating_add(1)).min(10_000);
            param_values.push(libsql::Value::Integer(traversal_budget as i64));
        }

        if let Some(ns) = namespace {
            let ns_json = serde_json::to_string(&ns)?;
            param_values.push(libsql::Value::Text(ns_json));
        }
        if let Some(limit) = max_results {
            param_values.push(libsql::Value::Integer(limit.min(1000) as i64));
        }

        let mut rows = conn
            .query(&sql, libsql::params_from_iter(param_values))
            .await?;

        let mut results = Vec::new();
        while let Some(row) = rows.next().await? {
            results.push(self.row_to_memory(&row).await?);
        }

        drop(rows);
        // Treat graph expansion as a traversal signal for adjacent edges. The
        // update is best-effort for compact legacy schemas without traversal
        // columns, and runs only after the result cursor is closed.
        if connection_has_column(&conn, "memory_links", "last_traversed_at").await?
            && connection_has_column(&conn, "memory_links", "user_created").await?
        {
            let placeholders = seed_strings
                .iter()
                .map(|_| "?")
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "UPDATE memory_links SET last_traversed_at = ? WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})"
            );
            let mut values = vec![libsql::Value::Integer(Utc::now().timestamp())];
            values.extend(
                seed_strings
                    .iter()
                    .map(|id| libsql::Value::Text(id.clone())),
            );
            values.extend(
                seed_strings
                    .iter()
                    .map(|id| libsql::Value::Text(id.clone())),
            );
            conn.execute(&sql, libsql::params_from_iter(values)).await?;
        }

        debug!("Graph traversal found {} memories", results.len());
        Ok(results)
    }
}

/// One durable captured turn. Transcript rows are searchable audit data, not
/// recallable memories and never receive embeddings.
#[derive(Debug, Clone, serde::Serialize)]
pub struct SessionTranscriptRecord {
    pub id: MemoryId,
    pub namespace: Namespace,
    pub source_memory_id: MemoryId,
    pub session_id: Option<String>,
    pub turn_id: Option<String>,
    pub user_text: String,
    pub assistant_text: String,
    pub content: String,
    pub observed_at: chrono::DateTime<Utc>,
    pub valid_from: chrono::DateTime<Utc>,
    pub valid_until: Option<chrono::DateTime<Utc>>,
    pub created_at: chrono::DateTime<Utc>,
}

/// A derived memory plus its typed entity records for atomic turn learning.
#[derive(Debug, Clone)]
pub struct LearningMemory {
    pub memory: MemoryNote,
    pub entities: Vec<MemoryEntity>,
}

/// Durable state for one reasoning memory item. The underlying note remains a
/// normal knowledge memory so existing APIs and migrations remain compatible;
/// the companion table supplies the outcome-aware metadata.
#[derive(Debug, Clone)]
pub struct ReasoningMemoryRecord {
    pub memory: ReasoningMemory,
    pub entities: Vec<MemoryEntity>,
}

/// Durable state for one bounded memory-maintenance run.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MaintenanceRunRecord {
    pub id: String,
    pub idempotency_key: String,
    pub job_kind: String,
    pub namespace: Option<String>,
    pub status: String,
    pub started_at: String,
    pub completed_at: Option<String>,
    pub item_limit: usize,
    pub retry_limit: usize,
    pub timeout_ms: u64,
    pub stale_after_days: i64,
    pub attempts: usize,
    pub items_processed: usize,
    pub findings_count: usize,
    pub errors_count: usize,
    pub report_json: Option<String>,
    pub error_message: Option<String>,
}

/// Durable state for one owner-routed interaction-policy proposal.
#[derive(Debug, Clone, serde::Serialize)]
pub struct InteractionPolicyProposalRecord {
    pub id: String,
    pub namespace: String,
    pub source_memory_id: String,
    pub source_revision: String,
    pub polarity: String,
    pub guidance: String,
    pub applicability: String,
    pub signal: String,
    pub confidence: f32,
    pub anchors: String,
    pub evidence_quote: String,
    pub proposer: String,
    pub owner: String,
    pub status: String,
    pub created_at: String,
    pub reviewed_by: Option<String>,
    pub decided_at: Option<String>,
    pub decision_note: Option<String>,
    pub applied_at: Option<String>,
    pub error_message: Option<String>,
}

/// Durable state for one owner-routed memory change proposal.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MemoryProposalRecord {
    pub id: String,
    pub namespace: String,
    pub target_memory_id: String,
    pub base_updated_at: String,
    pub before_content: String,
    pub proposed_content: String,
    pub diff_text: String,
    pub source_memory_ids: String,
    pub source_revisions: String,
    pub evidence_quotes: String,
    pub proposer: String,
    pub owner: String,
    pub status: String,
    pub created_at: String,
    pub reviewed_by: Option<String>,
    pub decided_at: Option<String>,
    pub decision_note: Option<String>,
    pub applied_at: Option<String>,
    pub error_message: Option<String>,
}

/// Durable owner-reviewed constraint proposal. Approved rows are read by the
/// bootstrap layer; proposed/rejected rows never enter startup context.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ConstraintProposalRecord {
    pub id: String,
    pub namespace: String,
    pub text: String,
    pub scope: String,
    pub priority: u8,
    pub valid_until: Option<String>,
    pub source_memory_ids: String,
    pub evidence_quotes: String,
    pub proposer: String,
    pub owner: String,
    pub status: String,
    pub created_at: String,
    pub approved_by: Option<String>,
    pub decided_at: Option<String>,
    pub decision_note: Option<String>,
}

impl LibsqlStorage {
    async fn standard_vector_search(
        &self,
        query_embedding: &[f32],
        limit: usize,
        namespace: Option<Namespace>,
    ) -> Result<Vec<SearchResult>> {
        let conn = self.get_conn()?;
        let columns = self.memory_columns("m");
        let sql = if namespace.is_some() {
            format!(
                "SELECT {columns}, e.embedding FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id WHERE m.namespace = ? AND m.is_archived = 0 AND m.memory_class = 'knowledge' AND m.tags NOT LIKE '%\"turn_sync\"%' AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now'))",
                columns = columns
            )
        } else {
            format!(
                "SELECT {columns}, e.embedding FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id WHERE m.is_archived = 0 AND m.memory_class = 'knowledge' AND m.tags NOT LIKE '%\"turn_sync\"%' AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now'))",
                columns = columns
            )
        };
        let mut rows = if let Some(namespace) = namespace {
            conn.query(&sql, params![serde_json::to_string(&namespace)?])
                .await?
        } else {
            conn.query(&sql, params![]).await?
        };
        let mut results = Vec::new();
        while let Some(row) = rows.next().await? {
            let embedding = match decode_embedding_from_row(&row, 21) {
                Ok(embedding) => embedding,
                Err(_) => continue,
            };
            if embedding.len() != query_embedding.len() {
                continue;
            }
            let score = crate::embeddings::cosine_similarity(query_embedding, &embedding);
            let mut memory = self.row_to_memory(&row).await?;
            memory.embedding = Some(embedding);
            results.push(SearchResult {
                memory,
                score,
                match_reason: format!("Vector similarity: {:.2}", score),
            });
        }
        results.sort_by(|left, right| {
            right
                .score
                .partial_cmp(&left.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        results.truncate(limit.min(1000));
        Ok(results)
    }

    /// Start one durable maintenance run. The unique idempotency key makes a
    /// retried request return to the same run rather than creating duplicate
    /// observable work.
    pub async fn start_maintenance_run(
        &self,
        id: &str,
        idempotency_key: &str,
        job_kind: &str,
        namespace: Option<&Namespace>,
        item_limit: usize,
        retry_limit: usize,
        timeout: std::time::Duration,
        stale_after_days: i64,
    ) -> Result<bool> {
        let conn = self.get_conn()?;
        let namespace = namespace.map(serde_json::to_string).transpose()?;
        let started_at = Utc::now().to_rfc3339();
        let tx = conn.transaction().await?;
        let mut reclaimed = false;
        let mut affected = tx
            .execute(
                "INSERT OR IGNORE INTO memory_maintenance_runs (id, idempotency_key, job_kind, namespace, status, started_at, item_limit, retry_limit, timeout_ms, stale_after_days) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                params![
                    id,
                    idempotency_key,
                    job_kind,
                    namespace,
                    started_at.clone(),
                    item_limit as i64,
                    retry_limit as i64,
                    timeout.as_millis().max(1) as i64,
                    stale_after_days,
                ],
            )
            .await?;
        if affected == 0 {
            let mut rows = tx
                .query(
                    "SELECT id, status, started_at, timeout_ms FROM memory_maintenance_runs WHERE idempotency_key = ?",
                    params![idempotency_key],
                )
                .await?;
            if let Some(row) = rows.next().await? {
                let existing_id: String = row.get(0)?;
                let status: String = row.get(1)?;
                let existing_started_at: String = row.get(2)?;
                let timeout_ms: i64 = row.get(3)?;
                drop(row);
                drop(rows);
                let stale = status == "running"
                    && chrono::DateTime::parse_from_rfc3339(&existing_started_at)
                        .ok()
                        .map(|value| {
                            Utc::now()
                                .signed_duration_since(value.with_timezone(&Utc))
                                .num_milliseconds()
                                > timeout_ms.max(1)
                        })
                        .unwrap_or(false);
                if stale {
                    affected = tx
                        .execute(
                            "UPDATE memory_maintenance_runs SET id = ?, status = 'running', started_at = ?, completed_at = NULL, item_limit = ?, retry_limit = ?, timeout_ms = ?, stale_after_days = ?, attempts = 0, items_processed = 0, findings_count = 0, errors_count = 0, report_json = NULL, error_message = NULL WHERE id = ? AND idempotency_key = ? AND status = 'running'",
                            params![
                                id,
                                started_at,
                                item_limit as i64,
                                retry_limit as i64,
                                timeout.as_millis().max(1) as i64,
                                stale_after_days,
                                existing_id,
                                idempotency_key,
                            ],
                        )
                        .await?;
                    reclaimed = affected > 0;
                }
            }
        }
        if affected > 0 {
            tx.execute(
                "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', NULL, ?)",
                params![
                    serde_json::json!({
                        "event": if reclaimed { "maintenance_reclaimed" } else { "maintenance_started" },
                        "run_id": id,
                        "idempotency_key": idempotency_key,
                        "job_kind": job_kind,
                    })
                    .to_string(),
                ],
            )
            .await?;
        }
        tx.commit().await?;
        Ok(affected > 0)
    }

    /// Find a maintenance run by its idempotency key.
    pub async fn get_maintenance_run(
        &self,
        idempotency_key: &str,
    ) -> Result<Option<MaintenanceRunRecord>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT id, idempotency_key, job_kind, namespace, status, started_at, completed_at, item_limit, retry_limit, timeout_ms, stale_after_days, attempts, items_processed, findings_count, errors_count, report_json, error_message FROM memory_maintenance_runs WHERE idempotency_key = ?",
                params![idempotency_key],
            )
            .await?;
        match rows.next().await? {
            Some(row) => Ok(Some(Self::maintenance_run_from_row(&row)?)),
            None => Ok(None),
        }
    }

    /// Return whether this worker still owns the running maintenance lease.
    ///
    /// A reclaimed run keeps the same idempotency key but receives a new run
    /// id.  Checking the id at attempt boundaries fences a worker that wakes
    /// after its lease has expired; it must not publish a report for the new
    /// owner.
    pub async fn maintenance_run_lease_active(&self, id: &str) -> Result<bool> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT 1 FROM memory_maintenance_runs WHERE id = ? AND status = 'running' LIMIT 1",
                params![id],
            )
            .await?;
        Ok(rows.next().await?.is_some())
    }

    /// Persist the terminal result of one maintenance run if its lease is
    /// still current.  A false result means that another worker reclaimed the
    /// idempotency key and this worker is fenced from publishing anything.
    pub async fn finish_maintenance_run(
        &self,
        id: &str,
        status: &str,
        attempts: usize,
        items_processed: usize,
        findings_count: usize,
        errors_count: usize,
        report_json: Option<&str>,
        error_message: Option<&str>,
    ) -> Result<bool> {
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let affected = tx
            .execute(
                "UPDATE memory_maintenance_runs SET status = ?, completed_at = ?, attempts = ?, items_processed = ?, findings_count = ?, errors_count = ?, report_json = ?, error_message = ? WHERE id = ? AND status = 'running'",
                params![
                    status,
                    Utc::now().to_rfc3339(),
                    attempts as i64,
                    items_processed as i64,
                    findings_count as i64,
                    errors_count as i64,
                    report_json,
                    error_message,
                    id,
                ],
            )
            .await?;
        if affected > 0 {
            tx.execute(
                "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', NULL, ?)",
                params![serde_json::json!({
                    "event": "maintenance_finished",
                    "run_id": id,
                    "status": status,
                    "attempts": attempts,
                    "items_processed": items_processed,
                    "findings_count": findings_count,
                    "errors_count": errors_count,
                })
                .to_string(),],
            )
            .await?;
        }
        tx.commit().await?;
        Ok(affected > 0)
    }

    /// List recent maintenance runs for operator-facing status surfaces.
    pub async fn list_maintenance_runs(
        &self,
        job_kind: Option<&str>,
        limit: usize,
    ) -> Result<Vec<MaintenanceRunRecord>> {
        let conn = self.get_conn()?;
        let limit = limit.clamp(1, 1000) as i64;
        let mut rows = if let Some(job_kind) = job_kind {
            conn.query(
                "SELECT id, idempotency_key, job_kind, namespace, status, started_at, completed_at, item_limit, retry_limit, timeout_ms, stale_after_days, attempts, items_processed, findings_count, errors_count, report_json, error_message FROM memory_maintenance_runs WHERE job_kind = ? ORDER BY started_at DESC LIMIT ?",
                params![job_kind, limit],
            )
            .await?
        } else {
            conn.query(
                "SELECT id, idempotency_key, job_kind, namespace, status, started_at, completed_at, item_limit, retry_limit, timeout_ms, stale_after_days, attempts, items_processed, findings_count, errors_count, report_json, error_message FROM memory_maintenance_runs ORDER BY started_at DESC LIMIT ?",
                params![limit],
            )
            .await?
        };
        let mut records = Vec::new();
        while let Some(row) = rows.next().await? {
            records.push(Self::maintenance_run_from_row(&row)?);
        }
        Ok(records)
    }

    fn maintenance_run_from_row(row: &libsql::Row) -> Result<MaintenanceRunRecord> {
        Ok(MaintenanceRunRecord {
            id: row.get(0)?,
            idempotency_key: row.get(1)?,
            job_kind: row.get(2)?,
            namespace: row.get(3)?,
            status: row.get(4)?,
            started_at: row.get(5)?,
            completed_at: row.get(6)?,
            item_limit: row.get::<i64>(7)?.max(0) as usize,
            retry_limit: row.get::<i64>(8)?.max(0) as usize,
            timeout_ms: row.get::<i64>(9)?.max(0) as u64,
            stale_after_days: row.get(10)?,
            attempts: row.get::<i64>(11)?.max(0) as usize,
            items_processed: row.get::<i64>(12)?.max(0) as usize,
            findings_count: row.get::<i64>(13)?.max(0) as usize,
            errors_count: row.get::<i64>(14)?.max(0) as usize,
            report_json: row.get(15)?,
            error_message: row.get(16)?,
        })
    }

    /// List bounded orphan records from the text-learning integrity view for
    /// advisory maintenance reports.
    pub async fn list_text_learning_orphans(&self, limit: usize) -> Result<Vec<(String, String)>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT kind, id FROM text_learning_orphans ORDER BY kind, id LIMIT ?",
                params![limit.clamp(1, 1000) as i64],
            )
            .await?;
        let mut orphans = Vec::new();
        while let Some(row) = rows.next().await? {
            orphans.push((row.get(0)?, row.get(1)?));
        }
        Ok(orphans)
    }

    async fn find_integrity_parent(
        &self,
        tx: &libsql::Transaction,
        memory: &MemoryNote,
    ) -> Result<Option<MemoryId>> {
        // Bare un-enriched notes without any provenance/context/tags are often
        // intentionally repeated observations (and are used as historical
        // snapshots by callers). Require at least one richer signal before
        // applying the fallback lexical dedup path; embedding-backed writes
        // still use the content-hash/cosine gate unconditionally.
        if memory.embedding.is_none()
            && memory.provenance.is_none()
            && memory.tags.is_empty()
            && memory.context.trim().is_empty()
        {
            return Ok(None);
        }
        let namespace = serde_json::to_string(&memory.namespace)?;
        let memory_class = serde_json::to_value(memory.memory_class)?
            .as_str()
            .unwrap_or("knowledge")
            .to_string();
        let hash = content_hash(&memory.content);
        let mut exact = tx.query(
            "SELECT id, created_at, last_accessed_at FROM memories WHERE namespace = ? AND memory_class = ? AND content_hash = ? AND is_archived = 0 ORDER BY updated_at DESC LIMIT 1",
            params![namespace.clone(), memory_class.clone(), hash],
        ).await?;
        if let Some(row) = exact.next().await? {
            let existing_created = DateTime::parse_from_rfc3339(&row.get::<String>(1)?)
                .map(|value| value.with_timezone(&Utc))
                .ok();
            let existing_last_accessed = DateTime::parse_from_rfc3339(&row.get::<String>(2)?)
                .map(|value| value.with_timezone(&Utc))
                .ok();
            // Keep periodic historical snapshots distinct while collapsing
            // retries/repeated writes in the same day. A read-modify-write
            // snapshot with the same creation time but a different access
            // time is also intentionally preserved.
            let same_write_window = existing_created
                .map(|value| (memory.created_at - value).num_hours().abs() <= 24)
                .unwrap_or(true);
            let access_snapshot = existing_created == Some(memory.created_at)
                && existing_last_accessed.is_some_and(|value| value != memory.last_accessed_at);
            if same_write_window && !access_snapshot {
                return Ok(Some(MemoryId::from_string(&row.get::<String>(0)?)?));
            }
        }

        let mut candidates = if self.schema_type == SchemaType::LibSQL {
            tx.query(
                "SELECT id, content, embedding, created_at, last_accessed_at FROM memories WHERE namespace = ? AND memory_class = ? AND is_archived = 0 ORDER BY updated_at DESC LIMIT ?",
                params![namespace, memory_class, INTEGRITY_SCAN_LIMIT as i64],
            ).await?
        } else {
            tx.query(
                "SELECT m.id, m.content, e.embedding, m.created_at, m.last_accessed_at FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id = m.id WHERE m.namespace = ? AND m.memory_class = ? AND m.is_archived = 0 ORDER BY m.updated_at DESC LIMIT ?",
                params![namespace, memory_class, INTEGRITY_SCAN_LIMIT as i64],
            ).await?
        };
        while let Some(row) = candidates.next().await? {
            let candidate_created = DateTime::parse_from_rfc3339(&row.get::<String>(3)?)
                .map(|value| value.with_timezone(&Utc))
                .ok();
            if !candidate_created
                .map(|value| (memory.created_at - value).num_hours().abs() <= 24)
                .unwrap_or(true)
            {
                continue;
            }
            let candidate_last_accessed = DateTime::parse_from_rfc3339(&row.get::<String>(4)?)
                .map(|value| value.with_timezone(&Utc))
                .ok();
            if candidate_created == Some(memory.created_at)
                && candidate_last_accessed.is_some_and(|value| value != memory.last_accessed_at)
                && canonical_content(&memory.content)
                    == canonical_content(&row.get::<String>(1).unwrap_or_default())
            {
                continue;
            }
            let similarity = if let Some(incoming) = &memory.embedding {
                if matches!(row.column_type(2), Ok(libsql::ValueType::Blob)) {
                    cosine_similarity(incoming, &decode_embedding_from_row(&row, 2)?)
                        .unwrap_or_else(|| {
                            lexical_similarity(
                                &memory.content,
                                &row.get::<String>(1).unwrap_or_default(),
                            )
                        })
                } else {
                    lexical_similarity(&memory.content, &row.get::<String>(1).unwrap_or_default())
                }
            } else {
                lexical_similarity(&memory.content, &row.get::<String>(1).unwrap_or_default())
            };
            if similarity > NEAR_DUPLICATE_THRESHOLD {
                return Ok(Some(MemoryId::from_string(&row.get::<String>(0)?)?));
            }
        }
        Ok(None)
    }

    async fn validate_provenance_source(
        &self,
        tx: &libsql::Transaction,
        provenance: &crate::types::MemoryProvenance,
    ) -> Result<()> {
        let Some(source_id) = provenance.source_memory_id else {
            return Ok(());
        };
        let mut rows = tx
            .query(
                "SELECT 1 FROM memories WHERE id = ? LIMIT 1",
                params![source_id.to_string()],
            )
            .await?;
        if rows.next().await?.is_none() {
            return Err(MnemosyneError::MemoryNotFound(source_id.to_string()));
        }
        Ok(())
    }

    async fn add_integrity_entities(
        &self,
        tx: &libsql::Transaction,
        memory_id: MemoryId,
        namespace: &str,
        content: &str,
        related: &[String],
    ) -> Result<()> {
        for entity in extracted_entities(content, related) {
            entity.validate()?;
            tx.execute(
                "INSERT OR IGNORE INTO memory_entities (memory_id, namespace, normalized_name, display_name, role, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                params![memory_id.to_string(), namespace, entity.normalized_name, entity.display_name, entity.role, entity.confidence as f64],
            ).await?;
        }
        Ok(())
    }

    async fn add_bidirectional_links(
        &self,
        tx: &libsql::Transaction,
        source_id: MemoryId,
        links: &[MemoryLink],
        has_link_metadata: bool,
    ) -> Result<()> {
        for link in links {
            if link.target_id == source_id {
                continue;
            }
            let mut target_rows = tx
                .query(
                    "SELECT 1 FROM memories WHERE id = ? LIMIT 1",
                    params![link.target_id.to_string()],
                )
                .await?;
            if target_rows.next().await?.is_none() {
                return Err(MnemosyneError::MemoryNotFound(link.target_id.to_string()));
            }
            let link_type = serde_json::to_value(link.link_type)?
                .as_str()
                .unwrap_or("references")
                .to_string();
            let values = params![
                source_id.to_string(),
                link.target_id.to_string(),
                link_type.clone(),
                link.strength as f64,
                link.reason.clone(),
                link.created_at.to_rfc3339(),
                link.last_traversed_at.map(|value| value.to_rfc3339()),
                if link.user_created { 1i64 } else { 0i64 }
            ];
            if has_link_metadata {
                tx.execute("INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength, reason, created_at, last_traversed_at, user_created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values).await?;
                tx.execute("INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength, reason, created_at, last_traversed_at, user_created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", params![link.target_id.to_string(), source_id.to_string(), link_type, link.strength as f64, link.reason.clone(), link.created_at.to_rfc3339(), link.last_traversed_at.map(|value| value.to_rfc3339()), if link.user_created { 1i64 } else { 0i64 }]).await?;
            } else {
                tx.execute("INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", params![source_id.to_string(), link.target_id.to_string(), link_type.clone(), link.strength as f64, link.reason.clone(), link.created_at.to_rfc3339()]).await?;
                tx.execute("INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", params![link.target_id.to_string(), source_id.to_string(), link_type, link.strength as f64, link.reason.clone(), link.created_at.to_rfc3339()]).await?;
            }
        }
        Ok(())
    }

    async fn apply_structured_fact(
        &self,
        tx: &libsql::Transaction,
        fact: &StructuredFact,
    ) -> Result<()> {
        let mut rows = tx.query(
            "SELECT f.id, f.memory_id, f.object, f.confidence, f.observed_at FROM memory_facts f JOIN memories m ON m.id = f.memory_id WHERE f.subject = ? AND f.predicate = ? AND f.is_active = 1 AND m.is_archived = 0 AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) AND f.object <> ? ORDER BY f.confidence DESC, f.observed_at DESC LIMIT 1",
            params![fact.subject.clone(), fact.predicate.clone(), fact.object.clone()],
        ).await?;
        let prior = rows.next().await?;
        drop(rows);
        let prior_found = prior.is_some();
        let mut losing_prior = None;
        let superseded_prior = if let Some(row) = prior {
            let prior_id: i64 = row.get(0)?;
            let prior_memory: String = row.get(1)?;
            let prior_confidence: f64 = row.get(3)?;
            let prior_observed = DateTime::parse_from_rfc3339(&row.get::<String>(4)?)
                .map(|v| v.with_timezone(&Utc))
                .unwrap_or(DateTime::<Utc>::MIN_UTC);
            let new_wins = fact.confidence > prior_confidence as f32
                || (fact.confidence == prior_confidence as f32
                    && fact.observed_at >= prior_observed);
            if new_wins {
                tx.execute(
                    "UPDATE memory_facts SET is_active = 0, superseded_by = NULL WHERE id = ?",
                    params![prior_id],
                )
                .await?;
                // A structured fact is also a memory-level correction when the
                // successor is a different persisted memory. Keep same-memory
                // fact updates usable for callers that maintain several
                // observations on one row.
                let mut successor_rows = tx
                    .query(
                        "SELECT 1 FROM memories WHERE id = ? LIMIT 1",
                        params![fact.memory_id.to_string()],
                    )
                    .await?;
                let successor_exists = successor_rows.next().await?.is_some();
                drop(successor_rows);
                let memory_superseded =
                    prior_memory != fact.memory_id.to_string() && successor_exists;
                if memory_superseded {
                    tx.execute(
                        "UPDATE memories SET is_archived = 1, superseded_by = ?, updated_at = ? WHERE id = ? AND is_archived = 0",
                        params![fact.memory_id.to_string(), Utc::now().to_rfc3339(), prior_memory.clone()],
                    )
                    .await?;
                }
                tx.execute("INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('supersede', ?, ?)", params![prior_memory, serde_json::json!({"event":"fact_contradiction", "subject":fact.subject, "predicate":fact.predicate, "old_object":row.get::<String>(2)?, "new_object":fact.object, "new_memory_id":fact.memory_id, "memory_superseded":memory_superseded}).to_string()]).await?;
                true
            } else {
                losing_prior = Some(prior_memory);
                false
            }
        } else {
            false
        };
        let active = if prior_found && !superseded_prior {
            0i64
        } else {
            1i64
        };
        tx.execute("INSERT OR IGNORE INTO memory_facts (memory_id, subject, predicate, object, confidence, observed_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)", params![fact.memory_id.to_string(), fact.subject.clone(), fact.predicate.clone(), fact.object.clone(), fact.confidence as f64, fact.observed_at.to_rfc3339(), active]).await?;
        if let Some(prior_memory) = losing_prior {
            // Preserve the lower-confidence observation as inactive evidence,
            // but keep it out of the active recall surface instead of allowing
            // two contradictory facts to coexist silently.
            let mut successor_rows = tx
                .query(
                    "SELECT 1 FROM memories WHERE id = ? LIMIT 1",
                    params![fact.memory_id.to_string()],
                )
                .await?;
            let successor_exists = successor_rows.next().await?.is_some();
            drop(successor_rows);
            if successor_exists && prior_memory != fact.memory_id.to_string() {
                tx.execute(
                    "UPDATE memories SET is_archived = 1, superseded_by = ?, updated_at = ? WHERE id = ? AND is_archived = 0",
                    params![prior_memory.clone(), Utc::now().to_rfc3339(), fact.memory_id.to_string()],
                )
                .await?;
                tx.execute(
                    "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('supersede', ?, ?)",
                    params![fact.memory_id.to_string(), serde_json::json!({"event":"fact_conflict_rejected", "superseded_by":prior_memory, "subject":fact.subject, "predicate":fact.predicate, "object":fact.object}).to_string()],
                )
                .await?;
            }
        }
        Ok(())
    }

    async fn merge_integrity_parent(
        &self,
        tx: &libsql::Transaction,
        parent_id: MemoryId,
        memory: &MemoryNote,
        has_link_metadata: bool,
    ) -> Result<()> {
        let mut rows = tx.query("SELECT content, summary, keywords, tags, context, related_files, related_entities, importance, confidence FROM memories WHERE id = ?", params![parent_id.to_string()]).await?;
        let row = rows
            .next()
            .await?
            .ok_or_else(|| MnemosyneError::MemoryNotFound(parent_id.to_string()))?;
        let old_content: String = row.get(0)?;
        let mut keywords: Vec<String> =
            serde_json::from_str(&row.get::<String>(2)?).unwrap_or_default();
        let mut tags: Vec<String> =
            serde_json::from_str(&row.get::<String>(3)?).unwrap_or_default();
        let mut files: Vec<String> =
            serde_json::from_str(&row.get::<String>(5)?).unwrap_or_default();
        let mut entities: Vec<String> =
            serde_json::from_str(&row.get::<String>(6)?).unwrap_or_default();
        for (dest, values) in [
            (&mut keywords, &memory.keywords),
            (&mut tags, &memory.tags),
            (&mut files, &memory.related_files),
            (&mut entities, &memory.related_entities),
        ] {
            for value in values {
                if !dest.contains(value) {
                    dest.push(value.clone());
                }
            }
        }
        let content = if canonical_content(&old_content) == canonical_content(&memory.content) {
            old_content.clone()
        } else {
            // Preserve both evidence-bearing statements while keeping one
            // recall row. Exact repeats were handled by the hash gate above;
            // only genuinely new near-duplicate detail is appended.
            format!("{}\n\n{}", old_content, memory.content.trim())
        };
        tx.execute("UPDATE memories SET content = ?, content_hash = ?, summary = CASE WHEN length(?) > length(summary) THEN ? ELSE summary END, keywords = ?, tags = ?, context = CASE WHEN length(?) > length(context) THEN ? ELSE context END, related_files = ?, related_entities = ?, importance = MAX(importance, ?), confidence = MAX(confidence, ?), updated_at = ? WHERE id = ?", params![content.clone(), content_hash(&content), memory.summary.clone(), memory.summary.clone(), serde_json::to_string(&keywords)?, serde_json::to_string(&tags)?, memory.context.clone(), memory.context.clone(), serde_json::to_string(&files)?, serde_json::to_string(&entities)?, memory.importance as i64, memory.confidence as f64, Utc::now().to_rfc3339(), parent_id.to_string()]).await?;
        let namespace = serde_json::to_string(&memory.namespace)?;
        if self.table_exists_tx(tx, "memory_entities").await? {
            self.add_integrity_entities(
                tx,
                parent_id,
                &namespace,
                &memory.content,
                &memory.related_entities,
            )
            .await?;
        }
        self.add_bidirectional_links(tx, parent_id, &memory.links, has_link_metadata)
            .await?;
        if let Some(provenance) = &memory.provenance {
            provenance.validate()?;
            self.validate_provenance_source(tx, provenance).await?;
            tx.execute("INSERT OR REPLACE INTO memory_provenance (memory_id, source_kind, source_memory_id, session_id, turn_id, source_role, observed_at, evidence_quote, extractor_model, extraction_schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params![parent_id.to_string(), serde_json::to_value(provenance.source_kind)?.as_str().unwrap_or("manual"), provenance.source_memory_id.map(|id| id.to_string()), provenance.session_id.clone(), provenance.turn_id.clone(), serde_json::to_value(provenance.source_role)?.as_str().unwrap_or("unknown"), provenance.observed_at.to_rfc3339(), provenance.evidence_quote.clone(), provenance.extractor_model.clone(), provenance.extraction_schema_version.clone()]).await?;
        }
        tx.execute("INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)", params![parent_id.to_string(), serde_json::json!({"event":"integrity_enrichment", "source_memory_id":memory.id, "content_hash":content_hash(&memory.content)}).to_string()]).await?;
        if self.table_exists_tx(tx, "memory_facts").await? {
            if let Some(mut fact) = parse_structured_fact(memory) {
                fact.memory_id = parent_id;
                self.apply_structured_fact(tx, &fact).await?;
            }
        }
        Ok(())
    }

    /// Return the active values for one structured subject/predicate pair.
    pub async fn list_active_facts(
        &self,
        subject: &str,
        predicate: &str,
    ) -> Result<Vec<StructuredFact>> {
        let conn = self.get_conn()?;
        let mut rows = conn.query(
            "SELECT f.memory_id, f.subject, f.predicate, f.object, f.confidence, f.observed_at FROM memory_facts f JOIN memories m ON m.id = f.memory_id WHERE f.subject = ? AND f.predicate = ? AND f.is_active = 1 AND m.is_archived = 0 AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) ORDER BY f.confidence DESC, f.observed_at DESC",
            params![subject.to_lowercase(), predicate.to_lowercase()],
        ).await?;
        let mut facts = Vec::new();
        while let Some(row) = rows.next().await? {
            facts.push(StructuredFact {
                memory_id: MemoryId::from_string(&row.get::<String>(0)?)?,
                subject: row.get(1)?,
                predicate: row.get(2)?,
                object: row.get(3)?,
                confidence: row.get::<f64>(4)? as f32,
                observed_at: DateTime::parse_from_rfc3339(&row.get::<String>(5)?)
                    .map_err(|error| {
                        MnemosyneError::Database(format!("invalid fact timestamp: {error}"))
                    })?
                    .with_timezone(&Utc),
            });
        }
        Ok(facts)
    }

    /// Apply the same integrity gate to an explicit structured fact.
    pub async fn store_structured_fact(&self, fact: &StructuredFact) -> Result<()> {
        if fact.subject.trim().is_empty()
            || fact.predicate.trim().is_empty()
            || fact.object.trim().is_empty()
            || !fact.confidence.is_finite()
            || !(0.0..=1.0).contains(&fact.confidence)
        {
            return Err(MnemosyneError::ValidationError(
                "structured fact fields are invalid".into(),
            ));
        }
        let conn = self.get_conn()?;
        let mut memory_rows = conn
            .query(
                "SELECT 1 FROM memories WHERE id = ? LIMIT 1",
                params![fact.memory_id.to_string()],
            )
            .await?;
        if memory_rows.next().await?.is_none() {
            return Err(MnemosyneError::MemoryNotFound(fact.memory_id.to_string()));
        }
        drop(memory_rows);
        let tx = conn.transaction().await?;
        self.apply_structured_fact(&tx, fact).await?;
        tx.commit().await?;
        Ok(())
    }

    /// Count orphaned derived rows without mutating the database. Health and
    /// weekly audit reports use the same projection set as repair so operators
    /// can see both the pre-repair and post-repair counts.
    pub async fn orphan_projection_counts(&self) -> Result<OrphanRepairReport> {
        let conn = self.get_conn()?;
        let mut table_rows = conn
            .query(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')",
                params![],
            )
            .await?;
        let mut tables = HashSet::new();
        while let Some(row) = table_rows.next().await? {
            tables.insert(row.get::<String>(0)?);
        }
        drop(table_rows);
        let has = |name: &str| tables.contains(name);
        let mut report = OrphanRepairReport::default();
        if has("memory_links") {
            report.graph_links_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_links l LEFT JOIN memories s ON s.id = l.source_id LEFT JOIN memories t ON t.id = l.target_id WHERE s.id IS NULL OR t.id IS NULL",
            )
            .await?;
        }
        if has("memory_provenance") {
            report.provenance_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_provenance p LEFT JOIN memories m ON m.id = p.memory_id WHERE m.id IS NULL",
            )
            .await?;
            report.provenance_sources_cleared = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_provenance p LEFT JOIN memories s ON s.id = p.source_memory_id WHERE p.source_memory_id IS NOT NULL AND s.id IS NULL",
            )
            .await?;
        }
        if has("memory_entities") {
            report.entity_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_entities e LEFT JOIN memories m ON m.id = e.memory_id WHERE m.id IS NULL",
            )
            .await?;
        }
        if has("interaction_policies") {
            report.policy_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM interaction_policies p LEFT JOIN memories m ON m.id = p.policy_memory_id WHERE m.id IS NULL",
            )
            .await?;
        }
        if has("interaction_policy_evidence") {
            report.policy_evidence_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM interaction_policy_evidence e LEFT JOIN interaction_policies p ON p.policy_memory_id = e.policy_memory_id LEFT JOIN memories m ON m.id = e.source_memory_id WHERE p.policy_memory_id IS NULL OR m.id IS NULL",
            )
            .await?;
        }
        if has("memory_facts") {
            report.fact_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_facts f LEFT JOIN memories m ON m.id = f.memory_id WHERE m.id IS NULL",
            )
            .await?;
        }
        if has("memory_embeddings") {
            report.embeddings_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_embeddings e LEFT JOIN memories m ON m.id = e.memory_id WHERE m.id IS NULL",
            )
            .await?;
        }
        if has("memory_vectors") {
            report.vector_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memory_vectors v LEFT JOIN memories m ON m.id = v.memory_id WHERE m.id IS NULL",
            )
            .await?;
        }
        if has("memories_fts") {
            report.fts_rows_removed = scalar_count(
                &conn,
                "SELECT COUNT(*) FROM memories_fts f LEFT JOIN memories m ON m.rowid = f.rowid WHERE m.rowid IS NULL",
            )
            .await?;
        }
        Ok(report)
    }

    /// Repair at most `limit` rows from every derived projection and return
    /// exact deletion counts. No canonical memory row is deleted.
    pub async fn repair_orphans(&self, limit: usize) -> Result<OrphanRepairReport> {
        let limit = limit.clamp(1, INTEGRITY_SCAN_LIMIT) as i64;
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut report = OrphanRepairReport::default();
        if self.table_exists_tx(&tx, "memory_links").await? {
            report.graph_links_removed = tx.execute("DELETE FROM memory_links WHERE rowid IN (SELECT l.rowid FROM memory_links l LEFT JOIN memories s ON s.id = l.source_id LEFT JOIN memories t ON t.id = l.target_id WHERE s.id IS NULL OR t.id IS NULL LIMIT ?)", params![limit]).await?;
        }
        if self.table_exists_tx(&tx, "memory_provenance").await? {
            report.provenance_rows_removed = tx.execute("DELETE FROM memory_provenance WHERE rowid IN (SELECT rowid FROM memory_provenance WHERE memory_id NOT IN (SELECT id FROM memories) LIMIT ?)", params![limit]).await?;
            // Preserve a derived citation when only its source was removed,
            // but clear the dangling foreign-key value so future integrity
            // scans do not treat it as valid evidence.
            report.provenance_sources_cleared = tx
                .execute(
                    "UPDATE memory_provenance SET source_memory_id = NULL WHERE rowid IN (SELECT p.rowid FROM memory_provenance p LEFT JOIN memories s ON s.id = p.source_memory_id WHERE p.source_memory_id IS NOT NULL AND s.id IS NULL LIMIT ?)",
                    params![limit],
                )
                .await?;
        }
        if self.table_exists_tx(&tx, "memory_entities").await? {
            report.entity_rows_removed = tx.execute("DELETE FROM memory_entities WHERE rowid IN (SELECT rowid FROM memory_entities WHERE memory_id NOT IN (SELECT id FROM memories) LIMIT ?)", params![limit]).await?;
        }
        if self.table_exists_tx(&tx, "interaction_policies").await? {
            report.policy_rows_removed = tx.execute("DELETE FROM interaction_policies WHERE rowid IN (SELECT rowid FROM interaction_policies WHERE policy_memory_id NOT IN (SELECT id FROM memories) LIMIT ?)", params![limit]).await?;
        }
        if self
            .table_exists_tx(&tx, "interaction_policy_evidence")
            .await?
            && self.table_exists_tx(&tx, "interaction_policies").await?
        {
            report.policy_evidence_rows_removed = tx.execute("DELETE FROM interaction_policy_evidence WHERE rowid IN (SELECT rowid FROM interaction_policy_evidence WHERE policy_memory_id NOT IN (SELECT policy_memory_id FROM interaction_policies) OR source_memory_id NOT IN (SELECT id FROM memories) LIMIT ?)", params![limit]).await?;
        }
        if self.table_exists_tx(&tx, "memory_facts").await? {
            report.fact_rows_removed = tx
                .execute(
                    "DELETE FROM memory_facts WHERE rowid IN (SELECT rowid FROM memory_facts WHERE memory_id NOT IN (SELECT id FROM memories) LIMIT ?)",
                    params![limit],
                )
                .await?;
        }
        if self.schema_type == SchemaType::StandardSQLite
            && self.table_exists_tx(&tx, "memory_embeddings").await?
        {
            report.embeddings_removed = tx.execute("DELETE FROM memory_embeddings WHERE rowid IN (SELECT rowid FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memories) LIMIT ?)", params![limit]).await?;
        }
        if self.table_exists_tx(&tx, "memories_fts").await? {
            report.fts_rows_removed = tx
                .execute(
                    "DELETE FROM memories_fts WHERE rowid IN (SELECT rowid FROM memories_fts WHERE rowid NOT IN (SELECT rowid FROM memories) LIMIT ?)",
                    params![limit],
                )
                .await?;
        }
        report.vector_rows_removed = if self.table_exists_tx(&tx, "memory_vectors").await? {
            tx.execute("DELETE FROM memory_vectors WHERE rowid IN (SELECT rowid FROM memory_vectors WHERE memory_id NOT IN (SELECT id FROM memories) LIMIT ?)", params![limit]).await?
        } else {
            0
        };
        // Rebuild external-content FTS after removing stale rowids. This also
        // repairs missing index entries without deleting canonical content;
        // failures are returned so the maintenance report cannot claim a
        // successful repair while recall remains stale.
        if self.table_exists_tx(&tx, "memories_fts").await? {
            tx.execute(
                "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')",
                params![],
            )
            .await?;
        }
        tx.commit().await?;
        Ok(report)
    }

    async fn table_exists_tx(&self, tx: &libsql::Transaction, table: &str) -> Result<bool> {
        let mut rows = tx
            .query(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
                params![table],
            )
            .await?;
        Ok(rows.next().await?.is_some())
    }

    /// Persist an extracted interaction-policy suggestion without making it
    /// canonical. An owner must explicitly accept and apply it later.
    /// Persist a pending project-constraint proposal. Source memories and
    /// evidence are checked before the proposal becomes reviewable.
    pub async fn create_constraint_proposal(
        &self,
        id: &str,
        namespace: &Namespace,
        text: &str,
        scope: &str,
        priority: u8,
        valid_until: Option<&str>,
        source_memory_ids: &[MemoryId],
        evidence_quotes: &[String],
        proposer: &str,
        owner: &str,
    ) -> Result<ConstraintProposalRecord> {
        if id.trim().is_empty()
            || text.trim().is_empty()
            || text.chars().count() > 2_000
            || scope.trim().is_empty()
            || scope.chars().count() > 256
            || !(1..=10).contains(&priority)
            || proposer.trim().is_empty()
            || owner.trim().is_empty()
            || owner == "*"
        {
            return Err(MnemosyneError::ValidationError(
                "invalid constraint proposal fields".into(),
            ));
        }
        if source_memory_ids.is_empty()
            || source_memory_ids.len() > 16
            || evidence_quotes.is_empty()
        {
            return Err(MnemosyneError::ValidationError(
                "constraint proposal requires 1..=16 source memories and evidence quotes".into(),
            ));
        }
        if evidence_quotes.len() > 16
            || evidence_quotes
                .iter()
                .any(|quote| quote.trim().is_empty() || quote.chars().count() > 2_000)
        {
            return Err(MnemosyneError::ValidationError(
                "constraint evidence quotes must contain 1..=16 items of at most 2000 characters"
                    .into(),
            ));
        }
        if let Some(valid_until) = valid_until {
            chrono::DateTime::parse_from_rfc3339(valid_until).map_err(|_| {
                MnemosyneError::ValidationError("valid_until must be an RFC3339 timestamp".into())
            })?;
        }

        let mut sources = Vec::with_capacity(source_memory_ids.len());
        for source_id in source_memory_ids {
            let source = self.get_memory(*source_id).await?;
            if source.is_archived {
                return Err(MnemosyneError::ValidationError(format!(
                    "source memory {} is archived",
                    source_id
                )));
            }
            if source.memory_class == MemoryClass::InteractionPolicy {
                return Err(MnemosyneError::ValidationError(
                    "interaction policy rows cannot be constraint evidence".into(),
                ));
            }
            if source.namespace != *namespace && source.namespace != Namespace::Global {
                return Err(MnemosyneError::ValidationError(format!(
                    "source memory {} is outside the constraint namespace",
                    source_id
                )));
            }
            sources.push(source);
        }
        if evidence_quotes.iter().any(|quote| {
            !sources
                .iter()
                .any(|source| source.content.contains(quote) || source.summary.contains(quote))
        }) {
            return Err(MnemosyneError::ValidationError(
                "constraint evidence quote is not present in the supplied source memories".into(),
            ));
        }

        let conn = self.get_conn()?;
        let created_at = Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO constraint_proposals (id, namespace, text, scope, priority, valid_until, source_memory_ids, evidence_quotes, proposer, owner, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
            params![
                id,
                serde_json::to_string(namespace)?,
                text.trim(),
                scope.trim(),
                priority as i64,
                valid_until,
                serde_json::to_string(
                    &source_memory_ids
                        .iter()
                        .map(ToString::to_string)
                        .collect::<Vec<_>>(),
                )?,
                serde_json::to_string(evidence_quotes)?,
                proposer.trim(),
                owner.trim(),
                created_at,
            ],
        )
        .await?;
        conn.execute(
            "INSERT INTO audit_log (operation, metadata) VALUES ('create', ?)",
            params![serde_json::json!({
                "event": "constraint_proposal_created",
                "proposal_id": id,
                "namespace": namespace.to_string(),
            })
            .to_string()],
        )
        .await?;
        self.get_constraint_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("constraint proposal {}", id)))
    }

    pub async fn get_constraint_proposal(
        &self,
        id: &str,
    ) -> Result<Option<ConstraintProposalRecord>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT id, namespace, text, scope, priority, valid_until, source_memory_ids, evidence_quotes, proposer, owner, status, created_at, approved_by, decided_at, decision_note FROM constraint_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        match rows.next().await? {
            Some(row) => Ok(Some(Self::constraint_proposal_from_row(&row)?)),
            None => Ok(None),
        }
    }

    pub async fn list_constraint_proposals(
        &self,
        namespace: Option<&Namespace>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ConstraintProposalRecord>> {
        let limit = limit.clamp(1, 1_000);
        let mut clauses = Vec::new();
        let mut values = Vec::new();
        if let Some(namespace) = namespace {
            clauses.push("namespace = ?");
            values.push(libsql::Value::Text(serde_json::to_string(namespace)?));
        }
        if let Some(status) = status {
            clauses.push("status = ?");
            values.push(libsql::Value::Text(status.to_string()));
        }
        let where_clause = if clauses.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", clauses.join(" AND "))
        };
        let sql = format!(
            "SELECT id, namespace, text, scope, priority, valid_until, source_memory_ids, evidence_quotes, proposer, owner, status, created_at, approved_by, decided_at, decision_note FROM constraint_proposals{} ORDER BY priority DESC, created_at DESC, id ASC LIMIT {}",
            where_clause, limit
        );
        let conn = self.get_conn()?;
        let mut rows = conn.query(&sql, libsql::params_from_iter(values)).await?;
        let mut proposals = Vec::new();
        while let Some(row) = rows.next().await? {
            proposals.push(Self::constraint_proposal_from_row(&row)?);
        }
        Ok(proposals)
    }

    /// Approve or reject a proposed constraint. Approval only changes
    /// lifecycle state; it does not mutate a memory row.
    pub async fn decide_constraint_proposal(
        &self,
        id: &str,
        reviewer: &str,
        decision: &str,
        note: Option<&str>,
    ) -> Result<ConstraintProposalRecord> {
        if reviewer.trim().is_empty() || !matches!(decision, "approved" | "rejected") {
            return Err(MnemosyneError::ValidationError(
                "constraint decision requires a reviewer and approved/rejected status".into(),
            ));
        }
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT owner, status FROM constraint_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        let Some(row) = rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!(
                "constraint proposal {}",
                id
            )));
        };
        let owner: String = row.get(0)?;
        let status: String = row.get(1)?;
        drop(rows);
        if owner != reviewer {
            return Err(MnemosyneError::ValidationError(format!(
                "constraint proposal {} is routed to owner {}",
                id, owner
            )));
        }
        if status != "proposed" {
            return Err(MnemosyneError::ValidationError(format!(
                "constraint proposal {} is already {}",
                id, status
            )));
        }
        let now = Utc::now().to_rfc3339();
        conn.execute(
            "UPDATE constraint_proposals SET status = ?, approved_by = ?, decided_at = ?, decision_note = ? WHERE id = ? AND status = 'proposed'",
            params![decision, reviewer.trim(), now, note, id],
        )
        .await?;
        conn.execute(
            "INSERT INTO audit_log (operation, metadata) VALUES ('update', ?)",
            params![serde_json::json!({
                "event": "constraint_proposal_decided",
                "proposal_id": id,
                "status": decision,
                "reviewer": reviewer.trim(),
            })
            .to_string()],
        )
        .await?;
        self.get_constraint_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("constraint proposal {}", id)))
    }

    /// Retire an approved constraint without deleting its audit history.
    pub async fn supersede_constraint_proposal(
        &self,
        id: &str,
        reviewer: &str,
        note: Option<&str>,
    ) -> Result<ConstraintProposalRecord> {
        if reviewer.trim().is_empty() {
            return Err(MnemosyneError::ValidationError(
                "constraint supersession requires a reviewer".into(),
            ));
        }
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT owner, status FROM constraint_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        let Some(row) = rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!(
                "constraint proposal {}",
                id
            )));
        };
        let owner: String = row.get(0)?;
        let status: String = row.get(1)?;
        drop(rows);
        if owner != reviewer {
            return Err(MnemosyneError::ValidationError(format!(
                "constraint proposal {} is routed to owner {}",
                id, owner
            )));
        }
        if status != "approved" {
            return Err(MnemosyneError::ValidationError(format!(
                "only approved constraints can be superseded (current: {})",
                status
            )));
        }
        let now = Utc::now().to_rfc3339();
        conn.execute(
            "UPDATE constraint_proposals SET status = 'superseded', approved_by = ?, decided_at = ?, decision_note = ? WHERE id = ? AND status = 'approved'",
            params![reviewer.trim(), now, note, id],
        )
        .await?;
        conn.execute(
            "INSERT INTO audit_log (operation, metadata) VALUES ('update', ?)",
            params![serde_json::json!({
                "event": "constraint_proposal_superseded",
                "proposal_id": id,
                "reviewer": reviewer.trim(),
            })
            .to_string()],
        )
        .await?;
        self.get_constraint_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("constraint proposal {}", id)))
    }

    fn constraint_proposal_from_row(row: &libsql::Row) -> Result<ConstraintProposalRecord> {
        Ok(ConstraintProposalRecord {
            id: row.get(0)?,
            namespace: row.get(1)?,
            text: row.get(2)?,
            scope: row.get(3)?,
            priority: row.get::<i64>(4)?.clamp(1, 10) as u8,
            valid_until: row.get(5)?,
            source_memory_ids: row.get(6)?,
            evidence_quotes: row.get(7)?,
            proposer: row.get(8)?,
            owner: row.get(9)?,
            status: row.get(10)?,
            created_at: row.get(11)?,
            approved_by: row.get(12)?,
            decided_at: row.get(13)?,
            decision_note: row.get(14)?,
        })
    }

    pub async fn create_interaction_policy_proposal(
        &self,
        id: &str,
        namespace: &Namespace,
        source_memory_id: &MemoryId,
        policy: &crate::types::InteractionPolicy,
        proposer: &str,
        owner: &str,
    ) -> Result<InteractionPolicyProposalRecord> {
        policy.validate()?;
        if proposer.trim().is_empty() || owner.trim().is_empty() || owner.trim() == "*" {
            return Err(MnemosyneError::ValidationError(
                "policy proposal proposer/owner must be explicit; wildcard owners are not allowed"
                    .into(),
            ));
        }
        if policy.evidence.len() != 1
            || policy.evidence[0].source_memory_id != Some(*source_memory_id)
        {
            return Err(MnemosyneError::ValidationError(
                "policy proposal requires exactly one evidence record for its source memory".into(),
            ));
        }
        let conn = self.get_conn()?;
        let namespace_json = serde_json::to_string(namespace)?;
        let source_memory_id_string = source_memory_id.to_string();
        let global_namespace = serde_json::to_string(&Namespace::Global)?;
        let tx = conn.transaction().await?;
        let mut source_rows = tx
            .query(
                "SELECT namespace, memory_class, is_archived, content, summary, updated_at FROM memories WHERE id = ? LIMIT 1",
                params![source_memory_id_string.clone()],
            )
            .await?;
        let Some(source_row) = source_rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!(
                "policy proposal source memory {}",
                source_memory_id
            )));
        };
        let source_namespace: String = source_row.get(0)?;
        let source_class: String = source_row.get(1).unwrap_or_else(|_| "knowledge".into());
        let source_archived = source_row.get::<i64>(2).unwrap_or(0) != 0;
        let source_content: String = source_row.get(3)?;
        let source_summary: String = source_row.get(4)?;
        let source_updated_at: String = source_row.get(5)?;
        drop(source_row);
        drop(source_rows);
        let evidence_quote = &policy.evidence[0].evidence_quote;
        if source_class != "knowledge"
            || source_archived
            || (source_namespace != namespace_json && source_namespace != global_namespace)
            || (!source_content.contains(evidence_quote)
                && !source_summary.contains(evidence_quote))
        {
            return Err(MnemosyneError::ValidationError(
                "policy proposal source or evidence is not valid for its scope".into(),
            ));
        }
        let source_revision = content_revision(&source_content, &source_updated_at);
        let created_at = Utc::now().to_rfc3339();
        tx.execute(
            "INSERT INTO interaction_policy_proposals (id, namespace, source_memory_id, source_revision, polarity, guidance, applicability, signal, confidence, anchors, evidence_quote, proposer, owner, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            params![
                id,
                namespace_json,
                source_memory_id_string,
                source_revision,
                serde_json::to_value(policy.polarity)?.as_str().unwrap_or("prefer"),
                policy.guidance.clone(),
                policy.applicability.clone(),
                serde_json::to_value(policy.signal)?.as_str().unwrap_or("direct_preference"),
                policy.confidence as f64,
                serde_json::to_string(&policy.anchors)?,
                evidence_quote.clone(),
                proposer.trim(),
                owner.trim(),
                created_at,
            ],
        )
        .await?;
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
            params![
                source_memory_id.to_string(),
                serde_json::json!({
                    "event": "interaction_policy_proposal_created",
                    "proposal_id": id,
                    "owner": owner.trim(),
                    "proposer": proposer.trim(),
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;
        self.get_interaction_policy_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("interaction policy proposal {}", id)))
    }

    /// Return policy proposal IDs previously created from a raw turn.
    pub async fn interaction_policy_proposals_for_source(
        &self,
        source_memory_id: MemoryId,
    ) -> Result<Vec<String>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT id FROM interaction_policy_proposals WHERE source_memory_id = ? ORDER BY created_at DESC",
                params![source_memory_id.to_string()],
            )
            .await?;
        let mut ids = Vec::new();
        while let Some(row) = rows.next().await? {
            ids.push(row.get(0)?);
        }
        Ok(ids)
    }

    /// Fetch one interaction-policy proposal.
    pub async fn get_interaction_policy_proposal(
        &self,
        id: &str,
    ) -> Result<Option<InteractionPolicyProposalRecord>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT id, namespace, source_memory_id, source_revision, polarity, guidance, applicability, signal, confidence, anchors, evidence_quote, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM interaction_policy_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        match rows.next().await? {
            Some(row) => Ok(Some(Self::interaction_policy_proposal_from_row(&row)?)),
            None => Ok(None),
        }
    }

    /// List policy proposals with optional namespace/status filters.
    pub async fn list_interaction_policy_proposals(
        &self,
        namespace: Option<&Namespace>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<InteractionPolicyProposalRecord>> {
        let conn = self.get_conn()?;
        let mut conditions = Vec::new();
        let mut values = Vec::new();
        if let Some(namespace) = namespace {
            conditions.push("namespace = ?".to_string());
            values.push(libsql::Value::Text(serde_json::to_string(namespace)?));
        }
        if let Some(status) = status {
            conditions.push("status = ?".to_string());
            values.push(libsql::Value::Text(status.to_string()));
        }
        let where_clause = if conditions.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", conditions.join(" AND "))
        };
        values.push(libsql::Value::Integer(limit.clamp(1, 1000) as i64));
        let sql = format!(
            "SELECT id, namespace, source_memory_id, source_revision, polarity, guidance, applicability, signal, confidence, anchors, evidence_quote, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM interaction_policy_proposals{where_clause} ORDER BY created_at DESC LIMIT ?"
        );
        let mut rows = conn.query(&sql, libsql::params_from_iter(values)).await?;
        let mut proposals = Vec::new();
        while let Some(row) = rows.next().await? {
            proposals.push(Self::interaction_policy_proposal_from_row(&row)?);
        }
        Ok(proposals)
    }

    /// Accept or dismiss a pending interaction-policy proposal.
    pub async fn decide_interaction_policy_proposal(
        &self,
        id: &str,
        reviewer: &str,
        status: &str,
        decision_note: Option<&str>,
    ) -> Result<InteractionPolicyProposalRecord> {
        if !matches!(status, "accepted" | "dismissed") || reviewer.trim().is_empty() {
            return Err(MnemosyneError::ValidationError(
                "policy proposal decision must be accepted/dismissed with a reviewer".into(),
            ));
        }
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut rows = tx
            .query(
                "SELECT source_memory_id, owner, status FROM interaction_policy_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        let Some(row) = rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!(
                "interaction policy proposal {}",
                id
            )));
        };
        let source_memory_id: String = row.get(0)?;
        let owner: String = row.get(1)?;
        let current_status: String = row.get(2)?;
        drop(row);
        drop(rows);
        if current_status != "pending" {
            return Err(MnemosyneError::InvalidOperation(format!(
                "policy proposal {} is already {}",
                id, current_status
            )));
        }
        if owner != reviewer {
            return Err(MnemosyneError::PermissionDenied(format!(
                "policy proposal {} is routed to owner {}",
                id, owner
            )));
        }
        let now = Utc::now().to_rfc3339();
        tx.execute(
            "UPDATE interaction_policy_proposals SET status = ?, reviewed_by = ?, decided_at = ?, decision_note = ? WHERE id = ? AND status = 'pending'",
            params![status, reviewer, now, decision_note, id],
        )
        .await?;
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
            params![
                source_memory_id,
                serde_json::json!({
                    "event": "interaction_policy_proposal_decided",
                    "proposal_id": id,
                    "status": status,
                    "reviewer": reviewer,
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;
        self.get_interaction_policy_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("interaction policy proposal {}", id)))
    }

    /// Apply an accepted interaction-policy proposal after rechecking its
    /// source revision and verbatim evidence. The policy memory and proposal
    /// transition are committed atomically.
    pub async fn apply_interaction_policy_proposal(
        &self,
        id: &str,
        reviewer: &str,
    ) -> Result<InteractionPolicyProposalRecord> {
        if reviewer.trim().is_empty() {
            return Err(MnemosyneError::ValidationError(
                "policy proposal applier must not be empty".into(),
            ));
        }
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut rows = tx
            .query(
                "SELECT namespace, source_memory_id, source_revision, polarity, guidance, applicability, signal, confidence, anchors, evidence_quote, owner, status, reviewed_by FROM interaction_policy_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        let Some(row) = rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!(
                "interaction policy proposal {}",
                id
            )));
        };
        let namespace: String = row.get(0)?;
        let source_memory_id: String = row.get(1)?;
        let source_revision: String = row.get(2)?;
        let polarity: String = row.get(3)?;
        let guidance: String = row.get(4)?;
        let applicability: String = row.get(5)?;
        let signal: String = row.get(6)?;
        let confidence: f64 = row.get(7)?;
        let anchors_json: String = row.get(8)?;
        let evidence_quote: String = row.get(9)?;
        let owner: String = row.get(10)?;
        let status: String = row.get(11)?;
        let reviewed_by: Option<String> = row.get(12)?;
        drop(row);
        drop(rows);
        if status != "accepted" {
            return Err(MnemosyneError::InvalidOperation(format!(
                "policy proposal {} must be accepted before apply (current: {})",
                id, status
            )));
        }
        if owner != reviewer && reviewed_by.as_deref() != Some(reviewer) {
            return Err(MnemosyneError::PermissionDenied(format!(
                "policy proposal {} was not accepted by this reviewer",
                id
            )));
        }
        let mut source_rows = tx
            .query(
                "SELECT namespace, memory_class, is_archived, content, summary, updated_at FROM memories WHERE id = ? LIMIT 1",
                params![source_memory_id.clone()],
            )
            .await?;
        let Some(source_row) = source_rows.next().await? else {
            let error_message =
                format!("policy source memory {} no longer exists", source_memory_id);
            mark_policy_proposal_failed(&tx, id, &source_memory_id, &error_message).await?;
            tx.commit().await?;
            return Err(MnemosyneError::InvalidOperation(error_message));
        };
        let source_namespace: String = source_row.get(0)?;
        let source_class: String = source_row.get(1).unwrap_or_else(|_| "knowledge".into());
        let source_archived = source_row.get::<i64>(2).unwrap_or(0) != 0;
        let source_content: String = source_row.get(3)?;
        let source_summary: String = source_row.get(4)?;
        let source_updated_at: String = source_row.get(5)?;
        drop(source_row);
        drop(source_rows);
        let global_namespace = serde_json::to_string(&Namespace::Global)?;
        if source_class != "knowledge"
            || source_archived
            || (source_namespace != namespace && source_namespace != global_namespace)
            || content_revision(&source_content, &source_updated_at) != source_revision
            || (!source_content.contains(&evidence_quote)
                && !source_summary.contains(&evidence_quote))
        {
            let error_message = "policy source revision or evidence is stale";
            mark_policy_proposal_failed(&tx, id, &source_memory_id, error_message).await?;
            tx.commit().await?;
            return Err(MnemosyneError::InvalidOperation(error_message.into()));
        }
        let namespace_value: Namespace = serde_json::from_str(&namespace)?;
        let polarity_value = match polarity.as_str() {
            "avoid" => crate::types::PolicyPolarity::Avoid,
            "prefer" => crate::types::PolicyPolarity::Prefer,
            _ => {
                let error_message = "policy proposal has an invalid polarity";
                mark_policy_proposal_failed(&tx, id, &source_memory_id, error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::ValidationError(error_message.into()));
            }
        };
        let signal_value = match signal.as_str() {
            "correction" => crate::types::PolicySignalKind::Correction,
            "dissatisfaction" => crate::types::PolicySignalKind::Dissatisfaction,
            "approval" => crate::types::PolicySignalKind::Approval,
            "direct_preference" => crate::types::PolicySignalKind::DirectPreference,
            _ => {
                let error_message = "policy proposal has an invalid signal";
                mark_policy_proposal_failed(&tx, id, &source_memory_id, error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::ValidationError(error_message.into()));
            }
        };
        let anchors: Vec<String> = match serde_json::from_str(&anchors_json) {
            Ok(anchors) => anchors,
            Err(_) => {
                let error_message = "policy proposal has invalid anchors";
                mark_policy_proposal_failed(&tx, id, &source_memory_id, error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::ValidationError(error_message.into()));
            }
        };
        let now = Utc::now();
        let policy_memory_id = MemoryId::from_string(id)?;
        let policy = crate::types::InteractionPolicy {
            polarity: polarity_value,
            guidance: guidance.clone(),
            applicability: applicability.clone(),
            signal: signal_value,
            confidence: confidence as f32,
            anchors: anchors.clone(),
            evidence: vec![crate::types::MemoryProvenance {
                source_kind: crate::types::ProvenanceSourceKind::Turn,
                source_memory_id: Some(MemoryId::from_string(&source_memory_id)?),
                session_id: None,
                turn_id: None,
                source_role: crate::types::ProvenanceSourceRole::User,
                observed_at: now,
                evidence_quote: evidence_quote.clone(),
                extractor_model: None,
                extraction_schema_version: None,
            }],
        };
        policy.validate()?;
        let note = MemoryNote {
            id: policy_memory_id,
            namespace: namespace_value.clone(),
            created_at: now,
            updated_at: now,
            content: guidance.clone(),
            summary: guidance.clone(),
            keywords: Vec::new(),
            tags: vec!["interaction_policy".into()],
            context: applicability.clone(),
            memory_type: crate::types::MemoryType::Preference,
            memory_class: MemoryClass::InteractionPolicy,
            provenance: Some(policy.evidence[0].clone()),
            importance: 5,
            confidence: confidence as f32,
            links: vec![MemoryLink {
                target_id: MemoryId::from_string(&source_memory_id)?,
                link_type: crate::types::LinkType::References,
                strength: 1.0,
                reason: "owner-approved policy evidence".into(),
                created_at: now,
                last_traversed_at: None,
                user_created: false,
            }],
            related_files: Vec::new(),
            related_entities: anchors.clone(),
            access_count: 0,
            last_accessed_at: now,
            expires_at: None,
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: String::new(),
        };
        let entities = anchors
            .iter()
            .map(|anchor| MemoryEntity {
                display_name: anchor.clone(),
                normalized_name: normalize_entity_name(anchor),
                role: "anchor".into(),
                confidence: 1.0,
            })
            .collect();
        self.insert_learning_memory(
            &tx,
            &LearningMemory {
                memory: note,
                entities,
            },
        )
        .await?;
        tx.execute(
            "INSERT INTO interaction_policies (policy_memory_id, polarity, guidance, applicability, signal, confidence, anchors) VALUES (?, ?, ?, ?, ?, ?, ?)",
            params![
                id,
                serde_json::to_value(policy.polarity)?.as_str().unwrap_or("prefer"),
                policy.guidance,
                policy.applicability,
                serde_json::to_value(policy.signal)?.as_str().unwrap_or("direct_preference"),
                policy.confidence as f64,
                serde_json::to_string(&policy.anchors)?,
            ],
        )
        .await?;
        tx.execute(
            "INSERT INTO interaction_policy_evidence (policy_memory_id, source_memory_id, evidence_quote, observed_at) VALUES (?, ?, ?, ?)",
            params![id, source_memory_id.clone(), evidence_quote, now.to_rfc3339()],
        )
        .await?;
        let applied_at = now.to_rfc3339();
        tx.execute(
            "UPDATE interaction_policy_proposals SET status = 'applied', applied_at = ?, error_message = NULL WHERE id = ? AND status = 'accepted'",
            params![applied_at, id],
        )
        .await?;
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
            params![
                source_memory_id,
                serde_json::json!({
                    "event": "interaction_policy_proposal_applied",
                    "proposal_id": id,
                    "policy_memory_id": id,
                    "reviewer": reviewer,
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;
        self.get_interaction_policy_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("interaction policy proposal {}", id)))
    }

    fn interaction_policy_proposal_from_row(
        row: &libsql::Row,
    ) -> Result<InteractionPolicyProposalRecord> {
        Ok(InteractionPolicyProposalRecord {
            id: row.get(0)?,
            namespace: row.get(1)?,
            source_memory_id: row.get(2)?,
            source_revision: row.get(3)?,
            polarity: row.get(4)?,
            guidance: row.get(5)?,
            applicability: row.get(6)?,
            signal: row.get(7)?,
            confidence: row.get::<f64>(8)? as f32,
            anchors: row.get(9)?,
            evidence_quote: row.get(10)?,
            proposer: row.get(11)?,
            owner: row.get(12)?,
            status: row.get(13)?,
            created_at: row.get(14)?,
            reviewed_by: row.get(15)?,
            decided_at: row.get(16)?,
            decision_note: row.get(17)?,
            applied_at: row.get(18)?,
            error_message: row.get(19)?,
        })
    }

    /// Persist a pending owner-routed memory change proposal.
    pub async fn create_memory_proposal(
        &self,
        id: &str,
        namespace: &Namespace,
        target_memory_id: &MemoryId,
        base_updated_at: &str,
        before_content: &str,
        proposed_content: &str,
        diff_text: &str,
        source_memory_ids: &[MemoryId],
        evidence_quotes: &[String],
        proposer: &str,
        owner: &str,
    ) -> Result<MemoryProposalRecord> {
        let owner = owner.trim();
        if owner.is_empty() || owner == "*" {
            return Err(MnemosyneError::ValidationError(
                "proposal owner must be an explicit reviewer identity".into(),
            ));
        }
        let conn = self.get_conn()?;
        let namespace_json = serde_json::to_string(namespace)?;
        let source_ids_json = serde_json::to_string(
            &source_memory_ids
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>(),
        )?;
        let evidence_json = serde_json::to_string(evidence_quotes)?;
        let created_at = Utc::now().to_rfc3339();
        let tx = conn.transaction().await?;
        let mut source_revisions = Vec::with_capacity(source_memory_ids.len());
        for source_id in source_memory_ids {
            let mut rows = tx
                .query(
                    "SELECT updated_at, content FROM memories WHERE id = ? LIMIT 1",
                    params![source_id.to_string()],
                )
                .await?;
            let Some(row) = rows.next().await? else {
                return Err(MnemosyneError::NotFound(format!(
                    "proposal source memory {}",
                    source_id
                )));
            };
            let updated_at: String = row.get(0)?;
            let content: String = row.get(1)?;
            drop(row);
            drop(rows);
            source_revisions.push(content_revision(&content, &updated_at));
        }
        let source_revisions_json = serde_json::to_string(&source_revisions)?;
        tx.execute(
            "INSERT INTO memory_change_proposals (id, namespace, target_memory_id, base_updated_at, before_content, proposed_content, diff_text, source_memory_ids, source_revisions, evidence_quotes, proposer, owner, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            params![
                id,
                namespace_json,
                target_memory_id.to_string(),
                base_updated_at,
                before_content,
                proposed_content,
                diff_text,
                source_ids_json,
                source_revisions_json,
                evidence_json,
                proposer,
                owner,
                created_at,
            ],
        )
        .await?;
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
            params![
                target_memory_id.to_string(),
                serde_json::json!({
                    "event": "memory_proposal_created",
                    "proposal_id": id,
                    "owner": owner,
                    "proposer": proposer,
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;
        self.get_memory_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("memory proposal {}", id)))
    }

    /// Retrieve one durable proposal.
    pub async fn get_memory_proposal(&self, id: &str) -> Result<Option<MemoryProposalRecord>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT id, namespace, target_memory_id, base_updated_at, before_content, proposed_content, diff_text, source_memory_ids, source_revisions, evidence_quotes, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM memory_change_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        match rows.next().await? {
            Some(row) => Ok(Some(Self::memory_proposal_from_row(&row)?)),
            None => Ok(None),
        }
    }

    /// List proposals for a reviewer without removing them from the durable
    /// queue.
    pub async fn list_memory_proposals(
        &self,
        namespace: Option<&Namespace>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<MemoryProposalRecord>> {
        let conn = self.get_conn()?;
        let limit = limit.clamp(1, 1000) as i64;
        let namespace_json = namespace.map(serde_json::to_string).transpose()?;
        let (sql, values): (&str, Vec<libsql::Value>) = match (namespace_json, status) {
            (Some(namespace), Some(status)) => (
                "SELECT id, namespace, target_memory_id, base_updated_at, before_content, proposed_content, diff_text, source_memory_ids, source_revisions, evidence_quotes, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM memory_change_proposals WHERE namespace = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
                vec![
                    libsql::Value::Text(namespace),
                    libsql::Value::Text(status.to_string()),
                    libsql::Value::Integer(limit),
                ],
            ),
            (Some(namespace), None) => (
                "SELECT id, namespace, target_memory_id, base_updated_at, before_content, proposed_content, diff_text, source_memory_ids, source_revisions, evidence_quotes, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM memory_change_proposals WHERE namespace = ? ORDER BY created_at DESC LIMIT ?",
                vec![libsql::Value::Text(namespace), libsql::Value::Integer(limit)],
            ),
            (None, Some(status)) => (
                "SELECT id, namespace, target_memory_id, base_updated_at, before_content, proposed_content, diff_text, source_memory_ids, source_revisions, evidence_quotes, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM memory_change_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                vec![
                    libsql::Value::Text(status.to_string()),
                    libsql::Value::Integer(limit),
                ],
            ),
            (None, None) => (
                "SELECT id, namespace, target_memory_id, base_updated_at, before_content, proposed_content, diff_text, source_memory_ids, source_revisions, evidence_quotes, proposer, owner, status, created_at, reviewed_by, decided_at, decision_note, applied_at, error_message FROM memory_change_proposals ORDER BY created_at DESC LIMIT ?",
                vec![libsql::Value::Integer(limit)],
            ),
        };
        let mut rows = conn.query(sql, libsql::params_from_iter(values)).await?;
        let mut proposals = Vec::new();
        while let Some(row) = rows.next().await? {
            proposals.push(Self::memory_proposal_from_row(&row)?);
        }
        Ok(proposals)
    }

    /// Move a pending proposal to accepted or dismissed. Only the routed
    /// owner may decide it; applying an accepted proposal is a separate
    /// transaction and performs a base-revision check.
    pub async fn decide_memory_proposal(
        &self,
        id: &str,
        reviewer: &str,
        status: &str,
        decision_note: Option<&str>,
    ) -> Result<MemoryProposalRecord> {
        if !matches!(status, "accepted" | "dismissed") || reviewer.trim().is_empty() {
            return Err(MnemosyneError::ValidationError(
                "proposal decision must be accepted/dismissed with a reviewer".into(),
            ));
        }
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut rows = tx
            .query(
                "SELECT target_memory_id, owner, status FROM memory_change_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        let Some(row) = rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!("memory proposal {}", id)));
        };
        let target_memory_id: String = row.get(0)?;
        let owner: String = row.get(1)?;
        let current_status: String = row.get(2)?;
        drop(row);
        drop(rows);
        if current_status != "pending" {
            return Err(MnemosyneError::InvalidOperation(format!(
                "proposal {} is already {}",
                id, current_status
            )));
        }
        if owner != reviewer {
            return Err(MnemosyneError::PermissionDenied(format!(
                "proposal {} is routed to owner {}",
                id, owner
            )));
        }
        let now = Utc::now().to_rfc3339();
        tx.execute(
            "UPDATE memory_change_proposals SET status = ?, reviewed_by = ?, decided_at = ?, decision_note = ? WHERE id = ? AND status = 'pending'",
            params![status, reviewer, now, decision_note, id],
        )
        .await?;
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
            params![
                target_memory_id,
                serde_json::json!({
                    "event": "memory_proposal_decided",
                    "proposal_id": id,
                    "status": status,
                    "reviewer": reviewer,
                    "decision_note": decision_note,
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;
        self.get_memory_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("memory proposal {}", id)))
    }

    /// Apply an accepted proposal only if its target still has the exact base
    /// content and revision captured at proposal time.
    pub async fn apply_memory_proposal(
        &self,
        id: &str,
        reviewer: &str,
    ) -> Result<MemoryProposalRecord> {
        if reviewer.trim().is_empty() {
            return Err(MnemosyneError::ValidationError(
                "proposal applier must not be empty".into(),
            ));
        }
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut rows = tx
            .query(
                "SELECT namespace, target_memory_id, base_updated_at, before_content, proposed_content, source_memory_ids, source_revisions, evidence_quotes, owner, status, reviewed_by FROM memory_change_proposals WHERE id = ?",
                params![id],
            )
            .await?;
        let Some(row) = rows.next().await? else {
            return Err(MnemosyneError::NotFound(format!("memory proposal {}", id)));
        };
        let namespace: String = row.get(0)?;
        let target_memory_id: String = row.get(1)?;
        let base_updated_at: String = row.get(2)?;
        let before_content: String = row.get(3)?;
        let proposed_content: String = row.get(4)?;
        let source_memory_ids_json: String = row.get(5)?;
        let source_revisions_json: String = row.get(6)?;
        let evidence_quotes_json: String = row.get(7)?;
        let owner: String = row.get(8)?;
        let status: String = row.get(9)?;
        let reviewed_by: Option<String> = row.get(10)?;
        drop(row);
        drop(rows);
        if status != "accepted" {
            return Err(MnemosyneError::InvalidOperation(format!(
                "proposal {} must be accepted before apply (current: {})",
                id, status
            )));
        }
        if owner != reviewer && reviewed_by.as_deref() != Some(reviewer) {
            return Err(MnemosyneError::PermissionDenied(format!(
                "proposal {} was not accepted by this reviewer",
                id
            )));
        }
        let source_memory_ids: Vec<String> = serde_json::from_str(&source_memory_ids_json)
            .map_err(|error| {
                MnemosyneError::ValidationError(format!("invalid proposal sources: {error}"))
            })?;
        let source_revisions: Vec<String> =
            serde_json::from_str(&source_revisions_json).map_err(|error| {
                MnemosyneError::ValidationError(format!(
                    "invalid proposal source revisions: {error}"
                ))
            })?;
        let evidence_quotes: Vec<String> =
            serde_json::from_str(&evidence_quotes_json).map_err(|error| {
                MnemosyneError::ValidationError(format!("invalid proposal evidence: {error}"))
            })?;
        if source_revisions.len() != source_memory_ids.len() {
            let error_message =
                "proposal source revision snapshot count does not match source count";
            mark_proposal_failed(&tx, id, &target_memory_id, error_message).await?;
            tx.commit().await?;
            return Err(MnemosyneError::ValidationError(error_message.into()));
        }
        let source_memory_id = match source_memory_ids.first() {
            Some(source_memory_id) => source_memory_id,
            None => {
                let error_message = "accepted proposal has no source memory";
                mark_proposal_failed(&tx, id, &target_memory_id, error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::ValidationError(error_message.into()));
            }
        };
        let evidence_quote = match evidence_quotes.first() {
            Some(evidence_quote) => evidence_quote,
            None => {
                let error_message = "accepted proposal has no evidence quote";
                mark_proposal_failed(&tx, id, &target_memory_id, error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::ValidationError(error_message.into()));
            }
        };
        let global_namespace = serde_json::to_string(&Namespace::Global)?;
        let mut source_snapshots = Vec::with_capacity(source_memory_ids.len());
        for (source_index, source_id) in source_memory_ids.iter().enumerate() {
            let mut source_rows = tx
                .query(
                    "SELECT namespace, memory_class, is_archived, content, summary, updated_at FROM memories WHERE id = ? LIMIT 1",
                    params![source_id.clone()],
                )
                .await?;
            let Some(source_row) = source_rows.next().await? else {
                drop(source_rows);
                let error_message =
                    format!("proposal source memory {} no longer exists", source_id);
                mark_proposal_failed(&tx, id, &target_memory_id, &error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::InvalidOperation(error_message));
            };
            let source_namespace: String = source_row.get(0)?;
            let source_class: String = source_row.get(1).unwrap_or_else(|_| "knowledge".into());
            let source_archived = source_row.get::<i64>(2).unwrap_or(0) != 0;
            let source_content: String = source_row.get(3)?;
            let source_summary: String = source_row.get(4)?;
            let source_updated_at: String = source_row.get(5)?;
            drop(source_row);
            drop(source_rows);
            if source_class != "knowledge"
                || source_archived
                || (source_namespace != namespace && source_namespace != global_namespace)
                || content_revision(&source_content, &source_updated_at)
                    != source_revisions[source_index]
            {
                let error_message = format!(
                    "proposal source memory {} is no longer valid for this proposal",
                    source_id
                );
                mark_proposal_failed(&tx, id, &target_memory_id, &error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::InvalidOperation(error_message));
            }
            source_snapshots.push((source_content, source_summary));
        }
        for quote in &evidence_quotes {
            if !source_snapshots
                .iter()
                .any(|(content, summary)| content.contains(quote) || summary.contains(quote))
            {
                let error_message = "proposal evidence is no longer present in its source memories";
                mark_proposal_failed(&tx, id, &target_memory_id, error_message).await?;
                tx.commit().await?;
                return Err(MnemosyneError::InvalidOperation(error_message.into()));
            }
        }
        let now = Utc::now().to_rfc3339();
        // A content proposal invalidates all derived retrieval projections.
        // Keep the update and invalidation in this same transaction so recall
        // cannot observe new content paired with old embeddings/entities.
        let summary: String = proposed_content.chars().take(500).collect();
        let affected = if self.schema_type == SchemaType::LibSQL {
            tx.execute(
                "UPDATE memories SET content = ?, summary = ?, keywords = '[]', related_entities = '[]', embedding_model = '', embedding = NULL, updated_at = ? WHERE id = ? AND namespace = ? AND memory_class = 'knowledge' AND is_archived = 0 AND content = ? AND updated_at = ?",
                params![
                    proposed_content,
                    summary,
                    now.clone(),
                    target_memory_id.clone(),
                    namespace,
                    before_content,
                    base_updated_at,
                ],
            )
            .await?
        } else {
            tx.execute(
                "UPDATE memories SET content = ?, summary = ?, keywords = '[]', related_entities = '[]', embedding_model = '', updated_at = ? WHERE id = ? AND namespace = ? AND memory_class = 'knowledge' AND is_archived = 0 AND content = ? AND updated_at = ?",
                params![
                    proposed_content,
                    summary,
                    now.clone(),
                    target_memory_id.clone(),
                    namespace,
                    before_content,
                    base_updated_at,
                ],
            )
            .await?
        };
        if affected == 0 {
            let error_message = "proposal base revision is stale; canonical memory was not changed";
            tx.execute(
                "UPDATE memory_change_proposals SET status = 'failed', error_message = ?, applied_at = ? WHERE id = ? AND status = 'accepted'",
                params![error_message, now, id],
            )
            .await?;
            tx.execute(
                "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
                params![
                    target_memory_id,
                    serde_json::json!({
                        "event": "memory_proposal_failed",
                        "proposal_id": id,
                        "reason": "stale_base_revision",
                    })
                    .to_string(),
                ],
            )
            .await?;
            tx.commit().await?;
            return Err(MnemosyneError::InvalidOperation(error_message.into()));
        }
        tx.execute(
            "DELETE FROM memory_entities WHERE memory_id = ?",
            params![target_memory_id.clone()],
        )
        .await?;
        tx.execute(
            "DELETE FROM memory_provenance WHERE memory_id = ?",
            params![target_memory_id.clone()],
        )
        .await?;
        tx.execute(
            "INSERT INTO memory_provenance (memory_id, source_kind, source_memory_id, source_role, observed_at, evidence_quote) VALUES (?, 'manual', ?, 'unknown', ?, ?)",
            params![
                target_memory_id.clone(),
                source_memory_id.clone(),
                now.clone(),
                evidence_quote.clone(),
            ],
        )
        .await?;

        let mut auxiliary_rows = tx
            .query(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name IN ('memory_vectors', 'memory_embeddings')",
                params![],
            )
            .await?;
        let mut has_memory_vectors = false;
        let mut has_memory_embeddings = false;
        while let Some(row) = auxiliary_rows.next().await? {
            let name: String = row.get(0)?;
            has_memory_vectors |= name == "memory_vectors";
            has_memory_embeddings |= name == "memory_embeddings";
        }
        drop(auxiliary_rows);
        if has_memory_vectors {
            tx.execute(
                "DELETE FROM memory_vectors WHERE memory_id = ?",
                params![target_memory_id.clone()],
            )
            .await?;
        }
        if has_memory_embeddings {
            tx.execute(
                "DELETE FROM memory_embeddings WHERE memory_id = ?",
                params![target_memory_id.clone()],
            )
            .await?;
        }
        tx.execute(
            "UPDATE memory_change_proposals SET status = 'applied', applied_at = ?, error_message = NULL WHERE id = ? AND status = 'accepted'",
            params![now, id],
        )
        .await?;
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('update', ?, ?)",
            params![
                target_memory_id,
                serde_json::json!({
                    "event": "memory_proposal_applied",
                    "proposal_id": id,
                    "reviewer": reviewer,
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;
        self.get_memory_proposal(id)
            .await?
            .ok_or_else(|| MnemosyneError::NotFound(format!("memory proposal {}", id)))
    }

    fn memory_proposal_from_row(row: &libsql::Row) -> Result<MemoryProposalRecord> {
        Ok(MemoryProposalRecord {
            id: row.get(0)?,
            namespace: row.get(1)?,
            target_memory_id: row.get(2)?,
            base_updated_at: row.get(3)?,
            before_content: row.get(4)?,
            proposed_content: row.get(5)?,
            diff_text: row.get(6)?,
            source_memory_ids: row.get(7)?,
            source_revisions: row.get(8)?,
            evidence_quotes: row.get(9)?,
            proposer: row.get(10)?,
            owner: row.get(11)?,
            status: row.get(12)?,
            created_at: row.get(13)?,
            reviewed_by: row.get(14)?,
            decided_at: row.get(15)?,
            decision_note: row.get(16)?,
            applied_at: row.get(17)?,
            error_message: row.get(18)?,
        })
    }

    /// Store the typed policy payload associated with a policy memory.
    pub async fn store_interaction_policy(
        &self,
        policy_memory_id: MemoryId,
        policy: &crate::types::InteractionPolicy,
    ) -> Result<()> {
        policy.validate()?;
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        tx.execute(
            "INSERT OR REPLACE INTO interaction_policies (policy_memory_id, polarity, guidance, applicability, signal, confidence, anchors) VALUES (?, ?, ?, ?, ?, ?, ?)",
            params![
                policy_memory_id.to_string(),
                serde_json::to_value(policy.polarity)?.as_str().unwrap_or("prefer"),
                policy.guidance.clone(), policy.applicability.clone(),
                serde_json::to_value(policy.signal)?.as_str().unwrap_or("direct_preference"),
                policy.confidence as f64, serde_json::to_string(&policy.anchors)?,
            ],
        ).await?;
        for evidence in &policy.evidence {
            if let Some(source_id) = evidence.source_memory_id {
                tx.execute(
                    "INSERT OR IGNORE INTO interaction_policy_evidence (policy_memory_id, source_memory_id, evidence_quote, observed_at) VALUES (?, ?, ?, ?)",
                    params![policy_memory_id.to_string(), source_id.to_string(), evidence.evidence_quote.clone(), evidence.observed_at.to_rfc3339()],
                ).await?;
            }
        }
        tx.commit().await?;
        Ok(())
    }

    /// Find bounded exact entity matches without an additional model call.
    async fn entity_memory_ids(
        &self,
        query: &str,
        namespace: Option<Namespace>,
    ) -> Result<Vec<MemoryId>> {
        let conn = self.get_conn()?;
        let ns = namespace
            .map(|value| serde_json::to_string(&value))
            .transpose()?;
        let words: Vec<&str> = query
            .split(|c: char| !c.is_alphanumeric() && c != '_' && c != '-')
            .filter(|word| !word.is_empty())
            .collect();
        let mut names = Vec::new();
        // Query contiguous n-grams so multi-word entities such as "Rust
        // Analyzer" remain exact anchors while single-token entities still
        // work. The bound keeps arbitrary user input from creating a query
        // explosion.
        for start in 0..words.len() {
            for end in (start + 1)..=words.len().min(start + 6) {
                let normalized = normalize_entity_name(&words[start..end].join(" "));
                if normalized.len() >= 2 {
                    names.push(normalized);
                }
            }
        }
        names.sort();
        names.dedup();
        let mut found = Vec::new();
        for normalized in names {
            let (sql, params_vec) = if let Some(ref ns) = ns {
                (
                    "SELECT DISTINCT memory_id FROM memory_entities WHERE normalized_name = ? AND namespace = ? LIMIT 100",
                    vec![libsql::Value::Text(normalized.clone()), libsql::Value::Text(ns.clone())],
                )
            } else {
                (
                    "SELECT DISTINCT memory_id FROM memory_entities WHERE normalized_name = ? LIMIT 100",
                    vec![libsql::Value::Text(normalized.clone())],
                )
            };
            let mut rows = conn
                .query(sql, libsql::params_from_iter(params_vec))
                .await?;
            while let Some(row) = rows.next().await? {
                if let Ok(value) = row.get::<String>(0) {
                    if let Ok(id) = MemoryId::from_string(&value) {
                        found.push(id);
                    }
                }
            }
        }
        found.sort_unstable_by_key(|id| id.to_string());
        found.dedup();
        found.truncate(MAX_GRAPH_SEEDS);
        Ok(found)
    }

    async fn insert_learning_memory(
        &self,
        tx: &libsql::Transaction,
        item: &LearningMemory,
    ) -> Result<MemoryId> {
        let memory = &item.memory;
        if let Some(parent_id) = self.find_integrity_parent(tx, memory).await? {
            self.merge_integrity_parent(tx, parent_id, memory, true)
                .await?;
            return Ok(parent_id);
        }
        let memory_hash = content_hash(&memory.content);
        let memory_type = serde_json::to_value(memory.memory_type)?
            .as_str()
            .ok_or_else(|| MnemosyneError::Database("invalid memory type".into()))?
            .to_string();
        let memory_class = serde_json::to_value(memory.memory_class)?
            .as_str()
            .ok_or_else(|| MnemosyneError::Database("invalid memory class".into()))?
            .to_string();
        let namespace = serde_json::to_string(&memory.namespace)?;
        let keywords = serde_json::to_string(&memory.keywords)?;
        let tags = serde_json::to_string(&memory.tags)?;
        let related_files = serde_json::to_string(&memory.related_files)?;
        let related_entities = serde_json::to_string(&memory.related_entities)?;
        let superseded_by = memory.superseded_by.map(|id| id.to_string());

        if self.schema_type == SchemaType::LibSQL {
            if let Some(embedding) = &memory.embedding {
                let embedding_json = serde_json::to_string(embedding)?;
                tx.execute(
                    "INSERT INTO memories (id, namespace, created_at, updated_at, content, summary, keywords, tags, context, memory_type, memory_class, importance, confidence, related_files, related_entities, access_count, last_accessed_at, expires_at, is_archived, superseded_by, embedding_model, content_hash, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, vector32(?))",
                    params![memory.id.to_string(), namespace.clone(), memory.created_at.to_rfc3339(), memory.updated_at.to_rfc3339(), memory.content.clone(), memory.summary.clone(), keywords, tags, memory.context.clone(), memory_type, memory_class, memory.importance as i64, memory.confidence as f64, related_files, related_entities, memory.access_count as i64, memory.last_accessed_at.to_rfc3339(), memory.expires_at.map(|value| value.to_rfc3339()), if memory.is_archived { 1i64 } else { 0i64 }, superseded_by, memory.embedding_model.clone(), memory_hash.clone(), embedding_json],
                ).await?;
            } else {
                tx.execute(
                    "INSERT INTO memories (id, namespace, created_at, updated_at, content, summary, keywords, tags, context, memory_type, memory_class, importance, confidence, related_files, related_entities, access_count, last_accessed_at, expires_at, is_archived, superseded_by, embedding_model, content_hash, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    params![memory.id.to_string(), namespace.clone(), memory.created_at.to_rfc3339(), memory.updated_at.to_rfc3339(), memory.content.clone(), memory.summary.clone(), keywords, tags, memory.context.clone(), memory_type, memory_class, memory.importance as i64, memory.confidence as f64, related_files, related_entities, memory.access_count as i64, memory.last_accessed_at.to_rfc3339(), memory.expires_at.map(|value| value.to_rfc3339()), if memory.is_archived { 1i64 } else { 0i64 }, superseded_by, memory.embedding_model.clone(), memory_hash.clone()],
                ).await?;
            }
        } else {
            tx.execute(
                "INSERT INTO memories (id, namespace, created_at, updated_at, content, summary, keywords, tags, context, memory_type, memory_class, importance, confidence, related_files, related_entities, access_count, last_accessed_at, expires_at, is_archived, superseded_by, embedding_model, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![memory.id.to_string(), namespace.clone(), memory.created_at.to_rfc3339(), memory.updated_at.to_rfc3339(), memory.content.clone(), memory.summary.clone(), keywords, tags, memory.context.clone(), memory_type, memory_class, memory.importance as i64, memory.confidence as f64, related_files, related_entities, memory.access_count as i64, memory.last_accessed_at.to_rfc3339(), memory.expires_at.map(|value| value.to_rfc3339()), if memory.is_archived { 1i64 } else { 0i64 }, superseded_by, memory.embedding_model.clone(), memory_hash.clone()],
            ).await?;
        }

        self.add_bidirectional_links(tx, memory.id, &memory.links, true)
            .await?;

        if let Some(provenance) = &memory.provenance {
            provenance.validate()?;
            self.validate_provenance_source(tx, provenance).await?;
            tx.execute(
                "INSERT INTO memory_provenance (memory_id, source_kind, source_memory_id, session_id, turn_id, source_role, observed_at, evidence_quote, extractor_model, extraction_schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![memory.id.to_string(), serde_json::to_value(provenance.source_kind)?.as_str().unwrap_or("manual"), provenance.source_memory_id.map(|id| id.to_string()), provenance.session_id.clone(), provenance.turn_id.clone(), serde_json::to_value(provenance.source_role)?.as_str().unwrap_or("unknown"), provenance.observed_at.to_rfc3339(), provenance.evidence_quote.clone(), provenance.extractor_model.clone(), provenance.extraction_schema_version.clone()],
            ).await?;
        }

        if self.table_exists_tx(tx, "memory_entities").await? {
            let entities = if item.entities.is_empty() {
                extracted_entities(&memory.content, &memory.related_entities)
            } else {
                item.entities.clone()
            };
            for entity in &entities {
                entity.validate()?;
                let normalized_name = normalize_entity_name(&entity.normalized_name);
                if normalized_name.is_empty() {
                    continue;
                }
                tx.execute(
                    "INSERT OR IGNORE INTO memory_entities (memory_id, namespace, normalized_name, display_name, role, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                    params![memory.id.to_string(), namespace.clone(), normalized_name, entity.display_name.clone(), entity.role.clone(), entity.confidence as f64],
                ).await?;
            }
        }
        if self.table_exists_tx(tx, "memory_facts").await? {
            if let Some(fact) = parse_structured_fact(memory) {
                self.apply_structured_fact(tx, &fact).await?;
            }
        }

        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES (?, ?, ?)",
            params![
                "create",
                memory.id.to_string(),
                serde_json::json!({"memory_class": memory.memory_class}).to_string()
            ],
        )
        .await?;
        Ok(memory.id)
    }

    /// Persist all derived memories, links, provenance, entities, and an
    /// optional policy update in one transaction. A failed item rolls back the
    /// entire derived batch, while the already-written raw turn remains.
    pub async fn store_learning_batch(
        &self,
        items: &[LearningMemory],
        policy: Option<(MemoryId, crate::types::InteractionPolicy)>,
        superseded_policy: Option<(MemoryId, MemoryId)>,
    ) -> Result<()> {
        self.store_learning_batch_with_ids(items, policy, superseded_policy)
            .await
            .map(|_| ())
    }

    /// Variant of [`store_learning_batch`] that returns the canonical ID for
    /// each requested item. Integrity enrichment can merge a requested row
    /// into an existing parent, so callers that persist secondary metadata
    /// must use these returned IDs rather than the unmaterialized request IDs.
    pub async fn store_learning_batch_with_ids(
        &self,
        items: &[LearningMemory],
        policy: Option<(MemoryId, crate::types::InteractionPolicy)>,
        superseded_policy: Option<(MemoryId, MemoryId)>,
    ) -> Result<Vec<MemoryId>> {
        for item in items {
            if let Some(provenance) = &item.memory.provenance {
                provenance.validate()?;
            }
            for entity in &item.entities {
                entity.validate()?;
            }
        }
        if let Some((_, policy)) = &policy {
            policy.validate()?;
        }
        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        let mut stored_ids = Vec::with_capacity(items.len());
        for item in items {
            stored_ids.push(self.insert_learning_memory(&tx, item).await?);
        }
        if let Some((requested_policy_id, policy)) = policy {
            let policy_id = items
                .iter()
                .position(|item| item.memory.id == requested_policy_id)
                .and_then(|index| stored_ids.get(index).copied())
                .unwrap_or(requested_policy_id);
            tx.execute(
                "INSERT OR REPLACE INTO interaction_policies (policy_memory_id, polarity, guidance, applicability, signal, confidence, anchors) VALUES (?, ?, ?, ?, ?, ?, ?)",
                params![policy_id.to_string(), serde_json::to_value(policy.polarity)?.as_str().unwrap_or("prefer"), policy.guidance.clone(), policy.applicability.clone(), serde_json::to_value(policy.signal)?.as_str().unwrap_or("direct_preference"), policy.confidence as f64, serde_json::to_string(&policy.anchors)?],
            ).await?;
            for evidence in &policy.evidence {
                if let Some(source_id) = evidence.source_memory_id {
                    tx.execute(
                        "INSERT OR IGNORE INTO interaction_policy_evidence (policy_memory_id, source_memory_id, evidence_quote, observed_at) VALUES (?, ?, ?, ?)",
                        params![policy_id.to_string(), source_id.to_string(), evidence.evidence_quote.clone(), evidence.observed_at.to_rfc3339()],
                    ).await?;
                }
            }
            // Keep policy anchors in the same indexed relation as extracted
            // entities. This matters when a duplicate policy is merged: its
            // materialized memory row already exists, but new applicability
            // anchors still need to become searchable metadata.
            let global_namespace = serde_json::to_string(&Namespace::Global)?;
            for anchor in &policy.anchors {
                let normalized_name = normalize_entity_name(anchor);
                if !normalized_name.is_empty() {
                    tx.execute(
                        "INSERT OR IGNORE INTO memory_entities (memory_id, namespace, normalized_name, display_name, role, confidence) VALUES (?, ?, ?, ?, 'anchor', 1.0)",
                        params![policy_id.to_string(), global_namespace.clone(), normalized_name, anchor.clone()],
                    ).await?;
                }
            }
        }
        if let Some((old_id, new_id)) = superseded_policy {
            tx.execute(
                "UPDATE memories SET is_archived = 1, superseded_by = ?, updated_at = ? WHERE id = ? AND memory_class = 'interaction_policy'",
                params![new_id.to_string(), Utc::now().to_rfc3339(), old_id.to_string()],
            ).await?;
            tx.execute(
                "INSERT INTO audit_log (operation, memory_id, metadata) VALUES (?, ?, ?)",
                params![
                    "supersede",
                    old_id.to_string(),
                    serde_json::json!({"superseded_by": new_id}).to_string()
                ],
            )
            .await?;
        }
        tx.commit().await?;
        for (item, stored_id) in items.iter().zip(stored_ids.iter()) {
            if self.embedding_service.is_some() {
                if let Err(error) = self
                    .generate_and_store_embedding(stored_id, &item.memory.content)
                    .await
                {
                    warn!(
                        "Failed to generate embedding for learned memory {}: {}",
                        stored_id, error
                    );
                }
            }
        }
        Ok(stored_ids)
    }

    /// Persist one completed-task experience and its distilled strategy or
    /// guardrail items atomically. The source trajectory is stored separately
    /// so a failed extraction can be retried without losing the evidence.
    pub async fn store_reasoning_experience(
        &self,
        experience: &ReasoningExperience,
        items: &[ReasoningMemoryRecord],
    ) -> Result<()> {
        experience.validate()?;
        if items.len() > crate::reasoning::MAX_REASONING_ITEMS {
            return Err(MnemosyneError::ValidationError(format!(
                "too many reasoning items; maximum is {}",
                crate::reasoning::MAX_REASONING_ITEMS
            )));
        }
        let mut item_ids = std::collections::HashSet::new();
        for item in items {
            item.memory.validate()?;
            if item.memory.memory.id == experience.source_memory_id {
                return Err(MnemosyneError::ValidationError(
                    "reasoning item cannot reuse its source memory id".into(),
                ));
            }
            if !item_ids.insert(item.memory.memory.id) {
                return Err(MnemosyneError::ValidationError(
                    "reasoning item ids must be unique".into(),
                ));
            }
            if item.memory.memory.namespace != experience.namespace {
                return Err(MnemosyneError::ValidationError(
                    "reasoning item namespace must match its experience".into(),
                ));
            }
            if item.memory.memory.memory_class != MemoryClass::Knowledge {
                return Err(MnemosyneError::ValidationError(
                    "reasoning items must use the knowledge memory class".into(),
                ));
            }
            let expected_kind = match experience.outcome {
                TaskOutcome::Success => ReasoningLessonKind::Strategy,
                TaskOutcome::Failure => ReasoningLessonKind::Guardrail,
                TaskOutcome::Uncertain => item.memory.lesson_kind,
            };
            if item.memory.lesson_kind != expected_kind {
                return Err(MnemosyneError::ValidationError(
                    "reasoning lesson kind does not match experience outcome".into(),
                ));
            }
            let source_id = item
                .memory
                .memory
                .provenance
                .as_ref()
                .and_then(|p| p.source_memory_id);
            if source_id != Some(experience.source_memory_id) {
                return Err(MnemosyneError::ValidationError(
                    "reasoning item provenance must reference its source trajectory".into(),
                ));
            }
        }

        let conn = self.get_conn()?;
        let tx = conn.transaction().await?;
        tx.execute(
            "INSERT INTO reasoning_experiences (id, namespace, source_memory_id, task_summary, outcome, verifier, confidence, outcome_evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                experience.id.clone(),
                serde_json::to_string(&experience.namespace)?,
                experience.source_memory_id.to_string(),
                experience.task_summary.clone(),
                experience.outcome.as_str(),
                experience.verifier.clone(),
                experience.confidence as f64,
                experience.outcome_evidence.clone(),
                experience.created_at.to_rfc3339(),
            ],
        )
        .await?;

        let mut stored_item_ids = Vec::with_capacity(items.len());
        for item in items {
            let stored_id = self
                .insert_learning_memory(
                    &tx,
                    &LearningMemory {
                        memory: item.memory.memory.clone(),
                        entities: item.entities.clone(),
                    },
                )
                .await?;
            stored_item_ids.push(stored_id);
            tx.execute(
                "INSERT INTO reasoning_memory_items (memory_id, experience_id, lesson_kind, title, description, applicability) VALUES (?, ?, ?, ?, ?, ?)",
                params![
                    stored_id.to_string(),
                    experience.id.clone(),
                    item.memory.lesson_kind.as_str(),
                    item.memory.title.clone(),
                    item.memory.description.clone(),
                    item.memory.applicability.clone(),
                ],
            )
            .await?;
        }
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES ('create', ?, ?)",
            params![
                experience.source_memory_id.to_string(),
                serde_json::json!({
                    "event": "reasoning_experience",
                    "experience_id": experience.id,
                    "outcome": experience.outcome.as_str(),
                    "item_count": items.len(),
                })
                .to_string(),
            ],
        )
        .await?;
        tx.commit().await?;

        for (item, stored_id) in items.iter().zip(stored_item_ids.iter()) {
            if self.embedding_service.is_some() {
                if let Err(error) = self
                    .generate_and_store_embedding(stored_id, &item.memory.memory.content)
                    .await
                {
                    warn!(
                        "Failed to generate embedding for reasoning memory {}",
                        stored_id
                    );
                    debug!("reasoning embedding error: {}", error);
                }
            }
        }
        Ok(())
    }

    /// Search only distilled reasoning items. Generic factual recall can keep
    /// using the existing knowledge-class search without strategy items
    /// crowding it out. A wide bounded candidate pool allows the metadata
    /// filter to work with both keyword-only and vector-backed databases.
    pub async fn search_reasoning_strategies(
        &self,
        query: &str,
        namespace: Option<Namespace>,
        max_results: usize,
    ) -> Result<Vec<ReasoningSearchHit>> {
        if max_results == 0 {
            return Ok(Vec::new());
        }
        let candidate_limit = max_results.saturating_mul(32).min(256).max(max_results);
        let candidates = <Self as StorageBackend>::hybrid_search(
            self,
            query,
            namespace.clone(),
            candidate_limit,
            false,
        )
        .await?;
        if candidates.is_empty() {
            return Ok(Vec::new());
        }

        let conn = self.get_conn()?;
        let mut metadata = HashMap::new();
        let mut rows = if let Some(namespace) = namespace {
            conn.query(
                "SELECT r.memory_id, r.experience_id, e.outcome, r.lesson_kind, r.title, r.description, r.applicability FROM reasoning_memory_items r JOIN reasoning_experiences e ON e.id = r.experience_id JOIN memories m ON m.id = r.memory_id WHERE m.namespace = ? AND m.is_archived = 0 AND m.superseded_by IS NULL",
                params![serde_json::to_string(&namespace)?],
            )
            .await?
        } else {
            conn.query(
                "SELECT r.memory_id, r.experience_id, e.outcome, r.lesson_kind, r.title, r.description, r.applicability FROM reasoning_memory_items r JOIN reasoning_experiences e ON e.id = r.experience_id JOIN memories m ON m.id = r.memory_id WHERE m.is_archived = 0 AND m.superseded_by IS NULL",
                params![],
            )
            .await?
        };
        while let Some(row) = rows.next().await? {
            let memory_id = MemoryId::from_string(&row.get::<String>(0)?)?;
            metadata.insert(
                memory_id,
                (
                    row.get::<String>(1)?,
                    row.get::<String>(2)?,
                    row.get::<String>(3)?,
                    row.get::<String>(4)?,
                    row.get::<String>(5)?,
                    row.get::<String>(6)?,
                ),
            );
        }

        let mut hits = Vec::new();
        for result in candidates {
            let Some((experience_id, outcome, lesson_kind, title, description, applicability)) =
                metadata.get(&result.memory.id)
            else {
                continue;
            };
            let outcome = match outcome.as_str() {
                "success" => TaskOutcome::Success,
                "failure" => TaskOutcome::Failure,
                "uncertain" => TaskOutcome::Uncertain,
                _ => continue,
            };
            let lesson_kind = match lesson_kind.as_str() {
                "strategy" => ReasoningLessonKind::Strategy,
                "guardrail" => ReasoningLessonKind::Guardrail,
                _ => continue,
            };
            hits.push(ReasoningSearchHit {
                result,
                experience_id: experience_id.clone(),
                outcome,
                lesson_kind,
                title: title.clone(),
                description: description.clone(),
                applicability: applicability.clone(),
            });
            if hits.len() >= max_results {
                break;
            }
        }
        Ok(hits)
    }

    /// Store one captured turn in the durable transcript tier. The unique
    /// session/turn index makes retries idempotent when both identifiers exist.
    pub async fn store_session_transcript(&self, record: &SessionTranscriptRecord) -> Result<()> {
        let conn = self.get_conn()?;
        conn.execute(
            "INSERT OR IGNORE INTO session_transcripts (id, namespace, source_memory_id, session_id, turn_id, user_text, assistant_text, content, observed_at, valid_from, valid_until, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                record.id.to_string(), serde_json::to_string(&record.namespace)?,
                record.source_memory_id.to_string(), record.session_id.clone(), record.turn_id.clone(),
                record.user_text.clone(), record.assistant_text.clone(), record.content.clone(),
                record.observed_at.to_rfc3339(), record.valid_from.to_rfc3339(),
                record.valid_until.map(|value| value.to_rfc3339()), record.created_at.to_rfc3339(),
            ],
        ).await?;
        Ok(())
    }

    /// Full-text search over captured turns. This is intentionally separate
    /// from memory recall so raw conversations remain inspectable without
    /// becoming ranked context.
    pub async fn search_session_transcripts(
        &self,
        query: &str,
        namespace: Option<Namespace>,
        session_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<SessionTranscriptRecord>> {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let conn = self.get_conn()?;
        let mut filters = Vec::new();
        let mut values = Vec::new();
        if namespace.is_some() {
            filters.push("t.namespace = ?");
        }
        if session_id.is_some() {
            filters.push("t.session_id = ?");
        }
        let scope = if filters.is_empty() {
            String::new()
        } else {
            format!(" AND {}", filters.join(" AND "))
        };
        if let Some(namespace) = namespace {
            values.push(libsql::Value::Text(serde_json::to_string(&namespace)?));
        }
        if let Some(session_id) = session_id {
            values.push(libsql::Value::Text(session_id.to_owned()));
        }
        let (join, match_filter) = if query.trim().is_empty() {
            (String::new(), String::new())
        } else {
            values.insert(0, libsql::Value::Text(Self::build_fts_query(query)));
            (
                " JOIN session_transcripts_fts f ON f.rowid = t.rowid".to_string(),
                " AND session_transcripts_fts MATCH ?".to_string(),
            )
        };
        values.push(libsql::Value::Integer(limit.min(1000) as i64));
        let sql = format!("SELECT t.id, t.namespace, t.source_memory_id, t.session_id, t.turn_id, t.user_text, t.assistant_text, t.content, t.observed_at, t.valid_from, t.valid_until, t.created_at FROM session_transcripts t{join} WHERE 1=1{match_filter}{scope} ORDER BY t.created_at DESC LIMIT ?");
        let mut rows = conn.query(&sql, libsql::params_from_iter(values)).await?;
        let mut result = Vec::new();
        while let Some(row) = rows.next().await? {
            let parse = |index: i32| -> Result<chrono::DateTime<Utc>> {
                let value: String = row.get(index)?;
                chrono::DateTime::parse_from_rfc3339(&value)
                    .map(|parsed| parsed.with_timezone(&Utc))
                    .map_err(|error| {
                        MnemosyneError::Other(format!("Invalid transcript timestamp: {error}"))
                    })
            };
            let namespace: Namespace = serde_json::from_str(&row.get::<String>(1)?)?;
            let valid_until = row
                .get::<Option<String>>(10)?
                .map(|value| {
                    chrono::DateTime::parse_from_rfc3339(&value)
                        .map(|parsed| parsed.with_timezone(&Utc))
                        .map_err(|error| {
                            MnemosyneError::Other(format!("Invalid transcript timestamp: {error}"))
                        })
                })
                .transpose()?;
            result.push(SessionTranscriptRecord {
                id: MemoryId::from_string(&row.get::<String>(0)?)?,
                namespace,
                source_memory_id: MemoryId::from_string(&row.get::<String>(2)?)?,
                session_id: row.get(3)?,
                turn_id: row.get(4)?,
                user_text: row.get(5)?,
                assistant_text: row.get(6)?,
                content: row.get(7)?,
                observed_at: parse(8)?,
                valid_from: parse(9)?,
                valid_until,
                created_at: parse(11)?,
            });
        }
        Ok(result)
    }

    /// Find the durable raw turn for a caller-supplied session/turn identity.
    ///
    /// Only source rows (`source_memory_id IS NULL`) participate. Derived
    /// memories retain the same session and turn metadata, but must not make a
    /// retry appear to have created a second raw source.
    pub async fn find_turn_source_memory(
        &self,
        namespace: &Namespace,
        session_id: &str,
        turn_id: &str,
    ) -> Result<Option<MemoryId>> {
        let namespace = serde_json::to_string(namespace)?;
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT p.memory_id FROM memory_provenance p JOIN memories m ON m.id = p.memory_id WHERE m.namespace = ? AND p.source_kind = 'turn' AND p.source_memory_id IS NULL AND p.session_id = ? AND p.turn_id = ? ORDER BY m.created_at DESC LIMIT 1",
                params![namespace, session_id, turn_id],
            )
            .await?;
        if let Some(row) = rows.next().await? {
            Ok(Some(MemoryId::from_string(&row.get::<String>(0)?)?))
        } else {
            Ok(None)
        }
    }

    /// Return derived memories already materialized from one raw turn.
    ///
    /// This is used to make a retry after a successful extraction idempotent.
    /// Archived policy revisions are included so a retry cannot recreate a
    /// second policy merely because the first one was later superseded.
    pub async fn derived_memories_for_source(
        &self,
        source_memory_id: MemoryId,
    ) -> Result<Vec<(MemoryId, MemoryClass)>> {
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT m.id, m.memory_class FROM memories m JOIN memory_provenance p ON p.memory_id = m.id WHERE p.source_memory_id = ? ORDER BY m.created_at ASC, m.id ASC",
                params![source_memory_id.to_string()],
            )
            .await?;
        let mut result = Vec::new();
        while let Some(row) = rows.next().await? {
            let id = MemoryId::from_string(&row.get::<String>(0)?)?;
            let class = match row.get::<String>(1)?.as_str() {
                "interaction_policy" => MemoryClass::InteractionPolicy,
                _ => MemoryClass::Knowledge,
            };
            result.push((id, class));
        }
        Ok(result)
    }

    pub async fn list_interaction_policies(
        &self,
    ) -> Result<Vec<(MemoryNote, crate::types::InteractionPolicy)>> {
        let conn = self.get_conn()?;
        let mut rows = conn.query(
            "SELECT policy_memory_id, polarity, guidance, applicability, signal, confidence, anchors FROM interaction_policies ORDER BY policy_memory_id",
            params![],
        ).await?;
        let mut raw = Vec::new();
        while let Some(row) = rows.next().await? {
            raw.push((
                MemoryId::from_string(&row.get::<String>(0)?)?,
                row.get::<String>(1)?,
                row.get::<String>(2)?,
                row.get::<String>(3)?,
                row.get::<String>(4)?,
                row.get::<f64>(5)? as f32,
                row.get::<String>(6)?,
            ));
        }
        drop(rows);
        let mut result = Vec::new();
        for (id, polarity, guidance, applicability, signal, confidence, anchors_json) in raw {
            // Policy metadata without its owning memory is corruption, not an
            // empty search result. Surface it so the migration integrity check
            // and operators can repair it instead of silently losing guidance.
            let memory = self.get_memory(id).await?;
            let mut evidence_rows = conn.query(
                "SELECT source_memory_id, evidence_quote, observed_at FROM interaction_policy_evidence WHERE policy_memory_id = ?",
                params![id.to_string()],
            ).await?;
            let mut evidence = Vec::new();
            while let Some(row) = evidence_rows.next().await? {
                let source_id = MemoryId::from_string(&row.get::<String>(0)?)?;
                evidence.push(crate::types::MemoryProvenance {
                    source_kind: crate::types::ProvenanceSourceKind::Turn,
                    source_memory_id: Some(source_id),
                    session_id: None,
                    turn_id: None,
                    source_role: crate::types::ProvenanceSourceRole::User,
                    observed_at: chrono::DateTime::parse_from_rfc3339(&row.get::<String>(2)?)
                        .map_err(|error| {
                            MnemosyneError::Other(format!(
                                "Invalid policy evidence timestamp: {}",
                                error
                            ))
                        })?
                        .with_timezone(&Utc),
                    evidence_quote: row.get(1)?,
                    extractor_model: None,
                    extraction_schema_version: None,
                });
            }
            let policy = crate::types::InteractionPolicy {
                polarity: if polarity == "avoid" {
                    crate::types::PolicyPolarity::Avoid
                } else {
                    crate::types::PolicyPolarity::Prefer
                },
                guidance,
                applicability,
                signal: match signal.as_str() {
                    "correction" => crate::types::PolicySignalKind::Correction,
                    "dissatisfaction" => crate::types::PolicySignalKind::Dissatisfaction,
                    "approval" => crate::types::PolicySignalKind::Approval,
                    _ => crate::types::PolicySignalKind::DirectPreference,
                },
                confidence,
                anchors: serde_json::from_str(&anchors_json)?,
                evidence,
            };
            policy.validate()?;
            result.push((memory, policy));
        }
        Ok(result)
    }

    /// Return only current global policies with at least one live source turn
    /// and an exact/phrase anchor match. Guidance is deliberately independent
    /// from factual search so it cannot change factual abstention.
    pub async fn search_interaction_policies(
        &self,
        query: &str,
        max_results: usize,
    ) -> Result<Vec<SearchResult>> {
        let query_lower = query.to_lowercase();
        let mut matches = Vec::new();
        for (memory, policy) in self.list_interaction_policies().await? {
            if memory.namespace != Namespace::Global
                || memory.memory_class != MemoryClass::InteractionPolicy
                || memory.is_archived
                || memory.superseded_by.is_some()
                || policy.confidence < 0.5
                || policy.evidence.is_empty()
            {
                continue;
            }
            let mut live_evidence = false;
            for evidence in &policy.evidence {
                if let Some(source_id) = evidence.source_memory_id {
                    if self
                        .get_memory(source_id)
                        .await
                        .map(|source| !source.is_archived)
                        .unwrap_or(false)
                    {
                        live_evidence = true;
                        break;
                    }
                }
            }
            if !live_evidence {
                continue;
            }
            let anchor_match = policy
                .anchors
                .iter()
                .any(|anchor| query_contains_entity_phrase(&query_lower, anchor));
            let applicability_score =
                crate::session_extract::lexical_similarity(&policy.applicability, query);
            if !anchor_match && applicability_score < 0.25 {
                continue;
            }
            let score = (policy.confidence + if anchor_match { 0.15 } else { 0.0 }).min(1.0);
            matches.push(SearchResult {
                memory,
                score,
                match_reason: if anchor_match {
                    "explicit_policy_anchor".into()
                } else {
                    "policy_applicability".into()
                },
            });
        }
        matches.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        matches.truncate(max_results);
        Ok(matches)
    }
}

#[async_trait]
impl StorageBackend for LibsqlStorage {
    async fn store_memory(&self, memory: &MemoryNote) -> Result<()> {
        debug!("Storing memory: {}", memory.id);

        let conn = self.get_conn().map_err(|e| {
            let error_msg = e.to_string();
            if error_msg.contains("readonly") || error_msg.contains("permission") {
                MnemosyneError::Database(
                    "Cannot write to database: read-only or permission denied. Check file permissions and ensure WAL files (.db-wal, .db-shm) are writable.".to_string()
                )
            } else {
                e
            }
        })?;
        let has_link_metadata = connection_has_column(&conn, "memory_links", "last_traversed_at")
            .await?
            && connection_has_column(&conn, "memory_links", "user_created").await?;
        let tx = conn.transaction().await?;
        let is_raw_turn = memory.tags.iter().any(|tag| tag == "turn_sync")
            || memory.provenance.as_ref().is_some_and(|provenance| {
                provenance.source_kind == crate::types::ProvenanceSourceKind::Turn
                    && provenance.source_memory_id.is_none()
            });
        // Raw turns are append-only source events. Their deduplication key is
        // the explicit session/turn identity, not content: identical user and
        // assistant text can legitimately occur in separate turns.
        if !is_raw_turn {
            if let Some(parent_id) = self.find_integrity_parent(&tx, memory).await? {
                self.merge_integrity_parent(&tx, parent_id, memory, has_link_metadata)
                    .await?;
                tx.commit().await?;
                return Ok(());
            }
        }
        let memory_hash = content_hash(&memory.content);
        let supplied_embedding = (!is_raw_turn)
            .then_some(memory.embedding.as_ref())
            .flatten();

        // Insert memory metadata - schema varies by database type
        // LibSQL: embedding column with F32_BLOB type
        // StandardSQLite: embeddings stored separately in memory_embeddings table
        let (sql, include_embedding_param) = match self.schema_type {
            SchemaType::LibSQL => {
                // LibSQL schema: embedding column in memories table
                let sql = if supplied_embedding.is_some() {
                    r#"
                    INSERT INTO memories (
                        id, namespace, created_at, updated_at,
                        content, summary, keywords, tags, context,
                        memory_type, memory_class, importance, confidence,
                        related_files, related_entities,
                        access_count, last_accessed_at, expires_at,
                        is_archived, superseded_by, embedding_model, content_hash, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, vector32(?))
                    "#
                } else {
                    r#"
                    INSERT INTO memories (
                        id, namespace, created_at, updated_at,
                        content, summary, keywords, tags, context,
                        memory_type, memory_class, importance, confidence,
                        related_files, related_entities,
                        access_count, last_accessed_at, expires_at,
                        is_archived, superseded_by, embedding_model, content_hash, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    "#
                };
                (sql, supplied_embedding.is_some())
            }
            SchemaType::StandardSQLite => {
                // Standard SQLite schema: no embedding column in memories table
                let sql = r#"
                    INSERT INTO memories (
                        id, namespace, created_at, updated_at,
                        content, summary, keywords, tags, context,
                        memory_type, memory_class, importance, confidence,
                        related_files, related_entities,
                        access_count, last_accessed_at, expires_at,
                        is_archived, superseded_by, embedding_model, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    "#;
                (sql, false)
            }
        };

        // Serialize embedding outside params! macro to handle errors properly
        let embedding_json = match supplied_embedding {
            Some(emb) => Some(serde_json::to_string(emb).map_err(|e| {
                MnemosyneError::Database(format!("Failed to serialize embedding: {}", e))
            })?),
            None => None,
        };

        // Execute with schema-appropriate parameters
        if include_embedding_param {
            // LibSQL schema: include embedding parameter
            tx.execute(
                sql,
                params![
                    memory.id.to_string(),
                    serde_json::to_string(&memory.namespace)?,
                    memory.created_at.to_rfc3339(),
                    memory.updated_at.to_rfc3339(),
                    memory.content.clone(),
                    memory.summary.clone(),
                    serde_json::to_string(&memory.keywords)?,
                    serde_json::to_string(&memory.tags)?,
                    memory.context.clone(),
                    serde_json::to_value(memory.memory_type)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_type as string".to_string()
                        ))?,
                    serde_json::to_value(memory.memory_class)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_class as string".to_string()
                        ))?,
                    memory.importance as i64,
                    memory.confidence as f64,
                    serde_json::to_string(&memory.related_files)?,
                    serde_json::to_string(&memory.related_entities)?,
                    memory.access_count as i64,
                    memory.last_accessed_at.to_rfc3339(),
                    memory.expires_at.map(|dt| dt.to_rfc3339()),
                    if memory.is_archived { 1i64 } else { 0i64 },
                    memory.superseded_by.map(|id| id.to_string()),
                    memory.embedding_model.clone(),
                    memory_hash.clone(),
                    embedding_json
                ],
            )
            .await?;
        } else {
            // StandardSQLite schema: no embedding parameter
            tx.execute(
                sql,
                params![
                    memory.id.to_string(),
                    serde_json::to_string(&memory.namespace)?,
                    memory.created_at.to_rfc3339(),
                    memory.updated_at.to_rfc3339(),
                    memory.content.clone(),
                    memory.summary.clone(),
                    serde_json::to_string(&memory.keywords)?,
                    serde_json::to_string(&memory.tags)?,
                    memory.context.clone(),
                    serde_json::to_value(memory.memory_type)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_type as string".to_string()
                        ))?,
                    serde_json::to_value(memory.memory_class)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_class as string".to_string()
                        ))?,
                    memory.importance as i64,
                    memory.confidence as f64,
                    serde_json::to_string(&memory.related_files)?,
                    serde_json::to_string(&memory.related_entities)?,
                    memory.access_count as i64,
                    memory.last_accessed_at.to_rfc3339(),
                    memory.expires_at.map(|dt| dt.to_rfc3339()),
                    if memory.is_archived { 1i64 } else { 0i64 },
                    memory.superseded_by.map(|id| id.to_string()),
                    memory.embedding_model.clone(),
                    memory_hash.clone(),
                ],
            )
            .await?;
        }

        if self.schema_type == SchemaType::StandardSQLite {
            if let Some(embedding) = supplied_embedding {
                let bytes: Vec<u8> = embedding
                    .iter()
                    .flat_map(|value| value.to_le_bytes())
                    .collect();
                tx.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, dimension) VALUES (?, ?, ?)",
                    params![memory.id.to_string(), bytes, embedding.len() as i64],
                )
                .await?;
            }
        }

        // Links are a graph invariant: every accepted edge is idempotently
        // materialized in both directions.
        self.add_bidirectional_links(&tx, memory.id, &memory.links, has_link_metadata)
            .await?;

        // Keep provenance and indexed entities in the same transaction as the memory.
        if let Some(provenance) = &memory.provenance {
            provenance.validate()?;
            self.validate_provenance_source(&tx, provenance).await?;
            tx.execute(
                "INSERT INTO memory_provenance (memory_id, source_kind, source_memory_id, session_id, turn_id, source_role, observed_at, evidence_quote, extractor_model, extraction_schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params![
                    memory.id.to_string(),
                    serde_json::to_value(provenance.source_kind)?.as_str().unwrap_or("manual"),
                    provenance.source_memory_id.map(|id| id.to_string()),
                    provenance.session_id.clone(), provenance.turn_id.clone(),
                    serde_json::to_value(provenance.source_role)?.as_str().unwrap_or("unknown"),
                    provenance.observed_at.to_rfc3339(), provenance.evidence_quote.clone(),
                    provenance.extractor_model.clone(), provenance.extraction_schema_version.clone(),
                ],
            ).await?;
        }
        if self.table_exists_tx(&tx, "memory_entities").await? {
            self.add_integrity_entities(
                &tx,
                memory.id,
                &serde_json::to_string(&memory.namespace)?,
                &memory.content,
                &memory.related_entities,
            )
            .await?;
        }
        if self.table_exists_tx(&tx, "memory_facts").await? {
            if let Some(fact) = parse_structured_fact(memory) {
                self.apply_structured_fact(&tx, &fact).await?;
            } else {
                tx.execute(
                    "UPDATE memory_facts SET is_active = 0 WHERE memory_id = ?",
                    params![memory.id.to_string()],
                )
                .await?;
            }
        }

        // Inline audit log INSERT within the transaction (avoids a separate DB connection round-trip)
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES (?, ?, ?)",
            params![
                "create",
                memory.id.to_string(),
                serde_json::json!({
                    "namespace": memory.namespace,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                })
                .to_string(),
            ],
        )
        .await?;

        tx.commit().await.map_err(|e| {
            let error_msg = e.to_string();
            if error_msg.contains("readonly") || error_msg.contains("permission") {
                MnemosyneError::Database(
                    "Transaction failed: database is read-only. Ensure file and WAL files have write permissions.".to_string()
                )
            } else if error_msg.contains("locked") || error_msg.contains("busy") {
                MnemosyneError::Database(
                    "Transaction failed: database is locked. Another process may be writing.".to_string()
                )
            } else {
                MnemosyneError::Database(format!("Transaction commit failed: {}", error_msg))
            }
        })?;

        // Auto-generate embedding if embedding service is configured
        // This is a fire-and-forget operation - failures are logged but don't fail the store
        if self.embedding_service.is_some() {
            if let Err(e) = self
                .generate_and_store_embedding(&memory.id, &memory.content)
                .await
            {
                // Log error but don't fail the store operation
                // Embeddings can be regenerated later using CLI
                tracing::warn!(
                    "Failed to generate embedding for memory {}: {}",
                    memory.id,
                    e
                );
            }
        }

        debug!("Memory stored successfully: {}", memory.id);
        Ok(())
    }

    async fn get_memory(&self, id: MemoryId) -> Result<MemoryNote> {
        debug!("Fetching memory: {}", id);

        let conn = self.get_conn()?;
        let sql = format!(
            "SELECT {} FROM memories WHERE id = ?",
            self.memory_columns("")
        );
        let mut rows = conn.query(&sql, params![id.to_string()]).await?;

        let row = rows
            .next()
            .await?
            .ok_or_else(|| MnemosyneError::MemoryNotFound(id.to_string()))?;

        let mut memory = self.row_to_memory(&row).await?;
        drop(row);
        drop(rows);

        // StandardSQLite keeps embeddings in its companion table; hydrate the
        // public MemoryNote projection so read-modify-write callers do not
        // silently lose the vector.
        if self.schema_type == SchemaType::StandardSQLite {
            let mut embedding_rows = conn
                .query(
                    "SELECT embedding FROM memory_embeddings WHERE memory_id = ?",
                    params![id.to_string()],
                )
                .await?;
            if let Some(embedding_row) = embedding_rows.next().await? {
                memory.embedding = Some(decode_embedding_from_row(&embedding_row, 0)?);
            }
        }

        // Fetch associated links. The compact test/legacy schema may not
        // have the optional traversal columns yet.
        let has_link_metadata = connection_has_column(&conn, "memory_links", "last_traversed_at")
            .await?
            && connection_has_column(&conn, "memory_links", "user_created").await?;
        let link_query = if has_link_metadata {
            "SELECT target_id, link_type, strength, reason, created_at, last_traversed_at, user_created FROM memory_links WHERE source_id = ?"
        } else {
            "SELECT target_id, link_type, strength, reason, created_at FROM memory_links WHERE source_id = ?"
        };
        let mut link_rows = conn.query(link_query, params![id.to_string()]).await?;

        let mut links = Vec::new();
        while let Some(link_row) = link_rows.next().await? {
            let target_id_str: String = link_row.get(0)?;
            let target_id = MemoryId::from_string(&target_id_str)?;

            let link_type_str: String = link_row.get(1)?;
            let link_type = match Self::parse_link_type(&link_type_str) {
                Some(link_type) => link_type,
                None => continue,
            };

            let strength: f64 = link_row.get(2)?;
            let reason: String = link_row.get(3)?;
            let created_at = parse_datetime_from_row(&link_row, 4).ok_or_else(|| {
                MnemosyneError::Other("Invalid memory-link creation timestamp".into())
            })?;
            let last_traversed_at = if has_link_metadata {
                parse_datetime_from_row(&link_row, 5)
            } else {
                None
            };
            let user_created = if has_link_metadata {
                link_row.get::<i64>(6).unwrap_or(0) != 0
            } else {
                false
            };

            links.push(crate::types::MemoryLink {
                target_id,
                link_type,
                strength: strength as f32,
                reason,
                created_at,
                last_traversed_at,
                user_created,
            });
        }

        memory.links = links;
        Ok(memory)
    }

    async fn update_memory(&self, memory: &MemoryNote) -> Result<()> {
        debug!("Updating memory: {}", memory.id);

        let conn = self.get_conn()?;
        let has_link_metadata = connection_has_column(&conn, "memory_links", "last_traversed_at")
            .await?
            && connection_has_column(&conn, "memory_links", "user_created").await?;
        let tx = conn.transaction().await?;
        let is_raw_turn = memory.tags.iter().any(|tag| tag == "turn_sync")
            || memory.provenance.as_ref().is_some_and(|provenance| {
                provenance.source_kind == crate::types::ProvenanceSourceKind::Turn
                    && provenance.source_memory_id.is_none()
            });
        if !is_raw_turn {
            if let Some(parent_id) = self.find_integrity_parent(&tx, memory).await? {
                if parent_id != memory.id {
                    self.merge_integrity_parent(&tx, parent_id, memory, has_link_metadata)
                        .await?;
                    tx.execute("UPDATE memories SET is_archived = 1, superseded_by = ?, updated_at = ? WHERE id = ?", params![parent_id.to_string(), Utc::now().to_rfc3339(), memory.id.to_string()]).await?;
                    tx.commit().await?;
                    return Ok(());
                }
            }
        }
        let mut current_rows = tx
            .query(
                "SELECT content FROM memories WHERE id = ?",
                params![memory.id.to_string()],
            )
            .await?;
        let current_content = current_rows
            .next()
            .await?
            .and_then(|row| row.get::<String>(0).ok());
        drop(current_rows);
        let content_changed = current_content.as_deref() != Some(memory.content.as_str());
        let current_embedding = if self.schema_type == SchemaType::LibSQL {
            let mut rows = tx
                .query(
                    "SELECT embedding FROM memories WHERE id = ?",
                    params![memory.id.to_string()],
                )
                .await?;
            let embedding = if let Some(row) = rows.next().await? {
                if matches!(row.column_type(0), Ok(libsql::ValueType::Blob)) {
                    Some(decode_embedding_from_row(&row, 0)?)
                } else {
                    None
                }
            } else {
                None
            };
            drop(rows);
            embedding
        } else {
            let mut rows = tx
                .query(
                    "SELECT embedding FROM memory_embeddings WHERE memory_id = ?",
                    params![memory.id.to_string()],
                )
                .await?;
            let embedding = if let Some(row) = rows.next().await? {
                Some(decode_embedding_from_row(&row, 0)?)
            } else {
                None
            };
            drop(rows);
            embedding
        };
        let supplied_embedding_is_stale = content_changed
            && memory.embedding.as_ref().is_some_and(|supplied| {
                current_embedding
                    .as_ref()
                    .is_some_and(|current| supplied == current)
            });

        // Build SQL and params with or without embedding. StandardSQLite
        // keeps the vector in memory_embeddings; LibSQL stores it inline.
        if self.schema_type == SchemaType::StandardSQLite {
            tx.execute(
                r#"
                UPDATE memories SET
                    updated_at = ?,
                    content_hash = ?,
                    content = ?,
                    summary = ?,
                    keywords = ?,
                    tags = ?,
                    context = ?,
                    memory_class = ?,
                    importance = ?,
                    confidence = ?,
                    related_files = ?,
                    related_entities = ?,
                    is_archived = ?,
                    superseded_by = ?
                WHERE id = ?
                "#,
                params![
                    Utc::now().to_rfc3339(),
                    content_hash(&memory.content),
                    memory.content.clone(),
                    memory.summary.clone(),
                    serde_json::to_string(&memory.keywords)?,
                    serde_json::to_string(&memory.tags)?,
                    memory.context.clone(),
                    serde_json::to_value(memory.memory_class)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_class as string".to_string()
                        ))?,
                    memory.importance as i64,
                    memory.confidence as f64,
                    serde_json::to_string(&memory.related_files)?,
                    serde_json::to_string(&memory.related_entities)?,
                    if memory.is_archived { 1i64 } else { 0i64 },
                    memory.superseded_by.map(|id| id.to_string()),
                    memory.id.to_string(),
                ],
            )
            .await?;
            if let Some(embedding) = memory.embedding.as_ref() {
                let bytes: Vec<u8> = embedding
                    .iter()
                    .flat_map(|value| value.to_le_bytes())
                    .collect();
                tx.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, dimension) VALUES (?, ?, ?)",
                    params![memory.id.to_string(), bytes, embedding.len() as i64],
                )
                .await?;
            }
        } else if let Some(ref embedding) = memory.embedding {
            // Update with embedding using vector32()
            let embedding_json = serde_json::to_string(embedding)?;
            tx.execute(
                r#"
                UPDATE memories SET
                    updated_at = ?,
                    content_hash = ?,
                    content = ?,
                    summary = ?,
                    keywords = ?,
                    tags = ?,
                    context = ?,
                    memory_class = ?,
                    importance = ?,
                    confidence = ?,
                    related_files = ?,
                    related_entities = ?,
                    is_archived = ?,
                    superseded_by = ?,
                    embedding = vector32(?)
                WHERE id = ?
                "#,
                params![
                    Utc::now().to_rfc3339(),
                    content_hash(&memory.content),
                    memory.content.clone(),
                    memory.summary.clone(),
                    serde_json::to_string(&memory.keywords)?,
                    serde_json::to_string(&memory.tags)?,
                    memory.context.clone(),
                    serde_json::to_value(memory.memory_class)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_class as string".to_string()
                        ))?,
                    memory.importance as i64,
                    memory.confidence as f64,
                    serde_json::to_string(&memory.related_files)?,
                    serde_json::to_string(&memory.related_entities)?,
                    if memory.is_archived { 1i64 } else { 0i64 },
                    memory.superseded_by.map(|id| id.to_string()),
                    embedding_json,
                    memory.id.to_string(),
                ],
            )
            .await?;
        } else {
            // Update without embedding
            tx.execute(
                r#"
                UPDATE memories SET
                    updated_at = ?,
                    content_hash = ?,
                    content = ?,
                    summary = ?,
                    keywords = ?,
                    tags = ?,
                    context = ?,
                    memory_class = ?,
                    importance = ?,
                    confidence = ?,
                    related_files = ?,
                    related_entities = ?,
                    is_archived = ?,
                    superseded_by = ?
                WHERE id = ?
                "#,
                params![
                    Utc::now().to_rfc3339(),
                    content_hash(&memory.content),
                    memory.content.clone(),
                    memory.summary.clone(),
                    serde_json::to_string(&memory.keywords)?,
                    serde_json::to_string(&memory.tags)?,
                    memory.context.clone(),
                    serde_json::to_value(memory.memory_class)?
                        .as_str()
                        .ok_or_else(|| MnemosyneError::Database(
                            "Failed to serialize memory_class as string".to_string()
                        ))?,
                    memory.importance as i64,
                    memory.confidence as f64,
                    serde_json::to_string(&memory.related_files)?,
                    serde_json::to_string(&memory.related_entities)?,
                    if memory.is_archived { 1i64 } else { 0i64 },
                    memory.superseded_by.map(|id| id.to_string()),
                    memory.id.to_string(),
                ],
            )
            .await?;
        }

        // Replace this memory's outgoing edges and their generated reverse
        // edges, without deleting unrelated incoming edges from other
        // memories.
        let mut old_links = tx
            .query(
                "SELECT target_id, link_type FROM memory_links WHERE source_id = ?",
                params![memory.id.to_string()],
            )
            .await?;
        let mut old_targets = Vec::new();
        while let Some(row) = old_links.next().await? {
            old_targets.push((row.get::<String>(0)?, row.get::<String>(1)?));
        }
        drop(old_links);
        tx.execute(
            "DELETE FROM memory_links WHERE source_id = ?",
            params![memory.id.to_string()],
        )
        .await?;
        for (target_id, link_type) in old_targets {
            tx.execute(
                "DELETE FROM memory_links WHERE source_id = ? AND target_id = ? AND link_type = ?",
                params![target_id, memory.id.to_string(), link_type],
            )
            .await?;
        }
        self.add_bidirectional_links(&tx, memory.id, &memory.links, has_link_metadata)
            .await?;

        // Preserve typed entity metadata when a note is updated through its
        // compact `related_entities` projection.
        let mut entity_rows = tx
            .query(
                "SELECT normalized_name, display_name, role, confidence FROM memory_entities WHERE memory_id = ?",
                params![memory.id.to_string()],
            )
            .await?;
        let mut preserved_entities = Vec::new();
        while let Some(row) = entity_rows.next().await? {
            preserved_entities.push((
                row.get::<String>(0)?,
                row.get::<String>(1)?,
                row.get::<String>(2)?,
                row.get::<f64>(3)? as f32,
            ));
        }
        drop(entity_rows);
        tx.execute(
            "DELETE FROM memory_entities WHERE memory_id = ?",
            params![memory.id.to_string()],
        )
        .await?;
        let namespace_json = serde_json::to_string(&memory.namespace)?;
        for entity in extracted_entities(&memory.content, &memory.related_entities).iter() {
            let normalized = normalize_entity_name(&entity.normalized_name);
            if normalized.is_empty() {
                continue;
            }
            let existing = preserved_entities
                .iter()
                .find(|(old_normalized, old_display, _, _)| {
                    old_normalized == &normalized
                        || old_display.eq_ignore_ascii_case(&entity.display_name)
                });
            let (display_name, role, confidence) = existing
                .map(|(_, display, role, confidence)| (display.clone(), role.clone(), *confidence))
                .unwrap_or_else(|| {
                    (
                        entity.display_name.clone(),
                        entity.role.clone(),
                        entity.confidence,
                    )
                });
            tx.execute(
                "INSERT OR IGNORE INTO memory_entities (memory_id, namespace, normalized_name, display_name, role, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                params![memory.id.to_string(), namespace_json.clone(), normalized, display_name, role, confidence as f64],
            )
            .await?;
        }

        if self.table_exists_tx(&tx, "memory_provenance").await? {
            if let Some(provenance) = &memory.provenance {
                provenance.validate()?;
                self.validate_provenance_source(&tx, provenance).await?;
                tx.execute(
                    "INSERT OR REPLACE INTO memory_provenance (memory_id, source_kind, source_memory_id, session_id, turn_id, source_role, observed_at, evidence_quote, extractor_model, extraction_schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params![
                        memory.id.to_string(),
                        serde_json::to_value(provenance.source_kind)?.as_str().unwrap_or("manual"),
                        provenance.source_memory_id.map(|id| id.to_string()),
                        provenance.session_id.clone(), provenance.turn_id.clone(),
                        serde_json::to_value(provenance.source_role)?.as_str().unwrap_or("unknown"),
                        provenance.observed_at.to_rfc3339(), provenance.evidence_quote.clone(),
                        provenance.extractor_model.clone(), provenance.extraction_schema_version.clone(),
                    ],
                ).await?;
            } else {
                tx.execute(
                    "DELETE FROM memory_provenance WHERE memory_id = ?",
                    params![memory.id.to_string()],
                )
                .await?;
            }
        }

        // A read-modify-write of changed content without a replacement vector
        // must not leave a vector describing the old content searchable.
        if content_changed && (memory.embedding.is_none() || supplied_embedding_is_stale) {
            if self.schema_type == SchemaType::LibSQL {
                tx.execute(
                    "UPDATE memories SET embedding = NULL WHERE id = ?",
                    params![memory.id.to_string()],
                )
                .await?;
            } else {
                tx.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id = ?",
                    params![memory.id.to_string()],
                )
                .await?;
            }
        }
        if let Some(fact) = parse_structured_fact(memory) {
            self.apply_structured_fact(&tx, &fact).await?;
        }

        // Inline audit log INSERT within the transaction (avoids a separate DB connection round-trip)
        tx.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES (?, ?, ?)",
            params![
                "update",
                memory.id.to_string(),
                serde_json::json!({"importance": memory.importance}).to_string(),
            ],
        )
        .await?;

        tx.commit().await.map_err(|e| {
            let error_msg = e.to_string();
            if error_msg.contains("readonly") || error_msg.contains("permission") {
                MnemosyneError::Database(
                    "Transaction failed: database is read-only. Ensure file and WAL files have write permissions.".to_string()
                )
            } else if error_msg.contains("locked") || error_msg.contains("busy") {
                MnemosyneError::Database(
                    "Transaction failed: database is locked. Another process may be writing.".to_string()
                )
            } else {
                MnemosyneError::Database(format!("Transaction commit failed: {}", error_msg))
            }
        })?;

        Ok(())
    }

    async fn archive_memory(&self, id: MemoryId) -> Result<()> {
        debug!("Archiving memory: {}", id);

        let conn = self.get_conn()?;
        let now = Utc::now();
        if connection_has_column(&conn, "memories", "archived_at").await? {
            conn.execute(
                r#"
                UPDATE memories
                SET is_archived = 1, archived_at = COALESCE(archived_at, ?), updated_at = ?
                WHERE id = ?
                "#,
                params![now.timestamp(), now.to_rfc3339(), id.to_string()],
            )
            .await?;
        } else {
            conn.execute(
                "UPDATE memories SET is_archived = 1, updated_at = ? WHERE id = ?",
                params![now.to_rfc3339(), id.to_string()],
            )
            .await?;
        }
        if connection_has_column(&conn, "memory_facts", "memory_id").await? {
            conn.execute(
                "UPDATE memory_facts SET is_active = 0 WHERE memory_id = ?",
                params![id.to_string()],
            )
            .await?;
        }

        // Inline audit log INSERT on same connection (avoids separate get_conn() round-trip)
        conn.execute(
            "INSERT INTO audit_log (operation, memory_id, metadata) VALUES (?, ?, ?)",
            params!["archive", id.to_string(), "{}".to_string()],
        )
        .await?;

        Ok(())
    }

    async fn vector_search(
        &self,
        embedding: &[f32],
        limit: usize,
        namespace: Option<Namespace>,
    ) -> Result<Vec<SearchResult>> {
        if self.schema_type == SchemaType::StandardSQLite {
            return self
                .standard_vector_search(embedding, limit, namespace)
                .await;
        }
        debug!(
            "Vector search (limit: {}, namespace: {:?})",
            limit, namespace
        );

        let conn = self.get_conn()?;
        let query_embedding = serde_json::to_string(embedding)?;

        let sql = if namespace.is_some() {
            format!(
                r#"
                SELECT
                    id, namespace, created_at, updated_at, content, summary,
                    keywords, tags, context, memory_type, memory_class, importance, confidence,
                    related_files, related_entities, access_count, last_accessed_at,
                    expires_at, is_archived, superseded_by, embedding_model,
                    embedding, vector_distance_cos(embedding, vector32(?)) as distance
                FROM memories
                WHERE embedding IS NOT NULL
                  AND is_archived = 0
                  AND memory_class = 'knowledge'
                  AND tags NOT LIKE '%\"turn_sync\"%'
                  AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
                  AND namespace = ?
                ORDER BY distance ASC
                LIMIT {}
                "#,
                limit
            )
        } else {
            format!(
                r#"
                SELECT
                    id, namespace, created_at, updated_at, content, summary,
                    keywords, tags, context, memory_type, memory_class, importance, confidence,
                    related_files, related_entities, access_count, last_accessed_at,
                    expires_at, is_archived, superseded_by, embedding_model,
                    embedding, vector_distance_cos(embedding, vector32(?)) as distance
                FROM memories
                WHERE embedding IS NOT NULL
                  AND is_archived = 0
                  AND memory_class = 'knowledge'
                  AND tags NOT LIKE '%\"turn_sync\"%'
                  AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
                ORDER BY distance ASC
                LIMIT {}
                "#,
                limit
            )
        };

        let mut rows = if let Some(ref ns) = namespace {
            let ns_json = serde_json::to_string(ns)?;
            conn.query(&sql, params![query_embedding, ns_json]).await?
        } else {
            conn.query(&sql, params![query_embedding]).await?
        };

        let mut results = Vec::new();
        while let Some(row) = rows.next().await? {
            let distance: f64 = row.get(self.memory_column_count())?;
            let memory = self.row_to_memory(&row).await?;
            let similarity = (1.0 - (distance as f32 / 2.0)).clamp(0.0, 1.0);

            results.push(SearchResult {
                memory,
                score: similarity,
                match_reason: format!("Vector similarity: {:.2}", similarity),
            });
        }

        debug!("Vector search returned {} results", results.len());
        Ok(results)
    }

    async fn keyword_search(
        &self,
        query: &str,
        namespace: Option<Namespace>,
    ) -> Result<Vec<SearchResult>> {
        debug!("Keyword search: {} (namespace: {:?})", query, namespace);

        let namespace_filter = match namespace {
            Some(ns) => Some(serde_json::to_string(&ns).map_err(|e| {
                MnemosyneError::Database(format!("Failed to serialize namespace: {}", e))
            })?),
            None => None,
        };

        // Convert multi-word queries to OR logic for FTS5, dropping common
        // function words that otherwise match many unrelated memories. If a
        // query contains only stop words, retain the original terms rather
        // than turning it into an invalid empty MATCH expression.
        let fts_query = Self::build_fts_query(query);

        // Handle empty query - return all memories in namespace (no FTS5).
        // A non-empty punctuation-only query has no safe FTS token; treat it
        // as a keyword miss rather than sending an empty MATCH expression.
        if !query.trim().is_empty() && fts_query.is_empty() {
            return Ok(Vec::new());
        }
        let conn = self.get_conn()?;
        let candidate_limit = self.search_config.fts_candidate_limit.max(1);
        let class_filter = self.knowledge_predicate("m");
        let mut rows = if query.trim().is_empty() {
            // Empty query: list all memories (filtered by namespace if provided)
            let columns = self.memory_columns("m");
            let sql = if namespace_filter.is_some() {
                format!(
                    "SELECT {columns} FROM memories m WHERE m.namespace = ? AND m.is_archived = 0 AND {class_filter} AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) ORDER BY m.importance DESC, m.created_at DESC LIMIT {candidate_limit}",
                    columns = columns,
                    class_filter = class_filter,
                    candidate_limit = candidate_limit
                )
            } else {
                format!(
                    "SELECT {columns} FROM memories m WHERE m.is_archived = 0 AND {class_filter} AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) ORDER BY m.importance DESC, m.created_at DESC LIMIT {candidate_limit}",
                    columns = columns,
                    class_filter = class_filter,
                    candidate_limit = candidate_limit
                )
            };

            if let Some(ref ns) = namespace_filter {
                conn.query(&sql, params![ns.clone()]).await?
            } else {
                conn.query(&sql, params![]).await?
            }
        } else {
            // Non-empty query: use FTS5 full-text search with OR logic. Keep
            // bm25's relevance signal instead of returning rowid order; the
            // latter makes common terms and unrelated early memories outrank
            // a memory matching several query terms. The row cap is a wide
            // candidate pool, not the final result limit: deep BM25 matches
            // must reach fusion so multi-signal reranking can rescue them.
            let columns = self.memory_columns("m");
            let sql = if namespace_filter.is_some() {
                format!(
                    "SELECT {columns}, bm25(memories_fts) AS fts_rank FROM memories m JOIN memories_fts ON memories_fts.rowid = m.rowid WHERE memories_fts MATCH ? AND m.namespace = ? AND m.is_archived = 0 AND {class_filter} AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) ORDER BY fts_rank ASC LIMIT {candidate_limit}",
                    columns = columns,
                    class_filter = class_filter,
                    candidate_limit = candidate_limit
                )
            } else {
                format!(
                    "SELECT {columns}, bm25(memories_fts) AS fts_rank FROM memories m JOIN memories_fts ON memories_fts.rowid = m.rowid WHERE memories_fts MATCH ? AND m.is_archived = 0 AND {class_filter} AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) ORDER BY fts_rank ASC LIMIT {candidate_limit}",
                    columns = columns,
                    class_filter = class_filter,
                    candidate_limit = candidate_limit
                )
            };

            if let Some(ref ns_json) = namespace_filter {
                conn.query(&sql, params![fts_query, ns_json.clone()])
                    .await?
            } else {
                conn.query(&sql, params![fts_query]).await?
            }
        };

        let mut results = Vec::new();
        let mut bm25_rows = Vec::new();
        while let Some(row) = rows.next().await? {
            let memory = self.row_to_memory(&row).await?;
            if query.trim().is_empty() {
                results.push(SearchResult {
                    memory,
                    score: 0.8,
                    match_reason: "keyword_match".to_string(),
                });
            } else {
                let rank: f64 = row.get(self.memory_column_count())?;
                bm25_rows.push((memory, (-rank).max(0.0) as f32));
            }
        }

        // bm25 returns lower (usually negative) values for better matches.
        // Normalize per query so keyword relevance remains a bounded signal
        // compatible with the hybrid scorer's other components.
        let best_rank = bm25_rows
            .iter()
            .map(|(_, rank)| *rank)
            .fold(0.0_f32, f32::max);
        for (memory, rank) in bm25_rows {
            let score = if best_rank > 0.0 {
                rank / best_rank
            } else {
                0.0
            };
            results.push(SearchResult {
                memory,
                score,
                match_reason: format!("keyword_match ({:.2})", score),
            });
        }

        debug!("Keyword search found {} results", results.len());
        Ok(results)
    }

    async fn graph_traverse(
        &self,
        seed_ids: &[MemoryId],
        max_hops: usize,
        namespace: Option<Namespace>,
    ) -> Result<Vec<MemoryNote>> {
        self.graph_traverse_with_limit(seed_ids, max_hops, namespace, None)
            .await
    }

    async fn graph_traverse_bounded(
        &self,
        seed_ids: &[MemoryId],
        max_hops: usize,
        namespace: Option<Namespace>,
        max_results: usize,
    ) -> Result<Vec<MemoryNote>> {
        self.graph_traverse_with_limit(seed_ids, max_hops, namespace, Some(max_results))
            .await
    }

    async fn find_consolidation_candidates(
        &self,
        namespace: Option<Namespace>,
    ) -> Result<Vec<(MemoryNote, MemoryNote)>> {
        debug!(
            "Finding consolidation candidates (namespace: {:?})",
            namespace
        );

        let conn = self.get_conn()?;
        let search_namespace = namespace.clone();
        let sql = if self.schema_type == SchemaType::StandardSQLite {
            if namespace.is_some() {
                format!(
                    "SELECT {columns}, e.embedding FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id WHERE m.namespace = ? AND m.is_archived = 0 AND m.memory_class = 'knowledge' AND m.tags NOT LIKE '%\"turn_sync\"%' AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) LIMIT 100",
                    columns = self.memory_columns("m")
                )
            } else {
                format!(
                    "SELECT {columns}, e.embedding FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id WHERE m.is_archived = 0 AND m.memory_class = 'knowledge' AND m.tags NOT LIKE '%\"turn_sync\"%' AND (m.expires_at IS NULL OR datetime(m.expires_at) > datetime('now')) LIMIT 100",
                    columns = self.memory_columns("m")
                )
            }
        } else if namespace.is_some() {
            format!(
                "SELECT {} FROM memories WHERE namespace = ? AND is_archived = 0 AND memory_class = 'knowledge' AND tags NOT LIKE '%\"turn_sync\"%' AND (expires_at IS NULL OR datetime(expires_at) > datetime('now')) AND embedding IS NOT NULL LIMIT 100",
                self.memory_columns("")
            )
        } else {
            format!(
                "SELECT {} FROM memories WHERE is_archived = 0 AND memory_class = 'knowledge' AND tags NOT LIKE '%\"turn_sync\"%' AND (expires_at IS NULL OR datetime(expires_at) > datetime('now')) AND embedding IS NOT NULL LIMIT 100",
                self.memory_columns("")
            )
        };

        let mut rows = if let Some(ns) = namespace {
            let ns_json = serde_json::to_string(&ns)?;
            conn.query(&sql, params![ns_json]).await?
        } else {
            conn.query(&sql, params![]).await?
        };

        let mut memories = Vec::new();
        while let Some(row) = rows.next().await? {
            let mut memory = self.row_to_memory(&row).await?;
            if self.schema_type == SchemaType::StandardSQLite {
                memory.embedding =
                    Some(decode_embedding_from_row(&row, self.memory_column_count())?);
            }
            memories.push(memory);
        }

        debug!(
            "Found {} memories to compare for consolidation",
            memories.len()
        );

        let mut candidates = Vec::new();
        let similarity_threshold = 0.85;

        for i in 0..memories.len() {
            if let Some(ref embedding_i) = memories[i].embedding {
                let similar = self
                    .vector_search(embedding_i, 5, search_namespace.clone())
                    .await?;
                for (memory_id, similarity) in similar {
                    if memory_id == memories[i].id {
                        continue;
                    }
                    if similarity >= similarity_threshold {
                        let should_add = memories
                            .iter()
                            .position(|m| m.id == memory_id)
                            .map(|j| i < j)
                            .unwrap_or(false);

                        if should_add {
                            // Fetch the similar memory
                            if let Ok(similar_memory) = self.get_memory(memory_id).await {
                                debug!(
                                    "Consolidation candidate: {} <-> {} (similarity: {:.2})",
                                    memories[i].id, memory_id, similarity
                                );
                                candidates.push((memories[i].clone(), similar_memory));
                            }
                        }
                    }
                }
            }
        }

        debug!("Found {} consolidation candidate pairs", candidates.len());
        Ok(candidates)
    }

    async fn increment_access(&self, id: MemoryId) -> Result<()> {
        let conn = self.get_conn()?;
        conn.execute(
            r#"
            UPDATE memories
            SET access_count = access_count + 1,
                last_accessed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            "#,
            params![id.to_string()],
        )
        .await?;

        Ok(())
    }

    async fn count_memories(&self, namespace: Option<Namespace>) -> Result<usize> {
        let conn = self.get_conn()?;
        let (sql, params_vec) = if let Some(ns) = namespace {
            let ns_str = serde_json::to_string(&ns)?;
            (
                "SELECT COUNT(*) FROM memories WHERE namespace = ? AND is_archived = 0 AND tags NOT LIKE '%\"turn_sync\"%'",
                vec![ns_str],
            )
        } else {
            (
                "SELECT COUNT(*) FROM memories WHERE is_archived = 0 AND tags NOT LIKE '%\"turn_sync\"%'",
                vec![],
            )
        };

        let mut rows = if params_vec.is_empty() {
            conn.query(&sql, params![]).await?
        } else {
            conn.query(sql, params![params_vec[0].clone()]).await?
        };

        if let Some(row) = rows.next().await? {
            let count: i64 = row.get(0)?;
            Ok(count as usize)
        } else {
            Ok(0)
        }
    }

    async fn hybrid_search(
        &self,
        query: &str,
        namespace: Option<Namespace>,
        max_results: usize,
        expand_graph: bool,
    ) -> Result<Vec<SearchResult>> {
        debug!("Hybrid search: {} (expand_graph: {})", query, expand_graph);

        let effective_weights = self.retrieval_weights().await;
        let trace_namespace = namespace.as_ref().map(ToString::to_string);
        let mut fallback_reasons = Vec::new();
        // Collect scores from different sources
        let mut memory_scores: std::collections::HashMap<MemoryId, (f32, f32, f32, f32)> =
            std::collections::HashMap::new(); // (keyword, vector, graph, depth)
        let mut entity_ids = std::collections::HashSet::new();

        // 1. Keyword search
        let keyword_results = self.keyword_search(query, namespace.clone()).await?;
        debug!("Keyword search found {} results", keyword_results.len());

        for result in &keyword_results {
            memory_scores.insert(result.memory.id, (result.score, 0.0, 0.0, 0.0));
        }

        // 2. Vector search (if embedding service available and query non-empty)
        // Ranked vector channel results, kept ordered best-first for fusion.
        let mut vector_results: Vec<(MemoryId, f32)> = Vec::new();
        let mut graph_candidate_count = 0usize;
        if !query.is_empty()
            && self.embedding_service.is_some()
            && self.search_config.enable_vector_search
        {
            // Generate query embedding
            if let Some(service) = &self.embedding_service {
                match service.embed(query).await {
                    Ok(query_embedding) => {
                        // Perform vector search with a wide candidate pool:
                        // fusion and later reranking can only rescue relevant
                        // rows that survive candidate selection.
                        let fresh_vector_results = self
                            .vector_search(&query_embedding, max_results * 4, namespace.clone())
                            .await?;
                        debug!("Vector search found {} results", fresh_vector_results.len());

                        vector_results = fresh_vector_results;
                        for (memory_id, similarity) in &vector_results {
                            let entry = memory_scores
                                .entry(*memory_id)
                                .or_insert((0.0, 0.0, 0.0, 0.0));
                            entry.1 = *similarity; // Update vector score
                        }
                    }
                    Err(e) => {
                        fallback_reasons.push("query_embedding_unavailable".to_string());
                        // Fail-closed retrieval: if a ranking signal fails, do not
                        // silently serve keyword-only (unranked) results.
                        if self.search_config.fail_closed {
                            let mut trace = crate::utils::retrieval::RetrievalTrace::for_query(
                                query,
                                effective_weights,
                            );
                            trace.namespace = trace_namespace.clone();
                            trace.keyword_candidates = keyword_results.len();
                            trace.fallback_reasons = vec![
                                "query_embedding_unavailable".to_string(),
                                "fail_closed".to_string(),
                            ];
                            if let Err(error) = self.record_retrieval_trace(&trace).await {
                                warn!("failed to persist retrieval trace: {}", error);
                            }
                            return Err(MnemosyneError::Database(format!(
                                "fail-closed retrieval: query embedding generation failed: {}",
                                e
                            )));
                        }
                        warn!(
                            "Failed to generate query embedding (fail-open degradation): {}",
                            e
                        );
                    }
                }
            }
        } else if !query.is_empty() && self.search_config.enable_vector_search {
            fallback_reasons.push("vector_search_unavailable".to_string());
        }

        // 3. Exact entity anchors. This is a union signal, never a hard
        // intersection, so ordinary keyword/vector recall remains resilient.
        if !query.is_empty() {
            for id in self.entity_memory_ids(query, namespace.clone()).await? {
                entity_ids.insert(id);
                memory_scores.entry(id).or_insert((0.0, 0.0, 0.0, 0.0));
            }
        }

        // 4. Graph expansion (if enabled)
        let use_graph = expand_graph && self.search_config.enable_graph_expansion;
        if use_graph && !memory_scores.is_empty() {
            debug!("Expanding graph from {} seed memories", memory_scores.len());
            let seed_ids = Self::select_graph_seed_ids(&memory_scores, 5);
            let graph_memories = self
                .graph_traverse_bounded(
                    &seed_ids,
                    self.search_config.max_graph_depth,
                    namespace.clone(),
                    max_results.min(1000),
                )
                .await?;

            graph_candidate_count = graph_memories.len();
            for memory in graph_memories {
                let entry = memory_scores
                    .entry(memory.id)
                    .or_insert((0.0, 0.0, 0.0, 1.0));
                entry.2 = 1.0; // Mark as graph-expanded
                entry.3 = entry.3.min(1.0); // Update depth
            }
        }

        // If no results from any source, return empty
        if memory_scores.is_empty() {
            let mut trace =
                crate::utils::retrieval::RetrievalTrace::for_query(query, effective_weights);
            trace.namespace = trace_namespace.clone();
            trace.keyword_candidates = keyword_results.len();
            trace.vector_candidates = vector_results.len();
            trace.fallback_reasons = fallback_reasons;
            if let Err(error) = self.record_retrieval_trace(&trace).await {
                warn!("failed to persist retrieval trace: {}", error);
            }
            debug!("No results from any search source");
            return Ok(vec![]);
        }

        // Fetch all candidates in a single batched read instead of one
        // get_memory round trip per candidate (two SQL queries per candidate
        // in the previous per-id loop).
        let candidate_ids: Vec<MemoryId> = memory_scores.keys().copied().collect();
        let memories = self.get_memories_batch(&candidate_ids).await?;

        // Compute final scores
        let now = Utc::now();
        let mut scored_results = Vec::new();

        for (memory_id, (keyword_score, vector_score, graph_score, depth)) in memory_scores {
            // Take the pre-fetched memory
            let memory = match memories.get(&memory_id) {
                Some(m) => m.clone(),
                None => {
                    warn!("Failed to fetch memory {}: not found in batch", memory_id);
                    continue;
                }
            };
            if memory.is_archived {
                continue;
            }

            // Compute component scores
            let importance_score = memory.importance as f32 / 10.0;
            let recency_score =
                bounded_recency_score(now, memory.created_at, memory.last_accessed_at);
            let graph_depth_score = if graph_score > 0.0 {
                1.0 / (1.0 + depth)
            } else {
                0.0
            };

            // Compute weighted final score using config weights
            let entity_score = if entity_ids.contains(&memory_id) {
                1.0
            } else {
                0.0
            };
            let final_score = (effective_weights.keyword * keyword_score
                + effective_weights.vector * vector_score
                + effective_weights.graph * graph_depth_score
                + self.search_config.importance_weight * importance_score
                + self.search_config.recency_weight * recency_score
                + 0.15 * entity_score)
                .clamp(0.0, 1.0);

            // Determine match reason
            let match_reason = if entity_score > 0.0 {
                format!("entity_anchor ({:.2})", final_score)
            } else if vector_score > keyword_score && vector_score > graph_depth_score {
                format!("vector_similarity ({:.2})", final_score)
            } else if keyword_score > 0.0 {
                format!("keyword_match ({:.2})", final_score)
            } else {
                format!("graph_expansion ({:.2})", final_score)
            };

            scored_results.push(SearchResult {
                memory,
                score: final_score,
                match_reason,
            });
        }

        // Sort by score, apply query-term coverage rescoring (one-token OR
        // matches lose to multi-term coverage), then limit results.
        // Handle potential NaN values gracefully - treat them as lowest priority
        scored_results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Less)
        });
        crate::utils::retrieval::apply_coverage_rescore(query, &mut scored_results);
        scored_results.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Less)
        });
        scored_results.truncate(max_results);

        let mut trace =
            crate::utils::retrieval::RetrievalTrace::for_query(query, effective_weights);
        trace.namespace = trace_namespace;
        trace.keyword_candidates = keyword_results.len();
        trace.vector_candidates = vector_results.len();
        trace.graph_candidates = graph_candidate_count;
        trace.fallback_reasons = fallback_reasons;
        trace.result_ids = scored_results
            .iter()
            .map(|result| result.memory.id.to_string())
            .collect();
        if let Err(error) = self.record_retrieval_trace(&trace).await {
            warn!("failed to persist retrieval trace: {}", error);
        }
        debug!("Hybrid search returned {} results", scored_results.len());
        Ok(scored_results)
    }

    async fn record_retrieval_trace(
        &self,
        trace: crate::utils::retrieval::RetrievalTrace,
    ) -> Result<()> {
        self.record_retrieval_trace(&trace).await
    }

    async fn retrieval_weights(&self) -> crate::utils::retrieval::RetrievalWeights {
        self.retrieval_weights().await
    }

    async fn record_retrieval_use(&self, memory_ids: &[MemoryId]) -> Result<()> {
        self.record_retrieval_use(memory_ids).await
    }

    async fn harvest_retrieval_golden_item(
        &self,
        query: &str,
        relevant_memory_ids: &[MemoryId],
        namespace: Option<Namespace>,
    ) -> Result<()> {
        self.harvest_retrieval_golden_item(query, relevant_memory_ids, namespace)
            .await
    }

    async fn run_retrieval_evaluation(
        &self,
        max_samples: usize,
    ) -> Result<crate::utils::retrieval::RetrievalEvaluationReport> {
        self.run_retrieval_evaluation(max_samples).await
    }

    async fn interaction_policy_search(
        &self,
        query: &str,
        max_results: usize,
    ) -> Result<Vec<SearchResult>> {
        self.search_interaction_policies(query, max_results).await
    }

    async fn list_approved_constraints(
        &self,
        namespace: &Namespace,
        limit: usize,
    ) -> Result<Vec<ConstraintProposalRecord>> {
        let proposals = self
            .list_constraint_proposals(Some(namespace), Some("approved"), limit)
            .await?;
        let mut live = Vec::with_capacity(proposals.len());
        for proposal in proposals {
            let source_ids: Vec<String> = serde_json::from_str(&proposal.source_memory_ids)?;
            let evidence_quotes: Vec<String> = serde_json::from_str(&proposal.evidence_quotes)?;
            let mut sources = Vec::with_capacity(source_ids.len());
            let mut valid = true;
            for source_id in source_ids {
                let source_id = MemoryId::from_string(&source_id)?;
                match self.get_memory(source_id).await {
                    Ok(memory) if !memory.is_archived => sources.push(memory),
                    _ => {
                        valid = false;
                        break;
                    }
                }
            }
            if valid
                && evidence_quotes.iter().all(|quote| {
                    sources.iter().any(|source| {
                        source.content.contains(quote) || source.summary.contains(quote)
                    })
                })
                && proposal.valid_until.as_deref().is_none_or(|value| {
                    chrono::DateTime::parse_from_rfc3339(value)
                        .map(|until| until.with_timezone(&Utc) > Utc::now())
                        .unwrap_or(false)
                })
            {
                live.push(proposal);
            }
        }
        Ok(live)
    }

    async fn list_memories(
        &self,
        namespace: Option<Namespace>,
        limit: usize,
        sort_by: crate::storage::MemorySortOrder,
    ) -> Result<Vec<MemoryNote>> {
        use crate::storage::MemorySortOrder;

        debug!(
            "Listing memories (namespace: {:?}, limit: {}, sort: {:?})",
            namespace, limit, sort_by
        );

        let conn = self.get_conn()?;
        let order_clause = match sort_by {
            MemorySortOrder::Recent => "created_at DESC",
            MemorySortOrder::Importance => "importance DESC, created_at DESC",
            MemorySortOrder::AccessCount => "access_count DESC, created_at DESC",
        };

        let (sql, params_vec) = if let Some(ns) = namespace {
            let ns_str = serde_json::to_string(&ns)?;
            (
                format!(
                    "SELECT {} FROM memories WHERE namespace = ? AND is_archived = 0 AND tags NOT LIKE '%\"turn_sync\"%' ORDER BY {} LIMIT ?",
                    self.memory_columns(""),
                    order_clause
                ),
                vec![ns_str],
            )
        } else {
            (
                format!(
                    "SELECT {} FROM memories WHERE is_archived = 0 AND tags NOT LIKE '%\"turn_sync\"%' ORDER BY {} LIMIT ?",
                    self.memory_columns(""),
                    order_clause
                ),
                vec![],
            )
        };

        let mut rows = if params_vec.is_empty() {
            conn.query(&sql, params![limit as i64]).await?
        } else {
            conn.query(&sql, params![params_vec[0].clone(), limit as i64])
                .await?
        };

        let mut memories = Vec::new();
        while let Some(row) = rows.next().await? {
            memories.push(self.row_to_memory(&row).await?);
        }

        debug!("Listed {} memories", memories.len());
        Ok(memories)
    }

    async fn store_modification_log(
        &self,
        log: &crate::agents::access_control::ModificationLog,
    ) -> Result<()> {
        debug!(
            "Storing modification log: {} for memory {}",
            log.id, log.memory_id
        );

        let conn = self.get_conn()?;

        conn.execute(
            r#"
            INSERT INTO memory_modification_log (id, memory_id, agent_role, modification_type, timestamp, changes)
            VALUES (?, ?, ?, ?, ?, ?)
            "#,
            params![
                log.id.clone(),
                log.memory_id.to_string(),
                log.agent_role.to_string(),
                log.modification_type.to_string(),
                log.timestamp.timestamp(),
                log.changes.clone(),
            ],
        )
        .await?;

        debug!("Modification log stored successfully: {}", log.id);
        Ok(())
    }

    async fn get_audit_trail(
        &self,
        memory_id: MemoryId,
    ) -> Result<Vec<crate::agents::access_control::ModificationLog>> {
        debug!("Fetching audit trail for memory: {}", memory_id);

        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                r#"
                SELECT id, memory_id, agent_role, modification_type, timestamp, changes
                FROM memory_modification_log
                WHERE memory_id = ?
                ORDER BY timestamp DESC
                "#,
                params![memory_id.to_string()],
            )
            .await?;

        let mut logs = Vec::new();
        while let Some(row) = rows.next().await? {
            let id: String = row.get(0)?;
            let memory_id_str: String = row.get(1)?;
            let agent_role_str: String = row.get(2)?;
            let modification_type_str: String = row.get(3)?;
            let timestamp: i64 = row.get(4)?;
            let changes: Option<String> = row.get(5)?;

            // Parse memory_id
            let memory_id = MemoryId::from_string(&memory_id_str)?;

            // Parse agent_role
            let agent_role = crate::agents::AgentRole::from_str(&agent_role_str)
                .map_err(|e| MnemosyneError::Other(format!("Invalid agent role: {}", e)))?;

            // Parse modification_type
            let modification_type = match modification_type_str.as_str() {
                "create" => crate::agents::access_control::ModificationType::Create,
                "update" => crate::agents::access_control::ModificationType::Update,
                "delete" => crate::agents::access_control::ModificationType::Delete,
                "archive" => crate::agents::access_control::ModificationType::Archive,
                "unarchive" => crate::agents::access_control::ModificationType::Unarchive,
                "supersede" => crate::agents::access_control::ModificationType::Supersede,
                _ => {
                    return Err(MnemosyneError::Other(format!(
                        "Unknown modification type: {}",
                        modification_type_str
                    )))
                }
            };

            // Convert timestamp to DateTime
            let timestamp =
                chrono::DateTime::<Utc>::from_timestamp(timestamp, 0).ok_or_else(|| {
                    MnemosyneError::Other(format!("Invalid timestamp: {}", timestamp))
                })?;

            logs.push(crate::agents::access_control::ModificationLog {
                id,
                memory_id,
                agent_role,
                modification_type,
                timestamp,
                changes,
            });
        }

        debug!("Fetched {} audit trail entries", logs.len());
        Ok(logs)
    }

    async fn get_modification_stats(
        &self,
        agent_role: crate::agents::AgentRole,
    ) -> Result<Vec<(crate::agents::access_control::ModificationType, u32)>> {
        debug!("Fetching modification stats for agent: {}", agent_role);

        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                r#"
                SELECT modification_type, COUNT(*) as count
                FROM memory_modification_log
                WHERE agent_role = ?
                GROUP BY modification_type
                ORDER BY count DESC
                "#,
                params![agent_role.to_string()],
            )
            .await?;

        let mut stats = Vec::new();
        while let Some(row) = rows.next().await? {
            let modification_type_str: String = row.get(0)?;
            let count: i64 = row.get(1)?;

            // Parse modification_type
            let modification_type = match modification_type_str.as_str() {
                "create" => crate::agents::access_control::ModificationType::Create,
                "update" => crate::agents::access_control::ModificationType::Update,
                "delete" => crate::agents::access_control::ModificationType::Delete,
                "archive" => crate::agents::access_control::ModificationType::Archive,
                "unarchive" => crate::agents::access_control::ModificationType::Unarchive,
                "supersede" => crate::agents::access_control::ModificationType::Supersede,
                _ => continue, // Skip unknown types
            };

            stats.push((modification_type, count as u32));
        }

        debug!("Fetched {} modification stats", stats.len());
        Ok(stats)
    }

    /// Store a work item for cross-session persistence
    async fn store_work_item(&self, item: &crate::orchestration::state::WorkItem) -> Result<()> {
        debug!("Storing work item: {:?}", item.id);
        let conn = self.get_conn()?;

        // Serialize complex fields to JSON
        let dependencies_json = serde_json::to_string(&item.dependencies).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize dependencies: {}", e))
        })?;

        let review_feedback_json = serde_json::to_string(&item.review_feedback).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize review_feedback: {}", e))
        })?;

        let suggested_tests_json = serde_json::to_string(&item.suggested_tests).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize suggested_tests: {}", e))
        })?;

        let execution_memory_ids_json =
            serde_json::to_string(&item.execution_memory_ids).map_err(|e| {
                MnemosyneError::Database(format!("Failed to serialize execution_memory_ids: {}", e))
            })?;

        let file_scope_json = serde_json::to_string(&item.file_scope).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize file_scope: {}", e))
        })?;

        // Serialize requirement tracking fields
        let requirements_json = serde_json::to_string(&item.requirements).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize requirements: {}", e))
        })?;

        let requirement_status_json =
            serde_json::to_string(&item.requirement_status).map_err(|e| {
                MnemosyneError::Database(format!("Failed to serialize requirement_status: {}", e))
            })?;

        let implementation_evidence_json = serde_json::to_string(&item.implementation_evidence)
            .map_err(|e| {
                MnemosyneError::Database(format!(
                    "Failed to serialize implementation_evidence: {}",
                    e
                ))
            })?;

        // Convert timestamps to Unix epoch milliseconds
        let created_at = item.created_at.timestamp_millis();
        let started_at = item.started_at.map(|t| t.timestamp_millis());
        let completed_at = item.completed_at.map(|t| t.timestamp_millis());

        // Convert AgentState, Phase, and AgentRole to strings
        let state_str = format!("{:?}", item.state);
        let phase_str = format!("{:?}", item.phase);
        let agent_role_str = format!("{:?}", item.agent);

        // Convert timeout duration to seconds
        let timeout_secs = item.timeout.map(|d| d.as_secs() as i64);

        // Convert consolidated_context_id to string
        let consolidated_context_id_str = item.consolidated_context_id.map(|id| id.to_string());

        conn.execute(
            r#"
            INSERT INTO work_items (
                id, description, original_intent, agent_role, state, phase, priority,
                dependencies, created_at, started_at, completed_at, error, timeout_secs,
                review_feedback, suggested_tests, review_attempt,
                execution_memory_ids, consolidated_context_id, estimated_context_tokens,
                assigned_branch, file_scope, requirements, requirement_status, implementation_evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
            params![
                item.id.to_string(),
                item.description.clone(),
                item.original_intent.clone(),
                agent_role_str,
                state_str,
                phase_str,
                item.priority as i64,
                dependencies_json,
                created_at,
                started_at,
                completed_at,
                item.error.clone(),
                timeout_secs,
                review_feedback_json,
                suggested_tests_json,
                item.review_attempt as i64,
                execution_memory_ids_json,
                consolidated_context_id_str,
                item.estimated_context_tokens as i64,
                item.assigned_branch.clone(),
                file_scope_json,
                requirements_json,
                requirement_status_json,
                implementation_evidence_json,
            ],
        )
        .await
        .map_err(|e| MnemosyneError::Database(format!("Failed to store work item: {}", e)))?;

        debug!("Work item stored successfully: {:?}", item.id);
        Ok(())
    }

    /// Load a work item by ID
    async fn load_work_item(
        &self,
        id: &crate::orchestration::state::WorkItemId,
    ) -> Result<crate::orchestration::state::WorkItem> {
        debug!("Loading work item: {:?}", id);
        let conn = self.get_conn()?;

        let id_str = id.to_string();

        let mut stmt = conn
            .prepare(
                r#"
                SELECT id, description, original_intent, agent_role, state, phase, priority,
                       dependencies, created_at, started_at, completed_at, error, timeout_secs,
                       review_feedback, suggested_tests, review_attempt,
                       execution_memory_ids, consolidated_context_id, estimated_context_tokens,
                       assigned_branch, file_scope, requirements, requirement_status, implementation_evidence
                FROM work_items
                WHERE id = ?
                "#,
            )
            .await
            .map_err(|e| {
                MnemosyneError::Database(format!("Failed to prepare load_work_item query: {}", e))
            })?;

        let row = stmt
            .query_row(params![id_str.as_str()])
            .await
            .map_err(|e| MnemosyneError::NotFound(format!("Work item not found: {}", e)))?;

        // Parse fields from row with proper error handling
        let description: String = row.get(1).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get description from row: {}", e))
        })?;
        let original_intent: String = row.get(2).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get original_intent from row: {}", e))
        })?;
        let agent_role_str: String = row.get(3).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get agent_role from row: {}", e))
        })?;
        let state_str: String = row.get(4).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get state from row: {}", e))
        })?;
        let phase_str: String = row.get(5).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get phase from row: {}", e))
        })?;
        let priority: i64 = row.get(6).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get priority from row: {}", e))
        })?;
        let dependencies_json: String = row.get(7).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get dependencies from row: {}", e))
        })?;
        let created_at_ms: i64 = row.get(8).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get created_at from row: {}", e))
        })?;
        let started_at_ms: Option<i64> = row.get(9).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get started_at from row: {}", e))
        })?;
        let completed_at_ms: Option<i64> = row.get(10).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get completed_at from row: {}", e))
        })?;
        let error: Option<String> = row.get(11).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get error from row: {}", e))
        })?;
        let timeout_secs: Option<i64> = row.get(12).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get timeout_secs from row: {}", e))
        })?;
        let review_feedback_json: String = row.get(13).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get review_feedback from row: {}", e))
        })?;
        let suggested_tests_json: String = row.get(14).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get suggested_tests from row: {}", e))
        })?;
        let review_attempt: i64 = row.get(15).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get review_attempt from row: {}", e))
        })?;
        let execution_memory_ids_json: String = row.get(16).map_err(|e| {
            MnemosyneError::Database(format!(
                "Failed to get execution_memory_ids from row: {}",
                e
            ))
        })?;
        let consolidated_context_id_str: Option<String> = row.get(17).map_err(|e| {
            MnemosyneError::Database(format!(
                "Failed to get consolidated_context_id from row: {}",
                e
            ))
        })?;
        let estimated_context_tokens: i64 = row.get(18).map_err(|e| {
            MnemosyneError::Database(format!(
                "Failed to get estimated_context_tokens from row: {}",
                e
            ))
        })?;
        let assigned_branch: Option<String> = row.get(19).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get assigned_branch from row: {}", e))
        })?;
        let file_scope_json: String = row.get(20).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get file_scope from row: {}", e))
        })?;
        let requirements_json: String = row.get(21).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get requirements from row: {}", e))
        })?;
        let requirement_status_json: String = row.get(22).map_err(|e| {
            MnemosyneError::Database(format!("Failed to get requirement_status from row: {}", e))
        })?;
        let implementation_evidence_json: String = row.get(23).map_err(|e| {
            MnemosyneError::Database(format!(
                "Failed to get implementation_evidence from row: {}",
                e
            ))
        })?;

        // Deserialize JSON fields
        let dependencies: Vec<crate::orchestration::state::WorkItemId> =
            serde_json::from_str(&dependencies_json).map_err(|e| {
                MnemosyneError::Database(format!("Failed to deserialize dependencies: {}", e))
            })?;

        let review_feedback: Option<Vec<String>> = serde_json::from_str(&review_feedback_json)
            .map_err(|e| {
                MnemosyneError::Database(format!("Failed to deserialize review_feedback: {}", e))
            })?;

        let suggested_tests: Option<Vec<String>> = serde_json::from_str(&suggested_tests_json)
            .map_err(|e| {
                MnemosyneError::Database(format!("Failed to deserialize suggested_tests: {}", e))
            })?;

        let execution_memory_ids: Vec<crate::types::MemoryId> =
            serde_json::from_str(&execution_memory_ids_json).map_err(|e| {
                MnemosyneError::Database(format!(
                    "Failed to deserialize execution_memory_ids: {}",
                    e
                ))
            })?;

        let file_scope: Option<Vec<std::path::PathBuf>> = serde_json::from_str(&file_scope_json)
            .map_err(|e| {
                MnemosyneError::Database(format!("Failed to deserialize file_scope: {}", e))
            })?;

        let requirements: Vec<String> = serde_json::from_str(&requirements_json).map_err(|e| {
            MnemosyneError::Database(format!("Failed to deserialize requirements: {}", e))
        })?;

        let requirement_status: std::collections::HashMap<
            String,
            crate::orchestration::state::RequirementStatus,
        > = serde_json::from_str(&requirement_status_json).map_err(|e| {
            MnemosyneError::Database(format!("Failed to deserialize requirement_status: {}", e))
        })?;

        let implementation_evidence: std::collections::HashMap<
            String,
            Vec<crate::types::MemoryId>,
        > = serde_json::from_str(&implementation_evidence_json).map_err(|e| {
            MnemosyneError::Database(format!(
                "Failed to deserialize implementation_evidence: {}",
                e
            ))
        })?;

        // Parse enums using string matching
        let agent = match agent_role_str.as_str() {
            "Orchestrator" => crate::launcher::agents::AgentRole::Orchestrator,
            "Optimizer" => crate::launcher::agents::AgentRole::Optimizer,
            "Executor" => crate::launcher::agents::AgentRole::Executor,
            "Reviewer" => crate::launcher::agents::AgentRole::Reviewer,
            _ => {
                return Err(MnemosyneError::Database(format!(
                    "Invalid agent_role: {}",
                    agent_role_str
                )))
            }
        };

        let state = match state_str.as_str() {
            "Idle" => crate::orchestration::state::AgentState::Idle,
            "Ready" => crate::orchestration::state::AgentState::Ready,
            "Active" => crate::orchestration::state::AgentState::Active,
            "Waiting" => crate::orchestration::state::AgentState::Waiting,
            "Blocked" => crate::orchestration::state::AgentState::Blocked,
            "PendingReview" => crate::orchestration::state::AgentState::PendingReview,
            "Complete" => crate::orchestration::state::AgentState::Complete,
            "Error" => crate::orchestration::state::AgentState::Error,
            _ => {
                return Err(MnemosyneError::Database(format!(
                    "Invalid state: {}",
                    state_str
                )))
            }
        };

        let phase = match phase_str.as_str() {
            "PromptToSpec" => crate::orchestration::state::Phase::PromptToSpec,
            "SpecToFullSpec" => crate::orchestration::state::Phase::SpecToFullSpec,
            "FullSpecToPlan" => crate::orchestration::state::Phase::FullSpecToPlan,
            "PlanToArtifacts" => crate::orchestration::state::Phase::PlanToArtifacts,
            "Complete" => crate::orchestration::state::Phase::Complete,
            _ => {
                return Err(MnemosyneError::Database(format!(
                    "Invalid phase: {}",
                    phase_str
                )))
            }
        };

        // Parse timestamps
        let created_at = chrono::DateTime::<chrono::Utc>::from_timestamp_millis(created_at_ms)
            .ok_or_else(|| {
                MnemosyneError::Database(format!("Invalid created_at timestamp: {}", created_at_ms))
            })?;

        let started_at = started_at_ms
            .map(|ms| {
                chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).ok_or_else(|| {
                    MnemosyneError::Database(format!("Invalid started_at timestamp: {}", ms))
                })
            })
            .transpose()?;

        let completed_at = completed_at_ms
            .map(|ms| {
                chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).ok_or_else(|| {
                    MnemosyneError::Database(format!("Invalid completed_at timestamp: {}", ms))
                })
            })
            .transpose()?;

        // Parse timeout duration
        let timeout = timeout_secs.map(|secs| std::time::Duration::from_secs(secs as u64));

        // Parse consolidated_context_id
        let consolidated_context_id = consolidated_context_id_str
            .map(|s| {
                crate::types::MemoryId::from_string(&s).map_err(|e| {
                    MnemosyneError::Database(format!(
                        "Failed to parse consolidated_context_id: {}",
                        e
                    ))
                })
            })
            .transpose()?;

        // Reconstruct WorkItem
        let work_item = crate::orchestration::state::WorkItem {
            id: id.clone(),
            description,
            original_intent,
            agent,
            state,
            phase,
            priority: priority as u8,
            dependencies,
            created_at,
            started_at,
            completed_at,
            error,
            timeout,
            assigned_branch,
            estimated_duration: None, // Not persisted
            file_scope,
            review_feedback,
            suggested_tests,
            review_attempt: review_attempt as u32,
            execution_memory_ids,
            consolidated_context_id,
            estimated_context_tokens: estimated_context_tokens as usize,
            requirements,
            requirement_status,
            implementation_evidence,
        };

        debug!("Work item loaded successfully: {:?}", id);
        Ok(work_item)
    }

    /// Update an existing work item
    async fn update_work_item(&self, item: &crate::orchestration::state::WorkItem) -> Result<()> {
        debug!("Updating work item: {:?}", item.id);
        let conn = self.get_conn()?;

        // Serialize complex fields to JSON
        let dependencies_json = serde_json::to_string(&item.dependencies).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize dependencies: {}", e))
        })?;

        let review_feedback_json = serde_json::to_string(&item.review_feedback).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize review_feedback: {}", e))
        })?;

        let suggested_tests_json = serde_json::to_string(&item.suggested_tests).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize suggested_tests: {}", e))
        })?;

        let execution_memory_ids_json =
            serde_json::to_string(&item.execution_memory_ids).map_err(|e| {
                MnemosyneError::Database(format!("Failed to serialize execution_memory_ids: {}", e))
            })?;

        let file_scope_json = serde_json::to_string(&item.file_scope).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize file_scope: {}", e))
        })?;

        let requirements_json = serde_json::to_string(&item.requirements).map_err(|e| {
            MnemosyneError::Database(format!("Failed to serialize requirements: {}", e))
        })?;

        let requirement_status_json =
            serde_json::to_string(&item.requirement_status).map_err(|e| {
                MnemosyneError::Database(format!("Failed to serialize requirement_status: {}", e))
            })?;

        let implementation_evidence_json = serde_json::to_string(&item.implementation_evidence)
            .map_err(|e| {
                MnemosyneError::Database(format!(
                    "Failed to serialize implementation_evidence: {}",
                    e
                ))
            })?;

        // Convert timestamps to Unix epoch milliseconds
        let started_at = item.started_at.map(|t| t.timestamp_millis());
        let completed_at = item.completed_at.map(|t| t.timestamp_millis());

        // Convert AgentState, Phase, and AgentRole to strings
        let state_str = format!("{:?}", item.state);
        let phase_str = format!("{:?}", item.phase);
        let agent_role_str = format!("{:?}", item.agent);

        // Convert timeout duration to seconds
        let timeout_secs = item.timeout.map(|d| d.as_secs() as i64);

        // Convert consolidated_context_id to string
        let consolidated_context_id_str = item.consolidated_context_id.map(|id| id.to_string());

        conn.execute(
            r#"
            UPDATE work_items SET
                description = ?,
                original_intent = ?,
                agent_role = ?,
                state = ?,
                phase = ?,
                priority = ?,
                dependencies = ?,
                started_at = ?,
                completed_at = ?,
                error = ?,
                timeout_secs = ?,
                review_feedback = ?,
                suggested_tests = ?,
                review_attempt = ?,
                execution_memory_ids = ?,
                consolidated_context_id = ?,
                estimated_context_tokens = ?,
                assigned_branch = ?,
                file_scope = ?,
                requirements = ?,
                requirement_status = ?,
                implementation_evidence = ?
            WHERE id = ?
            "#,
            params![
                item.description.clone(),
                item.original_intent.clone(),
                agent_role_str,
                state_str,
                phase_str,
                item.priority as i64,
                dependencies_json,
                started_at,
                completed_at,
                item.error.clone(),
                timeout_secs,
                review_feedback_json,
                suggested_tests_json,
                item.review_attempt as i64,
                execution_memory_ids_json,
                consolidated_context_id_str,
                item.estimated_context_tokens as i64,
                item.assigned_branch.clone(),
                file_scope_json,
                requirements_json,
                requirement_status_json,
                implementation_evidence_json,
                item.id.to_string(),
            ],
        )
        .await
        .map_err(|e| MnemosyneError::Database(format!("Failed to update work item: {}", e)))?;

        debug!("Work item updated successfully: {:?}", item.id);
        Ok(())
    }

    /// Load work items by state (for recovery)
    async fn load_work_items_by_state(
        &self,
        state: crate::orchestration::state::AgentState,
    ) -> Result<Vec<crate::orchestration::state::WorkItem>> {
        self.load_work_items_by_states(&[state]).await
    }

    /// Load work items by multiple states in a single query
    async fn load_work_items_by_states(
        &self,
        states: &[crate::orchestration::state::AgentState],
    ) -> Result<Vec<crate::orchestration::state::WorkItem>> {
        debug!("Loading work items by states: {:?}", states);
        let conn = self.get_conn()?;

        if states.is_empty() {
            return Ok(Vec::new());
        }

        let state_strs: Vec<String> = states.iter().map(|s| format!("{:?}", s)).collect();
        debug!("Querying work items in states: {:?}", state_strs);

        // Build IN clause with dynamic number of placeholders
        let placeholders = (0..states.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let state_params: Vec<libsql::Value> =
            state_strs.into_iter().map(libsql::Value::Text).collect();

        let stmt = conn
            .prepare(
                &format!(
                    r#"
                    SELECT id, description, original_intent, agent_role, state, phase, priority,
                           dependencies, created_at, started_at, completed_at, error, timeout_secs,
                           review_feedback, suggested_tests, review_attempt,
                           execution_memory_ids, consolidated_context_id, estimated_context_tokens,
                           assigned_branch, file_scope, requirements, requirement_status, implementation_evidence
                    FROM work_items
                    WHERE state IN ({placeholders})
                    ORDER BY priority DESC, created_at ASC
                    "#,
                ),
            )
            .await
            .map_err(|e| {
                MnemosyneError::Database(format!(
                    "Failed to prepare load_work_items_by_states query: {}",
                    e
                ))
            })?;

        let mut rows = stmt
            .query(libsql::params_from_iter(state_params))
            .await
            .map_err(|e| {
                MnemosyneError::Database(format!("Failed to query work items by states: {}", e))
            })?;

        let mut work_items = Vec::new();

        // Process each row
        while let Some(row) = rows.next().await.map_err(|e| {
            MnemosyneError::Database(format!("Failed to fetch work item row: {}", e))
        })? {
            // Parse fields from row with proper error handling
            let id_str: String = row.get(0).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get work item id: {}", e))
            })?;
            let description: String = row.get(1).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get description: {}", e))
            })?;
            let original_intent: String = row.get(2).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get original_intent: {}", e))
            })?;
            let agent_role_str: String = row.get(3).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get agent_role: {}", e))
            })?;
            let state_str: String = row
                .get(4)
                .map_err(|e| MnemosyneError::Database(format!("Failed to get state: {}", e)))?;
            let phase_str: String = row
                .get(5)
                .map_err(|e| MnemosyneError::Database(format!("Failed to get phase: {}", e)))?;
            let priority: i64 = row
                .get(6)
                .map_err(|e| MnemosyneError::Database(format!("Failed to get priority: {}", e)))?;
            let dependencies_json: String = row.get(7).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get dependencies: {}", e))
            })?;
            let created_at_ms: i64 = row.get(8).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get created_at: {}", e))
            })?;
            let started_at_ms: Option<i64> = row.get(9).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get started_at: {}", e))
            })?;
            let completed_at_ms: Option<i64> = row.get(10).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get completed_at: {}", e))
            })?;
            let error: Option<String> = row
                .get(11)
                .map_err(|e| MnemosyneError::Database(format!("Failed to get error: {}", e)))?;
            let timeout_secs: Option<i64> = row.get(12).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get timeout_secs: {}", e))
            })?;
            let review_feedback_json: String = row.get(13).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get review_feedback: {}", e))
            })?;
            let suggested_tests_json: String = row.get(14).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get suggested_tests: {}", e))
            })?;
            let review_attempt: i64 = row.get(15).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get review_attempt: {}", e))
            })?;
            let execution_memory_ids_json: String = row.get(16).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get execution_memory_ids: {}", e))
            })?;
            let consolidated_context_id_str: Option<String> = row.get(17).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get consolidated_context_id: {}", e))
            })?;
            let estimated_context_tokens: i64 = row.get(18).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get estimated_context_tokens: {}", e))
            })?;
            let assigned_branch: Option<String> = row.get(19).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get assigned_branch: {}", e))
            })?;
            let file_scope_json: String = row.get(20).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get file_scope: {}", e))
            })?;
            let requirements_json: String = row.get(21).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get requirements: {}", e))
            })?;
            let requirement_status_json: String = row.get(22).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get requirement_status: {}", e))
            })?;
            let implementation_evidence_json: String = row.get(23).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get implementation_evidence: {}", e))
            })?;

            // Deserialize JSON fields
            let dependencies: Vec<crate::orchestration::state::WorkItemId> =
                serde_json::from_str(&dependencies_json).map_err(|e| {
                    MnemosyneError::Database(format!("Failed to deserialize dependencies: {}", e))
                })?;

            let review_feedback: Option<Vec<String>> = serde_json::from_str(&review_feedback_json)
                .map_err(|e| {
                    MnemosyneError::Database(format!(
                        "Failed to deserialize review_feedback: {}",
                        e
                    ))
                })?;

            let suggested_tests: Option<Vec<String>> = serde_json::from_str(&suggested_tests_json)
                .map_err(|e| {
                    MnemosyneError::Database(format!(
                        "Failed to deserialize suggested_tests: {}",
                        e
                    ))
                })?;

            let execution_memory_ids: Vec<crate::types::MemoryId> =
                serde_json::from_str(&execution_memory_ids_json).map_err(|e| {
                    MnemosyneError::Database(format!(
                        "Failed to deserialize execution_memory_ids: {}",
                        e
                    ))
                })?;

            let file_scope: Option<Vec<std::path::PathBuf>> =
                serde_json::from_str(&file_scope_json).map_err(|e| {
                    MnemosyneError::Database(format!("Failed to deserialize file_scope: {}", e))
                })?;

            let requirements: Vec<String> =
                serde_json::from_str(&requirements_json).map_err(|e| {
                    MnemosyneError::Database(format!("Failed to deserialize requirements: {}", e))
                })?;

            let requirement_status: std::collections::HashMap<
                String,
                crate::orchestration::state::RequirementStatus,
            > = serde_json::from_str(&requirement_status_json).map_err(|e| {
                MnemosyneError::Database(format!("Failed to deserialize requirement_status: {}", e))
            })?;

            let implementation_evidence: std::collections::HashMap<
                String,
                Vec<crate::types::MemoryId>,
            > = serde_json::from_str(&implementation_evidence_json).map_err(|e| {
                MnemosyneError::Database(format!(
                    "Failed to deserialize implementation_evidence: {}",
                    e
                ))
            })?;

            // Parse ID (WorkItemId wraps a UUID)
            let uuid = uuid::Uuid::parse_str(&id_str).map_err(|e| {
                MnemosyneError::Database(format!("Invalid work item ID UUID: {}", e))
            })?;
            let id = crate::orchestration::state::WorkItemId::from(uuid);

            // Parse enums using string matching
            let agent = match agent_role_str.as_str() {
                "Orchestrator" => crate::launcher::agents::AgentRole::Orchestrator,
                "Optimizer" => crate::launcher::agents::AgentRole::Optimizer,
                "Executor" => crate::launcher::agents::AgentRole::Executor,
                "Reviewer" => crate::launcher::agents::AgentRole::Reviewer,
                _ => {
                    return Err(MnemosyneError::Database(format!(
                        "Invalid agent_role: {}",
                        agent_role_str
                    )))
                }
            };

            let state_enum = match state_str.as_str() {
                "Idle" => crate::orchestration::state::AgentState::Idle,
                "Ready" => crate::orchestration::state::AgentState::Ready,
                "Active" => crate::orchestration::state::AgentState::Active,
                "Waiting" => crate::orchestration::state::AgentState::Waiting,
                "Blocked" => crate::orchestration::state::AgentState::Blocked,
                "PendingReview" => crate::orchestration::state::AgentState::PendingReview,
                "Complete" => crate::orchestration::state::AgentState::Complete,
                "Error" => crate::orchestration::state::AgentState::Error,
                _ => {
                    return Err(MnemosyneError::Database(format!(
                        "Invalid state: {}",
                        state_str
                    )))
                }
            };

            let phase = match phase_str.as_str() {
                "PromptToSpec" => crate::orchestration::state::Phase::PromptToSpec,
                "SpecToFullSpec" => crate::orchestration::state::Phase::SpecToFullSpec,
                "FullSpecToPlan" => crate::orchestration::state::Phase::FullSpecToPlan,
                "PlanToArtifacts" => crate::orchestration::state::Phase::PlanToArtifacts,
                "Complete" => crate::orchestration::state::Phase::Complete,
                _ => {
                    return Err(MnemosyneError::Database(format!(
                        "Invalid phase: {}",
                        phase_str
                    )))
                }
            };

            // Parse timestamps
            let created_at = chrono::DateTime::<chrono::Utc>::from_timestamp_millis(created_at_ms)
                .ok_or_else(|| {
                    MnemosyneError::Database(format!(
                        "Invalid created_at timestamp: {}",
                        created_at_ms
                    ))
                })?;

            let started_at = started_at_ms
                .map(|ms| {
                    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).ok_or_else(|| {
                        MnemosyneError::Database(format!("Invalid started_at timestamp: {}", ms))
                    })
                })
                .transpose()?;

            let completed_at = completed_at_ms
                .map(|ms| {
                    chrono::DateTime::<chrono::Utc>::from_timestamp_millis(ms).ok_or_else(|| {
                        MnemosyneError::Database(format!("Invalid completed_at timestamp: {}", ms))
                    })
                })
                .transpose()?;

            // Parse timeout duration
            let timeout = timeout_secs.map(|secs| std::time::Duration::from_secs(secs as u64));

            // Parse consolidated_context_id
            let consolidated_context_id = consolidated_context_id_str
                .map(|s| {
                    crate::types::MemoryId::from_string(&s).map_err(|e| {
                        MnemosyneError::Database(format!(
                            "Failed to parse consolidated_context_id: {}",
                            e
                        ))
                    })
                })
                .transpose()?;

            // Reconstruct WorkItem
            let work_item = crate::orchestration::state::WorkItem {
                id,
                description,
                original_intent,
                agent,
                state: state_enum,
                phase,
                priority: priority as u8,
                dependencies,
                created_at,
                started_at,
                completed_at,
                error,
                timeout,
                assigned_branch,
                estimated_duration: None, // Not persisted
                file_scope,
                review_feedback,
                suggested_tests,
                review_attempt: review_attempt as u32,
                execution_memory_ids,
                consolidated_context_id,
                estimated_context_tokens: estimated_context_tokens as usize,
                requirements,
                requirement_status,
                implementation_evidence,
            };

            work_items.push(work_item);
        }

        debug!(
            "Loaded {} work items by states: {:?}",
            work_items.len(),
            states
        );
        Ok(work_items)
    }

    /// Delete a work item (when permanently completed)
    async fn delete_work_item(&self, id: &crate::orchestration::state::WorkItemId) -> Result<()> {
        debug!("Deleting work item: {:?}", id);
        let conn = self.get_conn()?;

        let id_str = id.to_string();

        conn.execute(
            "DELETE FROM work_items WHERE id = ?",
            params![id_str.as_str()],
        )
        .await
        .map_err(|e| MnemosyneError::Database(format!("Failed to delete work item: {}", e)))?;

        debug!("Work item deleted successfully: {:?}", id);
        Ok(())
    }
}

#[cfg(test)]
mod fts_query_tests {
    use super::{LibsqlStorage, MemoryId};
    use std::collections::HashMap;

    #[test]
    fn quotes_apostrophes_for_fts5() {
        assert_eq!(LibsqlStorage::escape_fts5_query("user's"), "\"user's\"");
    }

    #[test]
    fn quotes_plain_terms_too() {
        assert_eq!(LibsqlStorage::escape_fts5_query("memory"), "\"memory\"");
    }

    #[test]
    fn quotes_punctuation_and_boolean_words() {
        assert_eq!(
            LibsqlStorage::escape_fts5_query("schedule."),
            "\"schedule.\""
        );
        assert_eq!(LibsqlStorage::escape_fts5_query("OR"), "\"OR\"");
    }

    #[test]
    fn graph_seeds_are_score_ordered_and_bounded() {
        let high = MemoryId::new();
        let low = MemoryId::new();
        let mut scores = HashMap::new();
        scores.insert(low, (0.2, 0.1, 0.0, 0.0));
        scores.insert(high, (0.8, 0.7, 0.0, 0.0));

        assert_eq!(LibsqlStorage::select_graph_seed_ids(&scores, 1), vec![high]);
    }
}

// Additional implementation methods for LibsqlStorage
impl LibsqlStorage {
    async fn retrieval_setting(conn: &Connection, key: &str, default: &str) -> String {
        let Ok(mut rows) = conn
            .query(
                "SELECT value FROM retrieval_evaluation_config WHERE key = ? LIMIT 1",
                params![key],
            )
            .await
        else {
            return default.to_string();
        };
        rows.next()
            .await
            .ok()
            .flatten()
            .and_then(|row| row.get::<String>(0).ok())
            .unwrap_or_else(|| default.to_string())
    }

    /// Persist a privacy-conscious retrieval explanation. Diagnostics are
    /// enabled by default and failures never change recall behavior.
    pub async fn record_retrieval_trace(
        &self,
        trace: &crate::utils::retrieval::RetrievalTrace,
    ) -> Result<()> {
        let conn = self.get_conn()?;
        let diagnostics_enabled = Self::retrieval_setting(&conn, "diagnostics_enabled", "true")
            .await
            .parse::<bool>()
            .unwrap_or(true)
            && std::env::var("MNEMOSYNE_RECALL_DIAGNOSTICS")
                .map(|value| !matches!(value.to_ascii_lowercase().as_str(), "0" | "false" | "off"))
                .unwrap_or(true);
        if !diagnostics_enabled {
            return Ok(());
        }
        let now = Utc::now().timestamp();
        conn.execute(
            "INSERT OR REPLACE INTO retrieval_traces (id, query_hash, namespace, rewritten_terms, keyword_candidates, vector_candidates, graph_candidates, effective_weights, fallback_reasons, result_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                trace.id.clone(),
                trace.query_hash.clone(),
                trace.namespace.clone(),
                serde_json::to_string(
                    &trace
                        .rewritten_terms
                        .iter()
                        .map(|term| term_fingerprint(term))
                        .collect::<Vec<_>>(),
                )?,
                trace.keyword_candidates as i64,
                trace.vector_candidates as i64,
                trace.graph_candidates as i64,
                serde_json::to_string(&trace.effective_weights)?,
                serde_json::to_string(&trace.fallback_reasons)?,
                serde_json::to_string(&trace.result_ids)?,
                now,
            ],
        )
        .await?;

        let mut rows = conn
            .query(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN fallback_reasons != '[]' THEN 1 ELSE 0 END), 0) FROM retrieval_traces",
                params![],
            )
            .await?;
        if let Some(row) = rows.next().await? {
            let total: i64 = row.get(0)?;
            let fallback: i64 = row.get(1)?;
            let alert_rate = Self::retrieval_setting(&conn, "fallback_alert_rate", "0.05")
                .await
                .parse::<f64>()
                .unwrap_or(0.05)
                .clamp(0.0, 1.0);
            if total >= 20 && fallback as f64 / total as f64 > alert_rate {
                warn!(
                    "retrieval fallback rate {:.1}% exceeds configured {:.1}% threshold",
                    fallback as f64 * 100.0 / total as f64,
                    alert_rate * 100.0
                );
            }
        }
        drop(rows);
        // Evaluation is bounded by the persisted weekly gate and becomes
        // active automatically once a local golden item has been harvested.
        let mut golden = conn
            .query("SELECT 1 FROM retrieval_golden_items LIMIT 1", params![])
            .await?;
        let has_golden_items = golden.next().await?.is_some();
        drop(golden);
        let evaluation_enabled = Self::retrieval_setting(&conn, "enabled", "true")
            .await
            .parse::<bool>()
            .unwrap_or(true);
        if has_golden_items && evaluation_enabled {
            let _ = self.run_retrieval_evaluation(100).await;
        }
        Ok(())
    }

    /// Return the most recent persisted retrieval traces for diagnostics and
    /// operator-facing audit tooling.
    pub async fn list_retrieval_traces(
        &self,
        limit: usize,
    ) -> Result<Vec<crate::utils::retrieval::RetrievalTrace>> {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let conn = self.get_conn()?;
        let mut rows = conn
            .query(
                "SELECT id, query_hash, namespace, rewritten_terms, keyword_candidates, vector_candidates, graph_candidates, effective_weights, fallback_reasons, result_ids FROM retrieval_traces ORDER BY created_at DESC LIMIT ?",
                params![limit.clamp(1, 1_000) as i64],
            )
            .await?;
        let mut traces = Vec::new();
        while let Some(row) = rows.next().await? {
            traces.push(crate::utils::retrieval::RetrievalTrace {
                id: row.get(0)?,
                query_hash: row.get(1)?,
                namespace: row.get(2)?,
                rewritten_terms: serde_json::from_str(&row.get::<String>(3)?)?,
                keyword_candidates: row.get::<i64>(4)?.max(0) as usize,
                vector_candidates: row.get::<i64>(5)?.max(0) as usize,
                graph_candidates: row.get::<i64>(6)?.max(0) as usize,
                effective_weights: serde_json::from_str(&row.get::<String>(7)?)?,
                fallback_reasons: serde_json::from_str(&row.get::<String>(8)?)?,
                result_ids: serde_json::from_str(&row.get::<String>(9)?)?,
            });
        }
        Ok(traces)
    }

    /// Read learned weights, retaining safe defaults when the table is absent
    /// (for callers opening a pre-migration database).
    pub async fn retrieval_weights(&self) -> crate::utils::retrieval::RetrievalWeights {
        let defaults = crate::utils::retrieval::RetrievalWeights::default();
        let Ok(conn) = self.get_conn() else {
            return defaults;
        };
        let Ok(mut rows) = conn
            .query(
                "SELECT weights FROM retrieval_adaptive_weights WHERE profile = 'default'",
                params![],
            )
            .await
        else {
            return defaults;
        };
        let Ok(Some(row)) = rows.next().await else {
            return defaults;
        };
        serde_json::from_str::<crate::utils::retrieval::RetrievalWeights>(
            &row.get::<String>(0).unwrap_or_default(),
        )
        .unwrap_or(defaults)
    }

    /// Record a user-use signal without storing the raw response or query.
    pub async fn record_retrieval_use(&self, memory_ids: &[MemoryId]) -> Result<()> {
        if memory_ids.is_empty() {
            return Ok(());
        }
        let conn = self.get_conn()?;
        let mut rows = conn.query(
            "SELECT id, query_hash, namespace, rewritten_terms, result_ids, used_result_ids FROM retrieval_traces ORDER BY created_at DESC LIMIT 1000",
            params![],
        ).await?;
        let wanted: std::collections::HashSet<String> =
            memory_ids.iter().map(ToString::to_string).collect();
        // `mnemosyne.used` has no trace ID in its historical wire contract.
        // Attribute the signal to the newest matching query only; labeling
        // every historical trace containing the same memory corrupts the
        // golden set and makes evaluation look better than reality.
        let mut update = None;
        while let Some(row) = rows.next().await? {
            let id: String = row.get(0)?;
            let query_hash: String = row.get(1)?;
            let namespace: Option<String> = row.get(2)?;
            let query_terms: Vec<String> =
                serde_json::from_str(&row.get::<String>(3)?).unwrap_or_default();
            let ids: Vec<String> = serde_json::from_str(&row.get::<String>(4)?).unwrap_or_default();
            let mut used: Vec<String> =
                serde_json::from_str(&row.get::<String>(5).unwrap_or_else(|_| "[]".into()))
                    .unwrap_or_default();
            for memory_id in ids.into_iter().filter(|id| wanted.contains(id)) {
                if !used.contains(&memory_id) {
                    used.push(memory_id);
                }
            }
            if !used.is_empty() {
                update = Some((id, query_hash, namespace, query_terms, used));
                break;
            }
        }
        if let Some((id, query_hash, namespace, query_terms, used)) = update {
            conn.execute(
                "UPDATE retrieval_traces SET used_result_ids = ? WHERE id = ?",
                params![serde_json::to_string(&used)?, id],
            )
            .await?;
            conn.execute(
                "INSERT OR REPLACE INTO retrieval_golden_items (id, query_hash, query_terms, relevant_memory_ids, namespace, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                params![Uuid::new_v4().to_string(), query_hash, serde_json::to_string(&query_terms)?, serde_json::to_string(&used)?, namespace.unwrap_or_default(), Utc::now().timestamp()],
            ).await?;
        }
        Ok(())
    }

    /// Add or replace a privacy-conscious golden-set item.
    pub async fn upsert_retrieval_golden_item(
        &self,
        item: &crate::utils::retrieval::RetrievalGoldenItem,
    ) -> Result<()> {
        let conn = self.get_conn()?;
        let query_term_fingerprints = item
            .query_terms
            .iter()
            .map(|term| term_fingerprint(term))
            .collect::<Vec<_>>();
        conn.execute(
            "INSERT OR REPLACE INTO retrieval_golden_items (id, query_hash, query_terms, relevant_memory_ids, namespace, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            params![
                item.id.clone(), item.query_hash.clone(),
                serde_json::to_string(&query_term_fingerprints)?,
                serde_json::to_string(&item.relevant_memory_ids)?,
                item.namespace.clone().unwrap_or_default(), Utc::now().timestamp()
            ],
        ).await?;
        Ok(())
    }

    /// Harvest a user-confirmed query/result pair without persisting query text.
    pub async fn harvest_retrieval_golden_item(
        &self,
        query: &str,
        relevant_memory_ids: &[MemoryId],
        namespace: Option<Namespace>,
    ) -> Result<()> {
        let trace = crate::utils::retrieval::RetrievalTrace::for_query(
            query,
            crate::utils::retrieval::RetrievalWeights::default(),
        );
        self.upsert_retrieval_golden_item(&crate::utils::retrieval::RetrievalGoldenItem {
            id: Uuid::new_v4().to_string(),
            query_hash: trace.query_hash,
            query_terms: trace.rewritten_terms,
            relevant_memory_ids: relevant_memory_ids
                .iter()
                .map(ToString::to_string)
                .collect(),
            namespace: namespace.map(|value| value.to_string()),
        })
        .await
    }

    /// Evaluate at most `max_samples` golden items and adapt only after a
    /// minimum sample count. A recent run is reused to enforce weekly bounds.
    pub async fn run_retrieval_evaluation(
        &self,
        max_samples: usize,
    ) -> Result<crate::utils::retrieval::RetrievalEvaluationReport> {
        let conn = self.get_conn()?;
        let evaluation_enabled = Self::retrieval_setting(&conn, "enabled", "true")
            .await
            .parse::<bool>()
            .unwrap_or(true);
        if !evaluation_enabled {
            return Ok(crate::utils::retrieval::RetrievalEvaluationReport {
                sample_count: 0,
                precision_at_5: 0.0,
                phrasing_miss_rate: 0.0,
            });
        }
        let configured_interval =
            Self::retrieval_setting(&conn, "weekly_interval_seconds", "604800")
                .await
                .parse::<i64>()
                .unwrap_or(604800)
                .max(1);
        let configured_max_samples = Self::retrieval_setting(&conn, "max_samples", "100")
            .await
            .parse::<usize>()
            .unwrap_or(100)
            .clamp(1, 1000);
        let max_samples = max_samples.clamp(1, configured_max_samples);
        let now = Utc::now().timestamp();
        let mut recent = conn.query(
            "SELECT sample_count, precision_at_5, phrasing_miss_rate FROM retrieval_evaluation_runs WHERE created_at > ? ORDER BY created_at DESC LIMIT 1",
            params![now - configured_interval],
        ).await?;
        if let Some(row) = recent.next().await? {
            return Ok(crate::utils::retrieval::RetrievalEvaluationReport {
                sample_count: row.get::<i64>(0)? as usize,
                precision_at_5: row.get::<f64>(1)? as f32,
                phrasing_miss_rate: row.get::<f64>(2)? as f32,
            });
        }
        let mut golden = conn.query(
            "SELECT query_hash, relevant_memory_ids, COALESCE(namespace, '') FROM retrieval_golden_items ORDER BY created_at DESC LIMIT ?",
            params![max_samples as i64],
        ).await?;
        let mut samples = Vec::new();
        while let Some(row) = golden.next().await? {
            let query_hash: String = row.get(0)?;
            let relevant: Vec<String> =
                serde_json::from_str(&row.get::<String>(1)?).unwrap_or_default();
            let namespace: String = row.get(2)?;
            let mut traces = conn.query("SELECT keyword_candidates, result_ids FROM retrieval_traces WHERE query_hash = ? AND COALESCE(namespace, '') = ? ORDER BY created_at DESC LIMIT 1", params![query_hash, namespace]).await?;
            let (keyword_candidates, result_ids): (usize, Vec<String>) = match traces.next().await?
            {
                Some(trace) => (
                    trace.get::<i64>(0)?.max(0) as usize,
                    serde_json::from_str(&trace.get::<String>(1)?).unwrap_or_default(),
                ),
                None => continue,
            };
            samples.push((relevant, result_ids, keyword_candidates));
        }
        let sample_count = samples.len();
        let (precision_at_5, phrasing_miss_rate) = if sample_count == 0 {
            (0.0, 0.0)
        } else {
            let mut hits = 0usize;
            let mut miss = 0usize;
            for (relevant, results, keyword_candidates) in &samples {
                let relevant: std::collections::HashSet<_> = relevant.iter().collect();
                let top = results.iter().take(5);
                let count = top.filter(|id| relevant.contains(id)).count();
                hits += count;
                if *keyword_candidates == 0 && count == 0 {
                    miss += 1;
                }
            }
            (
                hits as f32 / (sample_count * 5) as f32,
                miss as f32 / sample_count as f32,
            )
        };
        let report = crate::utils::retrieval::RetrievalEvaluationReport {
            sample_count,
            precision_at_5,
            phrasing_miss_rate,
        };
        conn.execute("INSERT INTO retrieval_evaluation_runs (id, sample_count, precision_at_5, phrasing_miss_rate, created_at) VALUES (?, ?, ?, ?, ?)", params![Uuid::new_v4().to_string(), sample_count as i64, precision_at_5, phrasing_miss_rate, now]).await?;
        let min_samples = Self::retrieval_setting(&conn, "min_samples_for_adaptation", "20")
            .await
            .parse::<usize>()
            .unwrap_or(20)
            .clamp(1, 10_000);
        if sample_count >= min_samples {
            let current = self.retrieval_weights().await;
            let delta = (0.5 - precision_at_5).clamp(-0.05, 0.05);
            let mut updated = crate::utils::retrieval::RetrievalWeights {
                keyword: current.keyword + delta,
                vector: current.vector - delta,
                graph: current.graph,
            };
            updated.keyword = updated.keyword.clamp(0.20, 0.60);
            updated.vector = updated.vector.clamp(0.15, 0.50);
            updated.graph = updated.graph.clamp(0.05, 0.30);
            let sum = updated.keyword + updated.vector + updated.graph;
            updated.keyword /= sum;
            updated.vector /= sum;
            updated.graph /= sum;
            conn.execute("INSERT OR REPLACE INTO retrieval_adaptive_weights (profile, weights, sample_count, last_evaluated_at) VALUES ('default', ?, ?, ?)", params![serde_json::to_string(&updated)?, sample_count as i64, now]).await?;
        }
        Ok(report)
    }

    /// Fetch many memories in two round trips (one for rows, one for all
    /// associated links) instead of two queries per id.
    ///
    /// Hybrid recall used to call [`Self::get_memory`] once per candidate,
    /// which issued two SQL queries per candidate per recall and left most of
    /// each query's time waiting on storage round trips. Missing ids are
    /// simply absent from the returned map so callers can keep their
    /// skip-and-warn behavior.
    async fn get_memories_batch(&self, ids: &[MemoryId]) -> Result<HashMap<MemoryId, MemoryNote>> {
        let mut memories: HashMap<MemoryId, MemoryNote> = HashMap::with_capacity(ids.len());
        if ids.is_empty() {
            return Ok(memories);
        }

        let conn = self.get_conn()?;
        let placeholders = ids.iter().map(|_| "?").collect::<Vec<_>>().join(", ");
        let id_params: Vec<libsql::Value> = ids
            .iter()
            .map(|id| libsql::Value::Text(id.to_string()))
            .collect();

        let sql = format!(
            "SELECT {columns} FROM memories m WHERE m.id IN ({placeholders})",
            columns = self.memory_columns("m"),
            placeholders = placeholders
        );
        let mut rows = conn
            .query(&sql, libsql::params_from_iter(id_params.clone()))
            .await?;
        while let Some(row) = rows.next().await? {
            let memory = self.row_to_memory(&row).await?;
            memories.insert(memory.id, memory);
        }
        drop(rows);

        // Hydrate companion vectors for StandardSQLite so batch recall has the
        // same public projection as get_memory.
        if self.schema_type == SchemaType::StandardSQLite {
            let embedding_sql = format!(
                "SELECT memory_id, embedding FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                placeholders = placeholders
            );
            let mut embedding_rows = conn
                .query(&embedding_sql, libsql::params_from_iter(id_params.clone()))
                .await?;
            while let Some(embedding_row) = embedding_rows.next().await? {
                let memory_id = MemoryId::from_string(&embedding_row.get::<String>(0)?)?;
                if let Some(memory) = memories.get_mut(&memory_id) {
                    memory.embedding = Some(decode_embedding_from_row(&embedding_row, 1)?);
                }
            }
        }

        // Attach semantic links in one additional grouped query.
        let has_link_metadata = connection_has_column(&conn, "memory_links", "last_traversed_at")
            .await?
            && connection_has_column(&conn, "memory_links", "user_created").await?;
        let link_sql = if has_link_metadata {
            format!(
                "SELECT source_id, target_id, link_type, strength, reason, created_at, last_traversed_at, user_created FROM memory_links WHERE source_id IN ({placeholders})",
                placeholders = placeholders
            )
        } else {
            format!(
                "SELECT source_id, target_id, link_type, strength, reason, created_at FROM memory_links WHERE source_id IN ({placeholders})",
                placeholders = placeholders
            )
        };
        let mut link_rows = conn
            .query(&link_sql, libsql::params_from_iter(id_params))
            .await?;
        while let Some(link_row) = link_rows.next().await? {
            let source_id_str: String = link_row.get(0)?;
            let source_id = match MemoryId::from_string(&source_id_str) {
                Ok(id) => id,
                Err(e) => {
                    warn!("Invalid source_id in memory_links: {}", e);
                    continue;
                }
            };
            let Some(memory) = memories.get_mut(&source_id) else {
                continue;
            };
            let target_id_str: String = link_row.get(1)?;
            let target_id = match MemoryId::from_string(&target_id_str) {
                Ok(id) => id,
                Err(e) => {
                    warn!("Invalid target_id in memory_links: {}", e);
                    continue;
                }
            };
            let Some(link_type) = Self::parse_link_type(link_row.get::<String>(2)?.as_str()) else {
                continue;
            };
            let strength: f64 = link_row.get(3)?;
            let reason: String = link_row.get(4)?;
            let created_at = parse_datetime_from_row(&link_row, 5).ok_or_else(|| {
                MnemosyneError::Other("Invalid memory-link creation timestamp".into())
            })?;
            let last_traversed_at = if has_link_metadata {
                parse_datetime_from_row(&link_row, 6)
            } else {
                None
            };
            let user_created = if has_link_metadata {
                link_row.get::<i64>(7).unwrap_or(0) != 0
            } else {
                false
            };

            memory.links.push(crate::types::MemoryLink {
                target_id,
                link_type,
                strength: strength as f32,
                reason,
                created_at,
                last_traversed_at,
                user_created,
            });
        }

        debug!("Batch-fetched {} memories", memories.len());
        Ok(memories)
    }

    /// Map a stored link-type string to its typed representation.
    fn parse_link_type(value: &str) -> Option<crate::types::LinkType> {
        match value {
            "extends" => Some(crate::types::LinkType::Extends),
            "contradicts" => Some(crate::types::LinkType::Contradicts),
            "implements" => Some(crate::types::LinkType::Implements),
            "references" => Some(crate::types::LinkType::References),
            "supersedes" => Some(crate::types::LinkType::Supersedes),
            _ => None,
        }
    }

    /// Store version check cache entry
    pub async fn store_version_cache(
        &self,
        cache: &crate::version_check::VersionCheckCache,
    ) -> Result<()> {
        let conn = self.get_conn()?;
        let tool_str = serde_json::to_string(&cache.tool)?;

        conn.execute(
            "INSERT OR REPLACE INTO version_check_cache (tool, latest_version, release_url, checked_at, last_notified_version)
             VALUES (?, ?, ?, ?, NULL)",
            params![tool_str, cache.latest_version.clone(), cache.release_url.clone(), cache.checked_at as i64],
        )
        .await
        .map_err(|e| MnemosyneError::Database(format!("Failed to store version cache: {}", e)))?;

        Ok(())
    }

    /// Get version check cache entry for a tool
    pub async fn get_version_cache(
        &self,
        tool: crate::version_check::Tool,
    ) -> Result<Option<crate::version_check::VersionCheckCache>> {
        let conn = self.get_conn()?;
        let tool_str = serde_json::to_string(&tool)?;

        let mut rows = conn
            .query(
                "SELECT tool, latest_version, release_url, checked_at FROM version_check_cache WHERE tool = ?",
                params![tool_str],
            )
            .await
            .map_err(|e| MnemosyneError::Database(format!("Failed to query version cache: {}", e)))?;

        if let Some(row) = rows.next().await.map_err(|e| {
            MnemosyneError::Database(format!("Failed to read version cache row: {}", e))
        })? {
            let tool_json: String = row.get(0).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get tool from cache: {}", e))
            })?;
            let tool: crate::version_check::Tool = serde_json::from_str(&tool_json)?;
            let latest_version: String = row.get(1).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get latest_version from cache: {}", e))
            })?;
            let release_url: String = row.get(2).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get release_url from cache: {}", e))
            })?;
            let checked_at: i64 = row.get(3).map_err(|e| {
                MnemosyneError::Database(format!("Failed to get checked_at from cache: {}", e))
            })?;

            Ok(Some(crate::version_check::VersionCheckCache {
                tool,
                latest_version,
                release_url,
                checked_at: checked_at as u64,
            }))
        } else {
            Ok(None)
        }
    }

    /// Clear stale version check cache entries
    pub async fn clear_stale_version_cache(&self, max_age_hours: u64) -> Result<()> {
        let conn = self.get_conn()?;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let cutoff = now - (max_age_hours * 3600);

        conn.execute(
            "DELETE FROM version_check_cache WHERE checked_at < ?",
            params![cutoff as i64],
        )
        .await
        .map_err(|e| {
            MnemosyneError::Database(format!("Failed to clear stale version cache: {}", e))
        })?;

        Ok(())
    }
}
