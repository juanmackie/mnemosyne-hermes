//! Hermes MCP stdio contract test.
//!
//! Drives `mnemosyne serve` exactly the way Hermes Agent's MCP client does:
//! spawn the binary, speak newline-delimited JSON-RPC 2.0 over stdin/stdout,
//! and assert that stdout carries ONLY protocol messages while logs go to
//! stderr. Covers the full lifecycle remember → recall → update → forget →
//! recall(confirms gone) plus malformed-call tolerance (a small local model
//! WILL malform calls; the server must answer with JSON errors, never die).

use serde_json::{json, Value};
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::time::{timeout, Duration};

fn tmp_db(tag: &str) -> String {
    let dir = std::env::temp_dir().join(format!(
        "mnx-stdio-{}-{}-{}",
        tag,
        std::process::id(),
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
    ));
    std::fs::create_dir_all(&dir).expect("mkdir");
    dir.join("contract.db").display().to_string()
}

async fn spawn_server(
    db: &str,
) -> (
    tokio::process::Child,
    BufReader<tokio::process::ChildStdout>,
    tokio::process::ChildStdin,
) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_mnemosyne"));
    child
        .args(["serve"])
        .env("MNEMOSYNE_DB_PATH", db)
        .env("MNEMOSYNE_LOG_LEVEL", "warn")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut proc = child.spawn().expect("spawn mnemosyne serve");
    let stdin = proc.stdin.take().expect("stdin");
    let stdout = proc.stdout.take().expect("stdout");
    (proc, BufReader::new(stdout), stdin)
}

/// Send one request and read back exactly one newline-delimited JSON response.
async fn rpc(
    reader: &mut BufReader<tokio::process::ChildStdout>,
    stdin: &mut tokio::process::ChildStdin,
    request: &Value,
) -> Value {
    let line = serde_json::to_string(request).expect("serialize request");
    stdin.write_all(line.as_bytes()).await.expect("write req");
    stdin.write_all(b"\n").await.expect("write nl");
    stdin.flush().await.expect("flush");

    let mut response_line = String::new();
    timeout(
        Duration::from_secs(30),
        reader.read_line(&mut response_line),
    )
    .await
    .expect("response within 30s (server must not hang)")
    .expect("server closed stdout unexpectedly");

    // Contract: stdout lines are single-line JSON objects — no logs, no banners.
    serde_json::from_str(&response_line)
        .unwrap_or_else(|e| panic!("stdout line is not valid JSON ({e}): {response_line}"))
}

