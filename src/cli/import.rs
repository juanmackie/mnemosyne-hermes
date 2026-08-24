//! Import memories from the Python `mnemosyne-memory` SQLite store.
//!
//! The importer intentionally reads the source through SELECT/PRAGMA queries
//! only. Python provider versions add tables and columns over time, so the
//! mapping is presence-based instead of assuming one exact schema revision.

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use libsql::{Builder, Connection, Value as SqlValue};
use mnemosyne_core::{
    error::{MnemosyneError, Result},
    MemoryId, MemoryNote, MemoryType, Namespace, StorageBackend,
};
use serde::Serialize;
use serde_json::Value;
use std::collections::HashSet;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use tracing::{debug, info};
use uuid::Uuid;

const SOURCE_TABLES: &[&str] = &[
    "working_memory",
    "episodic_memory",
    "memories",
    "canonical_facts",
    "triples",
    "facts",
    "annotations",
];

#[derive(Debug, Default, Serialize)]
struct ImportReport {
    source: String,
    dry_run: bool,
    tables: Vec<String>,
    scanned: usize,
    imported: usize,
    skipped: usize,
    errors: Vec<String>,
}

#[derive(Debug)]
struct SourceMemory {
    source_table: String,
    source_id: String,
    content: String,
    source: String,
    memory_type: MemoryType,
    importance: u8,
    created_at: DateTime<Utc>,
    metadata: Value,
    tags: Vec<String>,
}

/// Import a Python mnemosyne-memory SQLite database into the Rust store.
///
/// The source is never written. Re-running the command is safe because target
/// IDs are deterministic for `(source path, table, source id)` and existing IDs
/// are skipped. `--dry-run` scans and reports rows without opening the target.
pub async fn handle(
    source_path: String,
    namespace: Option<String>,
    dry_run: bool,
    format: String,
    target_db_path: Option<String>,
) -> Result<()> {
    let source = canonical_source_path(&source_path)?;
    let source_db = Builder::new_local(source.to_string_lossy().to_string())
        .build()
        .await
        .map_err(|e| MnemosyneError::Database(format!("Failed to open source database: {}", e)))?;
    let source_conn = source_db
        .connect()
        .map_err(|e| MnemosyneError::Database(format!("Failed to connect to source database: {}", e)))?;

    let target_namespace = parse_namespace(namespace.as_deref().unwrap_or("agent:hermes"));
    let mut report = ImportReport {
        source: source.display().to_string(),
        dry_run,
        ..Default::default()
    };

    let mut target = None;
    if !dry_run {
        let target_path = target_db_path
            .map(PathBuf::from)
            .unwrap_or_else(default_target_path);
        let target_canonical = target_path
            .canonicalize()
            .unwrap_or_else(|_| target_path.clone());
        if target_canonical == source {
            return Err(MnemosyneError::ValidationError(
                "Source and target database are the same file; choose a separate target.".to_string(),
            ));
        }
        let storage = mnemosyne_core::LibsqlStorage::new_with_validation(
            mnemosyne_core::ConnectionMode::Local(target_path.to_string_lossy().to_string()),
            true,
        )
        .await?;
        target = Some(storage);
    }

    let mut seen = HashSet::new();
    for table in SOURCE_TABLES {
        if !table_exists(&source_conn, table).await? {
            continue;
        }
        report.tables.push((*table).to_string());
        let columns = table_columns(&source_conn, table).await?;
        let rows = read_table(&source_conn, table, &columns).await?;
        for row in rows {
            report.scanned += 1;
            let converted = convert_row(table, &columns, &row, &source, &target_namespace);
            let Some(memory) = converted else {
                debug!("Skipping source row from {}: columns={:?}, row={:?}", table, columns, row);
                report.errors.push(format!(
                    "Skipped {} row {}: no supported id/content fields",
                    table, report.scanned
                ));
                report.skipped += 1;
                continue;
            };

            // Some provider versions retain the same item in both a legacy
            // table and a tiered table. Avoid importing that exact duplicate
            // while retaining rows that differ by timestamp or source.
            let dedupe_key = format!(
                "{}\0{}\0{}\0{}",
                memory.content, memory.source, memory.created_at, memory.source_id
            );
            if !seen.insert(dedupe_key) {
                report.skipped += 1;
                continue;
            }

            let id = deterministic_id(&source, &memory.source_table, &memory.source_id);
            let note = to_memory_note(id, memory, target_namespace.clone());
            if let Some(storage) = target.as_ref() {
                match storage.get_memory(id).await {
                    Ok(_) => {
                        report.skipped += 1;
                        continue;
                    }
                    Err(MnemosyneError::MemoryNotFound(_)) => {}
                    Err(error) => return Err(error),
                }
                storage.store_memory(&note).await?;
            }
            report.imported += 1;
        }
    }

    emit_report(&report, &format);
    info!(
        "Imported {} of {} source rows from {}",
        report.imported, report.scanned, report.source
    );
    Ok(())
}

