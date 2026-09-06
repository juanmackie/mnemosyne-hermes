//! Performance benchmarks for Mnemosyne memory operations
//!
//! Run with: cargo bench
//!
//! `ppr_blend_10k` models the Personalized PageRank blend step (HippoRAG-style,
//! arxiv 2405.14831, retrieval mechanism only) against a synthetic 10k-memory
//! store at the scale the plan's latency target calls out ("PPR blend < 10ms
//! at 10k memories / p95 < 200ms").
//!
//! Faithful to the production path: `hybrid_search` does NOT run the power
//! iteration over all 10k nodes — `fetch_ppr_adjacency` caps the traversal at
//! 2 hops from the seed set, so the blend always iterates a small seed-bounded
//! subgraph. This benchmark therefore (1) builds a 10k-node weighted store
//! graph, (2) extracts the 2-hop seed subgraph exactly as the storage layer
//! would, and (3) times the pure-stdlib `personalized_ppr` + `normalize_ppr`
//! on that subgraph — the cost the `retrieval.ppr` flag adds per query.

use criterion::{black_box, criterion_group, criterion_main, Criterion};
use mnemosyne_core::utils::ppr::{
    normalize_ppr, personalized_ppr, DEFAULT_DAMPING, DEFAULT_ITERATIONS, WeightedAdjacency,
};
use std::collections::{HashMap, HashSet};

/// Build a synthetic small-world-ish weighted store graph for N memories.
/// Each node links to ~D random neighbors with a decaying strength, giving
/// meaningful multi-hop structure without pathological density.
fn build_store_graph(n: usize, degree: usize, seed: u64) -> WeightedAdjacency {
    let mut rng_state: u64 = seed ^ 0x9E37_79B9_7F4A_7C15;
    let mut next = || {
        // xorshift64 — small, deterministic.
        rng_state ^= rng_state << 13;
        rng_state ^= rng_state >> 7;
        rng_state ^= rng_state << 17;
        rng_state
    };

    let mut adj: WeightedAdjacency = HashMap::with_capacity(n);
    for i in 0..n {
        let id = format!("mem_{}", i);
        for _ in 0..degree {
            let j = (next() as usize) % n;
            if j == i {
                continue;
            }
            let tid = format!("mem_{}", j);
            let strength = 0.95_f32.powf(((i as isize - j as isize).unsigned_abs() % 64) as f32);
            adj.entry(id.clone()).or_default().push((tid.clone(), strength));
            adj.entry(tid).or_default().push((id.clone(), strength));
        }
    }
    adj.retain(|_, v| !v.is_empty());
    adj
}

/// Extract the 2-hop seed subgraph exactly like the storage layer's
/// `fetch_ppr_adjacency` (Rust BFS, `max_hops` capped at 2), then convert it
/// into the undirected weighted adjacency the power iteration consumes.
fn two_hop_subgraph(graph: &WeightedAdjacency, seeds: &[String], max_hops: usize) -> WeightedAdjacency {
    let mut reachable: HashSet<String> = HashSet::new();
    let mut frontier: HashSet<String> = seeds.iter().cloned().collect();
    reachable.extend(frontier.iter().cloned());
    for _ in 0..max_hops {
        let mut next_frontier: HashSet<String> = HashSet::new();
        for node in frontier.iter() {
            if let Some(neighbors) = graph.get(node) {
                for (nbr, _) in neighbors {
                    if reachable.insert(nbr.clone()) {
                        next_frontier.insert(nbr.clone());
                    }
                }
            }
        }
        frontier = next_frontier;
        if frontier.is_empty() {
            break;
        }
    }
    // Build the undirected weighted adjacency restricted to the reachable set.
    let mut out: WeightedAdjacency = HashMap::new();
    for node in reachable.iter() {
        let edges = out.entry(node.clone()).or_default();
        if let Some(neighbors) = graph.get(node) {
            for (nbr, w) in neighbors {
                if reachable.contains(nbr) {
                    edges.push((nbr.clone(), *w));
                }
            }
        }
    }
    out
}

fn bench_ppr_blend_10k(c: &mut Criterion) {
    const N: usize = 10_000;
    const DEGREE: usize = 6;
    let graph = build_store_graph(N, DEGREE, 0xC0FFEE);
    let seeds: Vec<String> = (0..5).map(|i| format!("mem_{}", i * 1999)).collect();
    // The production path never iterates the whole store — only the 2-hop seed
    // subgraph. Extract it once here; the benchmark times the subgraph PPR.
    let subgraph = two_hop_subgraph(&graph, &seeds, 2);
    eprintln!(
        "note: 2-hop seed subgraph of 10k-store = {} nodes (power iteration is bounded to this)",
        subgraph.len()
    );

    c.bench_function("ppr_blend_10k (5 seeds, 2-hop subgraph)", |b| {
        b.iter(|| {
            let scores = personalized_ppr(
                &seeds,
                &subgraph,
                DEFAULT_DAMPING,
                DEFAULT_ITERATIONS,
            );
            black_box(normalize_ppr(&scores))
        })
    });
}

criterion_group!(benches, bench_ppr_blend_10k);
criterion_main!(benches);
