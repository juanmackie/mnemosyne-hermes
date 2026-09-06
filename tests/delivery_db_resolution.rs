//! Delivery-lane integration tests: DB-path resolution must be consistent so
//! that init, remember, recall, and export all target the same database when
//! `MNEMOSYNE_DB_PATH` is set.
//!
//! These tests are isolated: they use an owned temp directory and never touch
//! the developer's real home or cloud credentials. Home-relative `~` expansion
//! is covered by the pure unit tests in src/cli/helpers.rs.

use std::process::{Command, Output};
use tempfile::TempDir;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_mnemosyne")
}

fn run_with_db(db_path: &str, args: &[&str]) -> Output {
    Command::new(bin())
        .args(args)
        .env("MNEMOSYNE_DB_PATH", db_path)
        .env("RUST_LOG", "off")
        .output()
        .expect("failed to spawn mnemosyne binary")
}

/// All memory-facing commands must resolve the same DB from the env var, so
/// init/remember/recall/export share one store.
#[test]
fn commands_resolve_to_same_db_from_env() {
    let tmp = TempDir::new().unwrap();
    let db = tmp.path().join("shared.db");
    let db_str = db.to_str().unwrap();

    let init = run_with_db(db_str, &["init"]);
    assert!(
        init.status.success(),
        "init failed: {}",
        String::from_utf8_lossy(&init.stdout)
    );
    assert!(db.exists(), "init did not create the DB at the env path");

    let remember = run_with_db(db_str, &["remember", "--content", "delivery e2e", "--no-enrich"]);
    assert!(
        remember.status.success(),
        "remember failed: {}",
        String::from_utf8_lossy(&remember.stdout)
    );

    let recall = run_with_db(db_str, &["recall", "--query", "delivery e2e"]);
    assert!(
        recall.status.success(),
        "recall failed: {}",
        String::from_utf8_lossy(&recall.stdout)
    );
    let recall_out = String::from_utf8_lossy(&recall.stdout);
    assert!(
        recall_out.contains("delivery e2e"),
        "recall did not find the memory in the same DB; output: {recall_out}"
    );

    let export = run_with_db(db_str, &["export"]);
    assert!(
        export.status.success(),
        "export failed: {}",
        String::from_utf8_lossy(&export.stdout)
    );
}