fn canonical_source_path(path: &str) -> Result<PathBuf> {
    let source = PathBuf::from(path);
    if !source.is_file() {
        return Err(MnemosyneError::ValidationError(format!(
            "Source database does not exist or is not a regular file: {}",
            source.display()
        )));
    }
    source.canonicalize().map_err(MnemosyneError::from)
}

fn default_target_path() -> PathBuf {
    dirs::data_local_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("mnemosyne")
        .join("mnemosyne.db")
}

fn parse_namespace(value: &str) -> Namespace {
    if let Some(name) = value.strip_prefix("project:") {
        Namespace::Project {
            name: name.to_string(),
        }
    } else if let Some(agent_id) = value.strip_prefix("agent:") {
        Namespace::Agent {
            agent_id: agent_id.to_string(),
        }
    } else if let Some(rest) = value.strip_prefix("session:") {
        let mut parts = rest.splitn(2, ':');
        match (parts.next(), parts.next()) {
            (Some(project), Some(session_id)) => Namespace::Session {
                project: project.to_string(),
                session_id: session_id.to_string(),
            },
            _ => Namespace::Global,
        }
    } else {
        Namespace::Global
    }
}

async fn table_exists(conn: &Connection, table: &str) -> Result<bool> {
    let mut rows = conn
        .query(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            [table],
        )
        .await?;
    Ok(rows.next().await?.is_some())
}

async fn table_columns(conn: &Connection, table: &str) -> Result<Vec<String>> {
    let sql = format!("PRAGMA table_info(\"{}\")", table.replace('"', "\"\""));
    let mut rows = conn.query(&sql, ()).await?;
    let mut columns = Vec::new();
    while let Some(row) = rows.next().await? {
        columns.push(row.get::<String>(1)?);
    }
    Ok(columns)
}

async fn read_table(conn: &Connection, table: &str, columns: &[String]) -> Result<Vec<Vec<SqlValue>>> {
    let quoted = format!("\"{}\"", table.replace('"', "\"\""));
    let sql = format!("SELECT * FROM {}", quoted);
    let mut rows = conn.query(&sql, ()).await?;
    let mut result = Vec::new();
    while let Some(row) = rows.next().await? {
        // Materialize values while the cursor is alive. Retaining Row handles
        // after advancing the cursor produces an all-NULL view in libsql.
        let mut values = Vec::with_capacity(row.column_count() as usize);
        for index in 0..row.column_count() {
            values.push(row.get_value(index)?);
        }
        result.push(values);
    }
    let _ = columns;
    Ok(result)
}

fn column_index(columns: &[String], names: &[&str]) -> Option<usize> {
    names.iter().find_map(|name| {
        columns
            .iter()
            .position(|column| column.eq_ignore_ascii_case(name))
    })
}

fn text_at(row: &[SqlValue], columns: &[String], names: &[&str]) -> Option<String> {
    let index = column_index(columns, names)?;
    match row.get(index)? {
        SqlValue::Text(value) => Some(value.clone()),
        SqlValue::Integer(value) => Some(value.to_string()),
        SqlValue::Real(value) => Some(value.to_string()),
        SqlValue::Null | SqlValue::Blob(_) => None,
    }
}

