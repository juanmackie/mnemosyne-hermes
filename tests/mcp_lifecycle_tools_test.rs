//! End-to-end contract for the Hermes lifecycle MCP tools.
//!
//! Drives `mnemosyne serve` over newline-delimited JSON-RPC 2.0 on stdio and
//! proves the native-provider path: a preference captured with `sync_turn` is
//! retrievable by `prefetch` in a fresh session with no explicit memory-tool
//! request, and non-interactive execution contexts neither capture nor inject.
//!
//! The prefetch query deliberately reuses the captured wording so retrieval is
//! deterministic on the keyless build (deterministic-hash fallback embeddings
//! are not semantically meaningful, but keyword/FTS recall still is).

use serde_json::{json, Value};
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::time::{timeout, Duration};

fn tmp_db(tag: &str) -> String {
    let dir = std::env::temp_dir().join(format!(
        "mnx-lifecycle-{}-{}-{}",
        tag,
        std::process::id(),
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
    ));
    std::fs::create_dir_all(&dir).expect("mkdir");
    dir.join("lifecycle.db").display().to_string()
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
        Duration::from_secs(60),
        reader.read_line(&mut response_line),
    )
    .await
    .expect("response within 60s (the server must not hang)")
    .expect("server closed stdout unexpectedly");

    serde_json::from_str(&response_line)
        .unwrap_or_else(|e| panic!("stdout line is not valid JSON ({e}): {response_line}"))
}

fn tool_payload(resp: &Value) -> Value {
    let text = resp["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_else(|| panic!("tools/call response missing content[0].text: {resp}"));
    serde_json::from_str(text)
        .unwrap_or_else(|e| panic!("content[0].text is not JSON ({e}): {text}"))
}

async fn call(
    reader: &mut BufReader<tokio::process::ChildStdout>,
    stdin: &mut tokio::process::ChildStdin,
    id: u64,
    name: &str,
    arguments: Value,
) -> Value {
    tool_payload(
        &rpc(
            reader,
            stdin,
            &json!({
                "jsonrpc": "2.0", "id": id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}
            }),
        )
        .await,
    )
}

#[tokio::test]
async fn lifecycle_tools_capture_then_prefetch_and_honour_skip_contexts() {
    let db = tmp_db("tools");
    let (mut proc, mut reader, mut stdin) = spawn_server(&db).await;

    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    )
    .await;
    assert_eq!(resp["result"]["serverInfo"]["name"], "mnemosyne");

    // Both dotted names and the underscore aliases are advertised, so a client
    // that cannot express dots still reaches the same handlers.
    let resp = rpc(
        &mut reader,
        &mut stdin,
        &json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    )
    .await;
    let names: Vec<String> = resp["result"]["tools"]
        .as_array()
        .expect("tools array")
        .iter()
        .filter_map(|t| t["name"].as_str().map(str::to_owned))
        .collect();
    for expected in [
        "mnemosyne.prefetch",
        "mnemosyne_prefetch",
        "mnemosyne.sync_turn",
        "mnemosyne_sync_turn",
    ] {
        assert!(
            names.contains(&expected.to_string()),
            "missing lifecycle tool {expected}: {names:?}"
        );
    }

    // ---- non-interactive contexts inject nothing and capture nothing -----
    for context in ["cron", "flush", "subagent", "background", "skill_loop"] {
        let payload = call(
            &mut reader,
            &mut stdin,
            10,
            "mnemosyne.prefetch",
            json!({"query": "concise bullet answers", "execution_context": context}),
        )
        .await;
        assert_eq!(
            payload["text"],
            json!(""),
            "{context} must not inject context"
        );
        assert_eq!(payload["skipped"], json!(true), "{context} reports skipped");
        assert_eq!(payload["count"], json!(0));

        let payload = call(
            &mut reader,
            &mut stdin,
            11,
            "mnemosyne.sync_turn",
            json!({
                "user_text": "I always want concise bullet answers",
                "assistant_text": "Understood.",
                "execution_context": context
            }),
        )
        .await;
        assert_eq!(
            payload["status"],
            json!("skipped"),
            "{context} must not capture"
        );
    }

    // ---- a genuine user turn is captured ---------------------------------
    let captured = call(
        &mut reader,
        &mut stdin,
        20,
        "mnemosyne.sync_turn",
        json!({
            "user_text": "For this project I always want concise bullet answers.",
            "assistant_text": "Noted, I will keep answers short.",
            "session_id": "session-a",
            "turn_id": "turn-1",
            "execution_context": "user_turn",
            "speaker": "user"
        }),
    )
    .await;
    assert_eq!(
        captured["status"],
        json!("captured"),
        "a user turn must capture: {captured}"
    );
    let source_id = captured["source_memory_id"]
        .as_str()
        .expect("captured turn reports its source memory")
        .to_string();

    // Replaying the same (session_id, turn_id) must not create a second turn.
    let replay = call(
        &mut reader,
        &mut stdin,
        21,
        "mnemosyne_sync_turn",
        json!({
            "user_text": "For this project I always want concise bullet answers.",
            "assistant_text": "Noted, I will keep answers short.",
            "session_id": "session-a",
            "turn_id": "turn-1",
            "execution_context": "user_turn"
        }),
    )
    .await;
    assert_eq!(replay["status"], json!("captured"));
    assert_eq!(
        replay["source_memory_id"].as_str(),
        Some(source_id.as_str()),
        "a replayed turn must reuse its memory instead of duplicating it"
    );

    // ---- a FRESH session retrieves it with no explicit memory request -----
    let prefetched = call(
        &mut reader,
        &mut stdin,
        30,
        "mnemosyne.prefetch",
        json!({
            "query": "concise bullet answers",
            "session_id": "session-b",
            "execution_context": "user_turn"
        }),
    )
    .await;
    let text = prefetched["text"].as_str().expect("prefetch text");
    assert!(
        prefetched["count"].as_u64().unwrap_or(0) >= 1,
        "fresh session must select captured context: {prefetched}"
    );
    assert!(
        text.contains("concise bullet answers"),
        "prefetched context must carry the captured preference: {text}"
    );
    assert!(
        !text.contains("<memory-context>"),
        "provider text must be UNFENCED so Hermes applies its own wrapper: {text}"
    );

    // Diagnostics must describe the real provider path without leaking text.
    let diagnostics = &prefetched["diagnostics"];
    assert!(diagnostics["embedding_mode"].is_string());
    assert!(diagnostics["embedding_dimensions"].is_number());
    assert!(diagnostics["section_counts"].is_object());
    assert!(diagnostics["selected_ids"].is_array());
    assert!(diagnostics["estimated_tokens"].is_number());
    assert!(diagnostics["skip_count"].is_number());

    drop(stdin);
    let _ = timeout(Duration::from_secs(15), proc.wait()).await;
}
