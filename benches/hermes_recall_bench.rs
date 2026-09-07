//! End-to-end `hybrid_search` latency at personal-agent scale.
//!
//! Models what a Hermes agent actually pays per `mnemosyne_recall` tool call:
//! one keyless (no embedding service) hybrid query over a 10k-memory store with
//! FTS keyword + graph expansion + PPR blend, including the retrieval-trace
//! write each query performs.
//!
//! Run with: cargo bench --bench hermes_recall_bench
//!
//! Emits `METRIC name=value` lines for the autoresearch harness
//! (`.auto/measure.sh`). Tunables via env: `BENCH_MEMORIES`, `BENCH_DEGREE`,
//! `BENCH_QUERIES`, `BENCH_REPEATS`.
//!
//! `results_hash` is an FNV-1a hash of the top-3 memory ids returned by every
//! query in the primary (PPR-on) phase. A pure performance change must leave it
//! byte-identical; if it moves, the change altered ranking, not just speed.

use chrono::{Duration, Utc};
use mnemosyne_core::config::SearchConfig;
use mnemosyne_core::storage::libsql::{ConnectionMode, LibsqlStorage};
use mnemosyne_core::storage::StorageBackend;
use mnemosyne_core::types::{LinkType, MemoryClass, MemoryId, MemoryLink, MemoryNote, MemoryType};
use std::collections::HashMap;
use std::time::Instant;

const VOCAB: &[&str] = &[
    "rust",
    "cargo",
    "libsql",
    "embedding",
    "fts",
    "index",
    "cache",
    "latency",
    "benchmark",
    "hermes",
    "agent",
    "memory",
    "namespace",
    "session",
    "project",
    "preference",
    "workflow",
    "kernel",
    "inference",
    "token",
    "budget",
    "privacy",
    "encryption",
    "keyring",
    "backup",
    "sync",
    "device",
    "calendar",
    "email",
    "meeting",
    "recipe",
    "workout",
    "sleep",
    "travel",
    "flight",
    "hotel",
    "spanish",
    "guitar",
    "python",
    "docker",
    "deploy",
    "webhook",
    "secret",
    "dashboard",
    "grafana",
    "prometheus",
    "alert",
    "incident",
    "postmortem",
    "budget",
    "lease",
];
const NOUNS: &[&str] = &[
    "decision",
    "constraint",
    "pattern",
    "note",
    "insight",
    "task",
    "reference",
    "policy",
    "tradeoff",
    "failure",
    "fix",
    "config",
    "log",
    "plan",
    "review",
];
const VERBS: &[&str] = &[
    "prefers",
    "requires",
    "avoids",
    "replaces",
    "extends",
    "blocks",
    "unblocks",
    "measures",
    "defers",
    "validates",
    "caches",
    "invalidates",
    "documents",
    "escalates",
    "resolves",
];

/// Queries a personal agent would plausibly issue. Two-to-four topical words so
/// the FTS channel has real matching work to do.
const QUERIES: &[&str] = &[
    "libsql index latency",
    "hermes agent memory namespace",
    "privacy encryption keyring",
    "benchmark cargo build",
    "session token budget",
    "incident postmortem alert",
    "travel flight hotel",
    "python docker deploy",
    "preference workflow preference",
    "graph memory recall",
    "backup sync device",
    "meeting calendar email",
    "embedding inference kernel",
    "dashboard grafana prometheus",
    "recipe workout sleep",
    "spanish guitar lesson",
    "webhook secret lease",
    "cache invalidates pattern",
    "constraint tradeoff review",
    "project namespace isolation",
    "agent escalates failure",
    "fts keyword ranking",
    "sleep tracking budget",
    "deploy rollback plan",
];

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

