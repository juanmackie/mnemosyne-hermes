//! Agent prefetch command — fire-and-forget context warmup
//!
//! Used by agent runtimes after a turn completes to kick off recall for the
//! *next* turn (mirrors `MemoryManager::prefetch_all` in hermes-agent).
//! When run from the CLI this blocks briefly and prints the prefetched
//! context to stdout so the runtime can enqueue it.

use mnemosyne_core::{error::Result, MemoryConfig, MemoryManager};
use std::path::PathBuf;

use super::helpers::{get_db_path, parse_namespace};

/// Warm up recall context for the given query and print to stdout.
pub async fn handle(
    query: String,
    namespace: Option<String>,
    limit: usize,
    global_db_path: Option<String>,
) -> Result<()> {
    let db_path = get_db_path(global_db_path);
    let mgr = MemoryManager::new_with_path("_prefetch", Some(PathBuf::from(db_path))).await?;
    let config = namespace
        .as_deref()
        .map(parse_namespace)
        .transpose()?
        .map(|ns| MemoryConfig::new().namespace(ns).max_results(limit))
        .unwrap_or_else(|| MemoryConfig::new().max_results(limit));

    let text = mgr.prefetch_with_config(&query, config).await;
    if !text.is_empty() {
        println!("{text}");
    }
    Ok(())
}
