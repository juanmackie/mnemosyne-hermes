//! `mnemosyne consolidate` — journaled exact-duplicate dedup.
//!
//! Dry run by default (it is the apply path rolled back, so counts are
//! exact). `--apply` first requires the WAL to checkpoint cleanly, copies
//! the database aside, then runs the single-transaction supersede.

use chrono::Utc;
use mnemosyne_core::{
    error::{MnemosyneError, Result},
    ConnectionMode, LibsqlStorage,
};

use super::helpers::get_db_path;

pub async fn handle(
    apply: bool,
    json: bool,
    limit: Option<usize>,
    global_db_path: Option<String>,
) -> Result<()> {
    let db_path = get_db_path(global_db_path);
    let storage =
        LibsqlStorage::new_with_validation(ConnectionMode::Local(db_path.clone()), true).await?;

    if apply {
        // Refuse to copy a database whose WAL still holds uncheckpointed
        // writes: the backup would miss committed data.
        if !storage.checkpoint_wal().await? {
            return Err(MnemosyneError::Database(
                "refusing --apply: WAL checkpoint busy (another connection holds reads/writes); retry once it drains".to_string(),
            ));
        }
        let backup = format!(
            "{}.pre-consolidate-{}",
            db_path,
            Utc::now().format("%Y%m%dT%H%M%S")
        );
        std::fs::copy(&db_path, &backup).map_err(|e| {
            MnemosyneError::Database(format!("failed to copy {} to {}: {}", db_path, backup, e))
        })?;
        eprintln!("database copied to {}", backup);
    }

    let report = storage.consolidate_exact_duplicates(!apply, limit).await?;

    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&report)
                .map_err(|e| MnemosyneError::Database(e.to_string()))?
        );
        return Ok(());
    }
    println!(
        "consolidate {} — run {}",
        if apply {
            "applied"
        } else {
            "dry run (pass --apply to mutate)"
        },
        report.run_id
    );
    println!("  duplicate groups:      {}", report.groups_found);
    println!("  duplicates superseded: {}", report.duplicates_superseded);
    println!("  edges repointed:       {}", report.edges_repointed);
    println!("  edges tombstoned:      {}", report.edges_tombstoned);
    println!("  vectors tombstoned:    {}", report.vectors_tombstoned);
    println!("  lineage repointed:     {}", report.lineage_repointed);
    println!("  orphan links staged:   {}", report.orphan_links_staged);
    println!(
        "  active memories:       {} -> {}",
        report.active_before, report.active_after
    );
    for keeper in &report.keepers {
        println!("  keeper: {}", keeper);
    }
    Ok(())
}
