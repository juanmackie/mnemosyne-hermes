//! End-to-end reference lane: documentation written with `remember --reference`
//! must stay searchable on its own lane and must not surface as a memory.
//!
//! The doc below deliberately *out-ranks* the fact on every static signal
//! (higher importance, more query terms). If the lane ever stops being applied,
//! this test is the thing that fails.

use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_mnemosyne")
}

fn run(db: &PathBuf, args: &[&str]) -> Value {
    let out = Command::new(binary())
        .arg("--db-path")
        .arg(db)
        .args(args)
        .output()
        .expect("invoke mnemosyne");
    assert!(
        out.status.success(),
        "{args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap_or_else(|e| {
        panic!(
            "{args:?} did not print JSON: {e}: {}",
            String::from_utf8_lossy(&out.stdout)
        )
    })
}

fn contents(payload: &Value) -> Vec<String> {
    payload["results"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .map(|r| r["content"].as_str().unwrap_or_default().to_string())
                .collect()
        })
        .unwrap_or_default()
}

#[test]
fn reference_documents_are_recalled_only_on_their_own_lane() {
    let dir = tempfile::TempDir::new().unwrap();
    let db = dir.path().join("lane.db");
    let ns = "project:reference-lane";

    let store = |args: &[&str]| {
        run(
            &db,
            &[
                "remember",
                "--namespace",
                ns,
                "--no-enrich",
                "--format",
                "json",
            ]
            .into_iter()
            .chain(args.iter().copied())
            .collect::<Vec<_>>(),
        )
    };

    store(&[
        "--content",
        "Our ingest drops payloads over 1 MB at the load balancer",
        "--importance",
        "6",
    ]);
    let doc = store(&[
        "--content",
        "Vendor reference: the ingest gateway accepts a payload up to 1 MB per request",
        "--importance",
        "9",
        "--reference",
    ]);
    assert!(
        doc["tags"]
            .as_array()
            .map(|t| t.iter().any(|x| x == "reference_only"))
            .unwrap_or(false),
        "--reference must mark the row: {doc}"
    );

    let query = ["--query", "ingest payload 1 MB limit"];
    let recall = |extra: &[&str]| {
        run(
            &db,
            &[
                "recall",
                "--limit",
                "5",
                "--namespace",
                ns,
                "--format",
                "json",
            ]
            .into_iter()
            .chain(query.iter().copied())
            .chain(extra.iter().copied())
            .collect::<Vec<_>>(),
        )
    };

    let fact = "Our ingest drops payloads over 1 MB at the load balancer";
    let document = "Vendor reference: the ingest gateway accepts a payload up to 1 MB per request";

    let memory = recall(&[]);
    assert_eq!(memory["scope"].as_str(), Some("memory"));
    let got = contents(&memory);
    assert!(got.iter().any(|c| c == fact), "fact missing: {got:?}");
    assert!(
        !got.iter().any(|c| c == document),
        "reference doc leaked into the memory lane: {got:?}"
    );

    let docs = recall(&["--scope", "reference"]);
    assert_eq!(
        contents(&docs),
        vec![document.to_string()],
        "documentation lane"
    );

    let both = recall(&["--scope", "all"]);
    let got = contents(&both);
    assert_eq!(got.len(), 2, "scope=all must restore both lanes: {got:?}");

    // The lane is per namespace; an unrelated namespace sees neither row.
    let other = run(
        &db,
        &[
            "recall",
            "--limit",
            "5",
            "--namespace",
            "project:other",
            "--format",
            "json",
            "--scope",
            "all",
            "--query",
            "ingest payload 1 MB limit",
        ],
    );
    assert_eq!(contents(&other), Vec::<String>::new());
}