/// Extract the tool's own JSON payload from an MCP-wrapped tools/call response.
fn tool_payload(resp: &Value) -> Value {
    let text = resp["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_else(|| panic!("tools/call response missing content[0].text: {resp}"));
    serde_json::from_str(text)
        .unwrap_or_else(|e| panic!("content[0].text is not JSON ({e}): {text}"))
}

#[tokio::test]
async fn hermes_stdio_full_lifecycle_and_malformed_tolerance() {
    let db = tmp_db("lifecycle");
    let (mut proc, mut reader, mut stdin) = spawn_server(&db).await;

    // ---- 1. initialize handshake -------------------------------------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    )
    .await;
    assert_eq!(resp["jsonrpc"], "2.0");
    assert_eq!(resp["id"], 1);
    assert!(
        resp["result"]["protocolVersion"].is_string(),
        "handshake must return protocolVersion"
    );
    assert_eq!(resp["result"]["serverInfo"]["name"], "mnemosyne");

    // ---- 2. tools/list ------------------------------------------------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    )
    .await;
    let tools = resp["result"]["tools"].as_array().expect("tools array");
    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
    for expected in [
        "mnemosyne.remember",
        "mnemosyne.recall",
        "mnemosyne.update",
        "mnemosyne.delete",
    ] {
        assert!(
            names.contains(&expected),
            "missing tool {expected}: {names:?}"
        );
    }

    // ---- 3. remember with namespace OMITTED (malformed-tolerant) ------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "mnemosyne.remember", "arguments": {
                "content": "The vault launch code is 7391-ALPHA",
                "importance": 8
            }}
        }),
    )
    .await;
    assert!(
        resp.get("error").is_none(),
        "remember without namespace must succeed: {resp}"
    );
    let payload = tool_payload(&resp);
    let memory_id = payload["memory_id"]
        .as_str()
        .expect("memory_id")
        .to_string();

    // ---- 4. recall finds it -------------------------------------------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "mnemosyne.recall", "arguments": {"query": "vault launch code"}}
        }),
    )
    .await;
    let payload = tool_payload(&resp);
    let results = payload["results"].as_array().expect("results array");
    assert!(
        results
            .iter()
            .any(|r| r["memory"]["id"] == json!(memory_id)),
        "recall must find the stored memory"
    );
    // Per-result confidence + abstention guidance present.
    assert!(
        results[0]["score"].is_number(),
        "results carry confidence scores"
    );
    assert!(
        payload["abstention_threshold"].is_number(),
        "abstention guidance must be documented in responses"
    );

    // ---- 5. update ------------------------------------------------------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "mnemosyne.update", "arguments": {
                "memory_id": memory_id,
                "importance": 10
            }}
        }),
    )
    .await;
    assert!(resp.get("error").is_none(), "update must succeed: {resp}");

    // ---- 6. delete ------------------------------------------------------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "mnemosyne.delete", "arguments": {"memory_id": memory_id}}
        }),
    )
    .await;
    assert!(resp.get("error").is_none(), "delete must succeed: {resp}");

    // ---- 7. recall confirms gone ---------------------------------------
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "mnemosyne.recall", "arguments": {"query": "vault launch code"}}
        }),
    )
    .await;
    let payload = tool_payload(&resp);
    let results = payload["results"].as_array().expect("results array");
    assert!(
        !results
            .iter()
            .any(|r| r["memory"]["id"] == json!(memory_id)),
        "deleted memory must not be recalled"
    );

    // ---- 8. malformed calls: JSON errors, server survives --------------
    // Invalid JSON entirely.
    stdin.write_all(b"this is not json\n").await.expect("write");
    let mut bad = String::new();
    timeout(Duration::from_secs(10), reader.read_line(&mut bad))
        .await
        .expect("parse-error response within 10s")
        .expect("stream open");
    let resp: Value = serde_json::from_str(&bad).expect("even parse errors are JSON");
    assert!(
        resp.get("error").is_some(),
        "invalid JSON must yield JSON-RPC error"
    );

    // Valid envelope, missing tool name.
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}}),
    )
    .await;
    assert!(resp.get("error").is_some(), "missing name must error");

    // Wrong argument types.
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "mnemosyne.remember", "arguments": {"content": 42}}
        }),
    )
    .await;
    assert!(
        resp.get("error").is_some() || resp["result"].is_object(),
        "wrong-type args must produce an error response, not a crash"
    );

    // Unknown method.
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 11, "method": "resources/list", "params": {}}),
    )
    .await;
    assert!(resp.get("error").is_some(), "unknown method must error");

    // Empty arguments object on a required-arg tool.
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {"name": "mnemosyne.recall", "arguments": {}}
        }),
    )
    .await;
    assert!(
        resp.get("error").is_some() || resp["result"].is_object(),
        "empty args must produce a response, not a crash"
    );

    // Every remaining tool tolerates empty/malformed args with a response.
    for (i, tool) in [
        "mnemosyne.list",
        "mnemosyne.graph",
        "mnemosyne.context",
        "mnemosyne.consolidate",
    ]
    .iter()
    .enumerate()
    {
        let id = 100 + i as i64;
        let resp = rpc(
            &mut reader,
            &mut stdin,
            &json!({"jsonrpc": "2.0", "id": id, "method": "tools/call",
                    "params": {"name": tool, "arguments": {}}}),
        )
        .await;
        assert!(
            resp.get("error").is_some() || resp["result"].is_object(),
            "{tool} with empty args must respond, not crash: {resp}"
        );
        assert_eq!(resp["id"], json!(id), "response id must match request");
    }

    // Server is still alive and answering after all abuse.
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": {}}),
    )
    .await;
    assert_eq!(resp["id"], 13, "server must survive malformed calls");

    // Clean shutdown of the spawned server.
    stdin.shutdown().await.ok();
    let _ = timeout(Duration::from_secs(10), proc.wait()).await;
}