fn float_at(row: &[SqlValue], columns: &[String], names: &[&str]) -> Option<f64> {
    let index = column_index(columns, names)?;
    match row.get(index)? {
        SqlValue::Real(value) => Some(*value),
        SqlValue::Integer(value) => Some(*value as f64),
        SqlValue::Text(value) => value.parse::<f64>().ok(),
        SqlValue::Null | SqlValue::Blob(_) => None,
    }
}

fn parse_timestamp(value: Option<String>) -> DateTime<Utc> {
    let Some(value) = value else {
        return Utc::now();
    };
    if let Ok(timestamp) = DateTime::parse_from_rfc3339(&value) {
        return timestamp.with_timezone(&Utc);
    }
    for format in ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d %H:%M:%S"] {
        if let Ok(timestamp) = NaiveDateTime::parse_from_str(&value, format) {
            return DateTime::from_naive_utc_and_offset(timestamp, Utc);
        }
    }
    if let Ok(date) = NaiveDate::parse_from_str(&value, "%Y-%m-%d") {
        return DateTime::from_naive_utc_and_offset(
            date.and_hms_opt(0, 0, 0).unwrap_or_default(),
            Utc,
        );
    }
    Utc::now()
}

fn parse_metadata(value: Option<String>) -> Value {
    value
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_else(|| Value::Object(serde_json::Map::new()))
}

fn convert_row(
    table: &str,
    columns: &[String],
    row: &[SqlValue],
    source: &Path,
    _namespace: &Namespace,
) -> Option<SourceMemory> {
    let source_id = text_at(row, columns, &["id", "memory_id", "fact_id", "rowid"])?;
    let source_name = text_at(row, columns, &["source", "author_id", "origin"])
        .unwrap_or_else(|| table.to_string());
    let created_at = parse_timestamp(text_at(
        row,
        columns,
        &["created_at", "timestamp", "valid_from"],
    ));
    let metadata = parse_metadata(text_at(row, columns, &["metadata_json", "metadata"]));

    let (content, memory_type, extra_tags) = match table {
        "triples" | "facts" => {
            let subject = text_at(row, columns, &["subject"])?;
            let predicate = text_at(row, columns, &["predicate"])?;
            let object = text_at(row, columns, &["object"])?;
            (
                format!("{} {} {}", subject, predicate, object),
                MemoryType::Entity,
                vec!["triple".to_string(), format!("predicate:{}", predicate)],
            )
        }
        "canonical_facts" => (
            text_at(row, columns, &["body", "content", "value"])? ,
            MemoryType::Preference,
            vec!["canonical".to_string(), "persona".to_string()],
        ),
        "annotations" => (
            text_at(row, columns, &["value", "content", "body"])? ,
            MemoryType::Insight,
            vec!["annotation".to_string()],
        ),
        _ => {
            let content = text_at(row, columns, &["content", "body", "text"])?;
            let kind = text_at(row, columns, &["memory_type", "type", "category"])
                .unwrap_or_default();
            (content, memory_type_from_source(&kind), Vec::new())
        }
    };

    if content.trim().is_empty() {
        return None;
    }

    let importance = float_at(row, columns, &["importance", "confidence"])
        .unwrap_or(0.5)
        .clamp(0.0, 1.0);
    let importance = if importance <= 1.0 {
        (importance * 10.0).round() as u8
    } else {
        importance.round() as u8
    }
    .clamp(1, 10);

    let mut tags = vec![
        "imported".to_string(),
        format!("source-table:{}", table),
    ];
    tags.extend(extra_tags);
    if let Some(category) = text_at(row, columns, &["category", "kind", "memory_type"]) {
        if !category.trim().is_empty() {
            tags.push(format!("source-type:{}", category.trim().to_lowercase()));
        }
    }

    let mut metadata = metadata;
    if let Value::Object(ref mut object) = metadata {
        object.insert(
            "source_database".to_string(),
            Value::String(source.display().to_string()),
        );
        object.insert(
            "source_table".to_string(),
            Value::String(table.to_string()),
        );
        if let Some(valid_until) = text_at(row, columns, &["valid_until"]) {
            object.insert("valid_until".to_string(), Value::String(valid_until));
        }
        if let Some(session_id) = text_at(row, columns, &["session_id"]) {
            object.insert("session_id".to_string(), Value::String(session_id));
        }
    }

    Some(SourceMemory {
        source_table: table.to_string(),
        source_id,
        content,
        source: source_name,
        memory_type,
        importance,
        created_at,
        metadata,
        tags,
    })
}