fn env_usize(key: &str, default: usize) -> usize {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn sentence(rng: &mut Rng) -> String {
    let words = 14 + rng.below(12);
    let mut parts: Vec<String> = Vec::with_capacity(words);
    for i in 0..words {
        let w = match i % 3 {
            0 => VOCAB[rng.below(VOCAB.len())],
            1 => NOUNS[rng.below(NOUNS.len())],
            _ => VERBS[rng.below(VERBS.len())],
        };
        parts.push(w.to_string());
    }
    parts.join(" ")
}

fn note(id: MemoryId, content: String, summary: String, links: Vec<MemoryLink>) -> MemoryNote {
    let now = Utc::now();
    MemoryNote {
        id,
        namespace: mnemosyne_core::types::Namespace::Global,
        created_at: now - Duration::days(30),
        updated_at: now,
        content,
        summary,
        keywords: vec![],
        tags: vec![],
        context: String::new(),
        memory_type: MemoryType::Insight,
        memory_class: MemoryClass::Knowledge,
        provenance: None,
        importance: 5,
        confidence: 0.7,
        links,
        related_files: vec![],
        related_entities: vec![],
        access_count: 0,
        last_accessed_at: now,
        expires_at: None,
        is_archived: false,
        superseded_by: None,
        embedding: None,
        embedding_model: String::new(),
    }
}

fn link_to(target: MemoryId, strength: f32) -> MemoryLink {
    MemoryLink {
        target_id: target,
        link_type: LinkType::References,
        strength,
        reason: "bench".into(),
        created_at: Utc::now(),
        last_traversed_at: None,
        user_created: false,
    }
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn fnv(hash: &mut u64, bytes: &[u8]) {
    for b in bytes {
        *hash ^= *b as u64;
        *hash = hash.wrapping_mul(0x0100_0000_01b3);
    }
}

/// Run every query `repeats` times against the current store config. Returns
/// (latencies in ms, average result count, ranking hash).
async fn sample(store: &LibsqlStorage, repeats: usize) -> (Vec<f64>, f64, u64) {
    let mut latencies = Vec::with_capacity(QUERIES.len() * repeats);
    let mut total_results = 0usize;
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for _ in 0..repeats {
        for query in QUERIES {
            let started = Instant::now();
            let results = store
                .hybrid_search(query, None, 10, true)
                .await
                .expect("hybrid_search failed");
            latencies.push(started.elapsed().as_secs_f64() * 1000.0);
            total_results += results.len();
            for r in results.iter().take(3) {
                fnv(&mut hash, r.memory.id.to_string().as_bytes());
            }
        }
    }
    latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let avg = total_results as f64 / (QUERIES.len() * repeats) as f64;
    (latencies, avg, hash)
}

fn report(tag: &str, lat: &[f64], avg_results: f64, hash: u32) -> HashMap<String, f64> {
    let p50 = percentile(lat, 0.50);
    let p95 = percentile(lat, 0.95);
    let mean = lat.iter().sum::<f64>() / lat.len() as f64;
    eprintln!(
        "note: {tag}: n={} p50={:.2}ms p95={:.2}ms mean={:.2}ms results/query={avg_results:.2} hash={hash:08x}",
        lat.len(),
        p50,
        p95,
        mean
    );
    let mut out = HashMap::new();
    out.insert(format!("{tag}_p50_ms"), p50);
    out.insert(format!("{tag}_p95_ms"), p95);
    out.insert(format!("{tag}_mean_ms"), mean);
    out.insert(format!("{tag}_results_per_query"), avg_results);
    out
}

#[tokio::main]
async fn main() {
    let n_memories = env_usize("BENCH_MEMORIES", 2_000);
    let degree = env_usize("BENCH_DEGREE", 6);
    let repeats = env_usize("BENCH_REPEATS", 2);

    let built = Instant::now();
    let mut store = LibsqlStorage::new_with_validation(ConnectionMode::InMemory, true)
        .await
        .expect("failed to open in-memory store");
    let mut cfg = SearchConfig::default();
    // Keyless personal-agent path: no embedding service is registered, so the
    // vector channel is inert and keyword + graph + PPR carry the ranking.
    cfg.enable_vector_search = false;
    cfg.enable_graph_expansion = true;
    cfg.enable_ppr = true;
    store.set_search_config(cfg);

    let mut ingest_ms = 0.0f64;
    let mut rng = Rng(0x5EED_CAFE_1234_9999);
    let ids: Vec<MemoryId> = (0..n_memories).map(|_| MemoryId::new()).collect();
    let notes: Vec<MemoryNote> = (0..n_memories)
        .map(|i| {
            let links: Vec<MemoryLink> = if i == 0 {
                vec![]
            } else {
                (0..degree.min(i))
                    .map(|_| link_to(ids[rng.below(i)], 0.3 + (rng.below(70) as f32) / 100.0))
                    .collect()
            };
            let summary = format!(
                "{} {}",
                VOCAB[rng.below(VOCAB.len())],
                NOUNS[rng.below(NOUNS.len())]
            );
            note(ids[i], sentence(&mut rng), summary, links)
        })
        .collect();

    // Fixture ingest is not the measured path; keep it cheap. `store_memory` is
    // latency-dominated per call, so overlapping calls with BENCH_CONC cuts the
    // setup cost without touching the production write path.
    let conc = env_usize("BENCH_CONC", 1).max(1);
    let mut store: LibsqlStorage = if conc == 1 {
        for n in &notes {
            store.store_memory(n).await.expect("store_memory failed");
        }
        store
    } else {
        let holder = std::sync::Arc::new(store);
        let per = notes.len().div_ceil(conc);
        let mut handles = Vec::new();
        for chunk in notes.chunks(per) {
            let holder = std::sync::Arc::clone(&holder);
            let chunk = chunk.to_vec();
            handles.push(tokio::spawn(async move {
                for n in &chunk {
                    holder.store_memory(n).await.expect("store_memory failed");
                }
            }));
        }
        for h in handles {
            h.await.expect("ingest task panicked");
        }
        std::sync::Arc::try_unwrap(holder).unwrap_or_else(|_| panic!("ingest Arc leak"))
    };

    ingest_ms = built.elapsed().as_secs_f64() * 1000.0;
    eprintln!(
        "note: ingested {} memories in {:.1}s ({} links, conc {})",
        n_memories,
        ingest_ms / 1000.0,
        degree,
        conc
    );

    // Warm-up: FTS page cache, statement caches, allocator warm-up.
    let _ = sample(&store, 1);

    // Localization pass: time the individual retrieval channels so a
    // regression in one of them points at a file instead of a guess.
    if std::env::var("BENCH_PROFILE").is_ok() {
        macro_rules! probe {
            ($label:expr, $body:expr) => {{
                let mut best = f64::MAX;
                for _ in 0..5 {
                    let t = Instant::now();
                    let n = $body;
                    let ms = t.elapsed().as_secs_f64() * 1000.0;
                    best = best.min(ms);
                    eprintln!("note: profile {label} = {ms:.2}ms (n={n})", label = $label);
                }
                eprintln!("note: profile-best {label} = {best:.2}ms", label = $label);
            }};
        }
        let q = QUERIES[0].to_string();
        let seeds: Vec<MemoryId> = ids.iter().take(5).copied().collect();
        probe!("keyword_search", {
            store.keyword_search(&q, None).await.unwrap().len()
        });
        probe!("graph_traverse_bounded", {
            store
                .graph_traverse_bounded(&seeds, 2, None, 1000)
                .await
                .unwrap()
                .len()
        });
        probe!("fetch_ppr_adjacency", {
            store
                .fetch_ppr_adjacency(&seeds, 2, None)
                .await
                .unwrap()
                .len()
        });
        probe!("count_memories", {
            store.count_memories(None).await.unwrap()
        });
        probe!("retrieval_weights", {
            store.retrieval_weights().await.keyword
        });
        probe!("record_retrieval_trace", {
            let trace = mnemosyne_core::utils::retrieval::RetrievalTrace::for_query(
                &q,
                store.retrieval_weights().await,
            );
            store
                .record_retrieval_trace(&trace)
                .await
                .map(|_| 1)
                .unwrap_or(0)
        });
    }

    let mut metrics: HashMap<String, f64> = HashMap::new();
    let (lat, avg, hash) = sample(&store, repeats).await;
    metrics.extend(report("hybrid_ppr", &lat, avg, (hash >> 32) as u32));

    let mut cfg = SearchConfig::default();
    cfg.enable_vector_search = false;
    cfg.enable_graph_expansion = true;
    cfg.enable_ppr = false;
    store.set_search_config(cfg);
    let (lat, avg, _) = sample(&store, repeats).await;
    metrics.extend(report("hybrid", &lat, avg, 0));

    let mut cfg = SearchConfig::default();
    cfg.enable_vector_search = false;
    cfg.enable_graph_expansion = false;
    cfg.enable_ppr = false;
    store.set_search_config(cfg);
    let (lat, avg, _) = sample(&store, repeats).await;
    metrics.extend(report("keyword_only", &lat, avg, 0));

    let primary = *metrics.get("hybrid_ppr_p95_ms").unwrap();
    metrics.insert("ppr_delta_ms".into(), primary - metrics["hybrid_p95_ms"]);
    metrics.insert(
        "graph_delta_ms".into(),
        metrics["hybrid_p95_ms"] - metrics["keyword_only_p95_ms"],
    );
    // Truncated to 32 bits: f64 (53-bit mantissa) carries it exactly, so the
    // METRIC line stays machine-comparable.
    metrics.insert("results_hash".into(), (hash >> 32) as f64);
    metrics.insert("ingest_ms".into(), ingest_ms);

    let mut keys: Vec<&String> = metrics.keys().collect();
    keys.sort();
    for key in keys {
        println!("METRIC {}={:.4}", key, metrics[key]);
    }
}
