//! Project-context bootstrap command.

use mnemosyne_core::{build_bootstrap, BootstrapRequest, ConnectionMode, LibsqlStorage};
use std::path::PathBuf;

use super::helpers::{get_db_path, parse_namespace};

/// Build and print a bounded, structured startup package.
pub async fn handle(
    project: Option<PathBuf>,
    namespace: Option<String>,
    task: String,
    agent: Option<String>,
    capability: Option<String>,
    budget_tokens: usize,
    min_confidence: f32,
    format: String,
    global_db_path: Option<String>,
) -> mnemosyne_core::error::Result<()> {
    if format != "json" {
        return Err(mnemosyne_core::error::MnemosyneError::ValidationError(
            "bootstrap currently supports only --format json".into(),
        ));
    }
    let db_path = get_db_path(global_db_path);
    let storage = LibsqlStorage::new(ConnectionMode::Local(db_path)).await?;
    let namespace = namespace.map(|value| parse_namespace(&value)).transpose()?;
    let response = build_bootstrap(
        &storage,
        BootstrapRequest {
            project,
            namespace,
            task,
            agent,
            capability,
            budget_tokens,
            min_confidence,
        },
    )
    .await?;
    println!("{}", serde_json::to_string_pretty(&response)?);
    Ok(())
}