fn memory_type_from_source(value: &str) -> MemoryType {
    match value.to_lowercase().as_str() {
        "preference" | "pref" => MemoryType::Preference,
        "instruction" | "constraint" | "rule" => MemoryType::Constraint,
        "decision" | "architecture_decision" => MemoryType::ArchitectureDecision,
        "task" => MemoryType::Task,
        "reference" => MemoryType::Reference,
        "bug_fix" | "bug" => MemoryType::BugFix,
        "configuration" | "config" => MemoryType::Configuration,
        "entity" | "fact" => MemoryType::Entity,
        _ => MemoryType::Insight,
    }
}

fn deterministic_id(source: &Path, table: &str, source_id: &str) -> MemoryId {
    let key = format!("{}\0{}\0{}", source.display(), table, source_id);
    let digest = Sha256::digest(key.as_bytes());
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    // Mark the truncated digest as a UUID-shaped, deterministic identifier.
    // UUIDv5 is not enabled because this repository's existing lockfile has a
    // deliberately broad dependency graph; the collision-resistant namespace
    // key above provides the same rerun/idempotence property here.
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    MemoryId(Uuid::from_bytes(bytes))
}

fn to_memory_note(id: MemoryId, memory: SourceMemory, namespace: Namespace) -> MemoryNote {
    let summary = memory
        .content
        .split_once(". ")
        .map(|(first, _)| format!("{}.", first))
        .unwrap_or_else(|| memory.content.chars().take(160).collect());
    let keywords = memory
        .content
        .split(|character: char| !character.is_alphanumeric())
        .filter(|word| word.len() >= 4)
        .map(|word| word.to_lowercase())
        .take(12)
        .collect();
    let context = serde_json::json!({
        "imported_from": memory.source,
        "source_table": memory.source_table,
        "source_id": memory.source_id,
        "metadata": memory.metadata,
    })
    .to_string();
    let now = Utc::now();

    MemoryNote {
        id,
        namespace,
        created_at: memory.created_at,
        updated_at: memory.created_at,
        content: memory.content,
        summary,
        keywords,
        tags: memory.tags,
        context,
        memory_type: memory.memory_type,
        importance: memory.importance,
        confidence: (memory.importance as f32 / 10.0).clamp(0.1, 1.0),
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
    }
}

fn emit_report(report: &ImportReport, format: &str) {
    if format.eq_ignore_ascii_case("json") {
        println!(
            "{}",
            serde_json::to_string_pretty(report).unwrap_or_else(|_| "{}".to_string())
        );
    } else {
        println!(
            "Imported {} of {} rows ({} skipped) from {}{}",
            report.imported,
            report.scanned,
            report.skipped,
            report.source,
            if report.dry_run { " [dry run]" } else { "" }
        );
        if !report.tables.is_empty() {
            println!("Tables: {}", report.tables.join(", "));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_ids_are_stable() {
        let path = Path::new("/tmp/source.db");
        assert_eq!(
            deterministic_id(path, "working_memory", "abc"),
            deterministic_id(path, "working_memory", "abc")
        );
        assert_ne!(
            deterministic_id(path, "working_memory", "abc"),
            deterministic_id(path, "episodic_memory", "abc")
        );
    }

    #[test]
    fn parses_provider_namespaces() {
        assert_eq!(parse_namespace("global"), Namespace::Global);
        assert_eq!(
            parse_namespace("agent:hermes"),
            Namespace::Agent {
                agent_id: "hermes".to_string()
            }
        );
        assert_eq!(
            parse_namespace("session:home:one"),
            Namespace::Session {
                project: "home".to_string(),
                session_id: "one".to_string()
            }
        );
    }
}
