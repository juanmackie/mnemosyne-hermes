//! `mnemosyne consolidate` — journaled exact-duplicate dedup, plus an optional
//! expired-row sweep.
//!
//! Dry run by default (it is the apply path rolled back, so counts are
//! exact). `--apply` first requires the WAL to checkpoint cleanly, copies
//! the database aside, then runs the single-transaction supersede.
//! `--forget-expired` additionally archives rows whose `expires_at` has passed;
//! it is opt-in because archiving rows is a visible change to what a store
//! contains, and it journals each archive so the sweep can be undone.

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
    forget_expired: bool,
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
    // Shares the consolidation run id so the archives are attributable to this run.
    let expired_archived = if forget_expired {
        storage
            .archive_expired(&report.run_id, !apply, limit)
            .await?
    } else {
        0
    };

    if json {
        let mut payload =
            serde_json::to_value(&report).map_err(|e| MnemosyneError::Database(e.to_string()))?;
        payload["expired_archived"] = serde_json::json!(expired_archived);
        payload["forget_expired"] = serde_json::json!(forget_expired);
        println!(
            "{}",
            serde_json::to_string_pretty(&payload)
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
    if forget_expired {
        println!(
            "  expired archived:      {}{}",
            expired_archived,
            if apply { "" } else { " (would archive)" }
        );
    }
    println!(
        "  active memories:       {} -> {}",
        report.active_before,
        report.active_after.saturating_sub(expired_archived)
    );
    for keeper in &report.keepers {
        println!("  keeper: {}", keeper);
    }
    Ok(())
}
