//! Agent sync command — records a completed user+assistant turn as memory
//!
//! Mirrors `MemoryManager::sync_all` in hermes-agent.  Stores the exchange
//! as a session-scoped memory so the agent can recall it on future turns.

use mnemosyne_core::{error::Result, MemoryConfig, MemoryManager, MemoryType};
use std::path::PathBuf;

use super::helpers::{get_db_path, parse_namespace};

/// Store a completed exchange in the agent's memory.
///
/// Parameters
/// - `user_text`: the clean user message (no nudge injection)
/// - `assistant_text`: the final assistant response
/// - `namespace`: namespace override (default: session:default)
/// - `memory_type`: optional memory type tag
/// - `global_db_path`: optional explicit database path
pub async fn handle(
    user_text: String,
    assistant_text: String,
    namespace: String,
    memory_type: Option<String>,
    learn: bool,
    global_db_path: Option<String>,
) -> Result<()> {
    let db_path = get_db_path(global_db_path);
    let mgr = MemoryManager::new_with_path("_sync", Some(PathBuf::from(db_path))).await?;

    let ns = parse_namespace(&namespace)?;
    let memory_type = memory_type
        .as_deref()
        .and_then(|value| serde_json::from_value(serde_json::json!(value)).ok());
    let config = MemoryConfig::new()
        .namespace(ns)
        .memory_type(memory_type.unwrap_or(MemoryType::Insight));
    if learn {
        let result = mgr
            .sync_and_learn_with_config(&user_text, &assistant_text, config)
            .await?;
        println!("{}", serde_json::to_string(&result)?);
    } else {
        let id = mgr
            .sync_with_config(&user_text, &assistant_text, config)
            .await?;
        println!("synced: {}", id);
    }
    Ok(())
}
