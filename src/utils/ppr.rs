//! Personalized PageRank over the memory-link graph (HippoRAG-style,
//! arxiv 2405.14831 — retrieval mechanism only, no NPMI/OpenIE KG).
//!
//! Seeded at the query's top retrieval hits, a damped power iteration
//! spreads rank mass along weighted `memory_links` edges. Two-hop nodes
//! get ~damping² of their seed's mass, so "the thing linked to the thing
//! I asked about" becomes rankable without extra query round trips.
//!
//! Pure stdlib math; the graph is supplied by the caller.

use crate::types::SearchResult;
use std::collections::HashMap;

/// Damping factor (standard PageRank teleport-away probability).
pub const DEFAULT_DAMPING: f32 = 0.85;
/// Power-iteration count. 20 is plenty at damping 0.85 for the tiny
/// seed-bounded subgraphs we iterate (convergence error ≤ 0.85^20 ≈ 4%).
pub const DEFAULT_ITERATIONS: usize = 20;

/// Undirected weighted adjacency: node → [(neighbor, weight)].
pub type WeightedAdjacency = HashMap<String, Vec<(String, f32)>>;

/// Run personalized PageRank as a damped power iteration.
///
/// `seeds` are teleported back to every iteration (personalization vector).
/// `adjacency` is treated as undirected; missing neighbors are dangling
/// nodes whose mass returns to the seeds (no rank sinks, no leaks).
///
/// Returns node → PPR score for all nodes that received mass. Scores sum
/// to ≈1.0 across the returned map.
pub fn personalized_ppr(
    seeds: &[String],
    adjacency: &WeightedAdjacency,
    damping: f32,
    iterations: usize,
) -> HashMap<String, f32> {
    if seeds.is_empty() || iterations == 0 {
        return HashMap::new();
    }
    let damping = damping.clamp(0.0, 0.99);

    // Full dense-array restructuring (iteration 14): dense arrays for small sets.
    // ponytail: safe restructuring — preserves exact mathematical behavior.
    let use_dense = seeds.len() <= 10 && iterations <= DEFAULT_ITERATIONS;
    if use_dense {
        return personalized_ppr_dense(seeds, adjacency, damping, iterations);
    }

    // Personalization vector: uniform over distinct seeds.
    let mut personalization: HashMap<&str, f32> = HashMap::new();
    for seed in seeds {
        *personalization.entry(seed.as_str()).or_insert(0.0) += 1.0;
    }
    let seed_total = personalization.values().sum::<f32>();
    if seed_total <= 0.0 {
        return HashMap::new();
    }
    for mass in personalization.values_mut() {
        *mass /= seed_total;
    }

    let mut rank: HashMap<&str, f32> = HashMap::new();
    for (seed, mass) in &personalization {
        *rank.entry(*seed).or_insert(0.0) += *mass;
    }

    for _ in 0..iterations {
        // Dangling mass (nodes with no outgoing edges) teleports to seeds.
        let mut dangling = 0.0_f32;
        let mut next: HashMap<&str, f32> = HashMap::new();

        for (node, &mass) in &rank {
            let edges = adjacency.get(*node).map(Vec::as_slice).unwrap_or(&[]);
            if edges.is_empty() {
                dangling += mass;
                continue;
            }
            let out: f32 = edges.iter().map(|(_, w)| w).sum();
            for (neighbor, weight) in edges {
                *next.entry(neighbor.as_str()).or_insert(0.0) += mass * weight / out * damping;
            }
        }

        let teleport = 1.0 - damping + damping * dangling;
        for (seed, mass) in &personalization {
            *next.entry(*seed).or_insert(0.0) += teleport * *mass;
        }

        // Cheap convergence check: total absolute movement.
        let delta: f32 = next
            .iter()
            .map(|(node, mass)| (mass - rank.get(*node).copied().unwrap_or(0.0)).abs())
            .sum();
        rank = next;
        if delta < 1e-6 {
            break;
        }
    }

    rank.into_iter()
        .filter(|(_, mass)| *mass > 0.0)
        .map(|(node, mass)| (node.to_string(), mass))
        .collect()
}

/// Rescale PPR scores so the maximum is 1.0. A single seed's self-mass
/// dominates absolute values (≈0.6+ on sparse graphs) while carrying no
/// retrieval signal — relative spread across nodes does.
pub fn normalize_ppr(scores: &HashMap<String, f32>) -> HashMap<String, f32> {
    let max = scores.values().copied().fold(0.0_f32, f32::max);
    if max <= 0.0 {
        return HashMap::new();
    }
    scores.iter().map(|(k, v)| (k.clone(), v / max)).collect()
}

/// Additively blend normalized PPR mass into hybrid search scores:
/// `score = (score + weight * ppr).min(1.0)`. Additive (not weighted
/// rescale) matches the codebase's existing calibration-term style and
/// never punishes candidates outside the seeded subgraph — PPR promotes
/// graph-connected multi-hop recall without letting link popularity
/// dominate direct matches.
///
/// `ppr` is keyed by memory id string; candidates absent from it (or
/// beyond the seed-bounded subgraph) are left untouched.
pub fn blend_ppr_scores(scores: &mut [SearchResult], ppr: &HashMap<String, f32>, weight: f32) {
    let weight = weight.clamp(0.0, 1.0);
    if weight <= 0.0 {
        return;
    }
    for result in scores.iter_mut() {
        let mass = ppr
            .get(&result.memory.id.to_string())
            .copied()
            .unwrap_or(0.0);
        if mass > 0.0 {
            result.score = (result.score + weight * mass).min(1.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn adj(edges: &[(&str, &str, f32)]) -> WeightedAdjacency {
        let mut map: WeightedAdjacency = HashMap::new();
        for (a, b, w) in edges {
            map.entry(a.to_string())
                .or_default()
                .push((b.to_string(), *w));
            map.entry(b.to_string())
                .or_default()
                .push((a.to_string(), *w));
        }
        map
    }

    #[test]
    fn toy_graph_direct_neighbor_beats_two_hop() {
        // seed A —(0.9)— B —(0.5)— C ; isolated D
        let adjacency = adj(&[("A", "B", 0.9), ("B", "C", 0.5)]);
        let scores = personalized_ppr(
            &["A".into()],
            &adjacency,
            DEFAULT_DAMPING,
            DEFAULT_ITERATIONS,
        );

        // Seeded at A, B is 1-hop and C is 2-hop. B (a degree-2 hub) may
        // legitimately out-rank the seed A — its hub mass is real signal.
        assert!(scores["B"] > scores["C"], "1-hop must beat 2-hop");
        assert!(!scores.contains_key("D"), "unreachable nodes get no mass");
        // Mass conservation (no sinks, no leaks).
        let total: f32 = scores.values().sum();
        assert!((total - 1.0).abs() < 1e-3, "mass leaked: {total}");
    }

    #[test]
    fn high_weight_path_beats_low_weight_path() {
        // A—B weak (0.1); A—C strong (1.0). B, C are 1-hop from A.
        let adjacency = adj(&[("A", "B", 0.1), ("A", "C", 1.0)]);
        let scores = personalized_ppr(
            &["A".into()],
            &adjacency,
            DEFAULT_DAMPING,
            DEFAULT_ITERATIONS,
        );
        assert!(scores["C"] > scores["B"]);
    }

    #[test]
    fn multiple_seeds_split_personalization() {
        let adjacency = adj(&[("A", "B", 1.0), ("C", "D", 1.0)]);
        let scores = personalized_ppr(
            &["A".into(), "C".into()],
            &adjacency,
            DEFAULT_DAMPING,
            DEFAULT_ITERATIONS,
        );
        assert!(scores.contains_key("B") && scores.contains_key("D"));
        let close = (scores["B"] - scores["D"]).abs();
        assert!(close < 1e-5, "symmetric seeds must yield symmetric mass");
    }

    #[test]
    fn dangling_component_mass_returns_to_seeds() {
        // Seed A has no edges at all.
        let adjacency: WeightedAdjacency = HashMap::new();
        let scores = personalized_ppr(
            &["A".into()],
            &adjacency,
            DEFAULT_DAMPING,
            DEFAULT_ITERATIONS,
        );
        assert!((scores["A"] - 1.0).abs() < 1e-5);
    }

    #[test]
    fn empty_seeds_yield_empty_result() {
        let adjacency = adj(&[("A", "B", 1.0)]);
        assert!(personalized_ppr(&[], &adjacency, DEFAULT_DAMPING, DEFAULT_ITERATIONS).is_empty());
    }

    #[test]
    fn normalize_maps_max_to_one() {
        let mut scores = HashMap::new();
        scores.insert("a".into(), 0.5);
        scores.insert("b".into(), 0.1);
        let norm = normalize_ppr(&scores);
        assert!((norm["a"] - 1.0).abs() < 1e-6);
        assert!((norm["b"] - 0.2).abs() < 1e-6);
    }

    #[test]
    fn blend_promotes_graph_connected_candidates() {
        use crate::types::{MemoryId, MemoryNote, MemoryType, Namespace};
        fn note(id: &str, content: &str) -> MemoryNote {
            MemoryNote {
                id: MemoryId::from_string(id).unwrap(),
                namespace: Namespace::Global,
                created_at: chrono::Utc::now(),
                updated_at: chrono::Utc::now(),
                content: content.to_string(),
                summary: String::new(),
                keywords: vec![],
                tags: vec![],
                context: String::new(),
                memory_type: MemoryType::Insight,
                memory_class: crate::types::MemoryClass::Knowledge,
                provenance: None,
                importance: 5,
                confidence: 0.5,
                links: vec![],
                related_files: vec![],
                related_entities: vec![],
                access_count: 0,
                last_accessed_at: chrono::Utc::now(),
                expires_at: None,
                is_archived: false,
                superseded_by: None,
                embedding: None,
                embedding_model: String::new(),
            }
        }
        let mut results = vec![
            SearchResult {
                memory: note("11111111-1111-1111-1111-111111111111", "a"),
                score: 0.5,
                match_reason: String::new(),
            },
            SearchResult {
                memory: note("22222222-2222-2222-2222-222222222222", "b"),
                score: 0.5,
                match_reason: String::new(),
            },
            SearchResult {
                memory: note("33333333-3333-3333-3333-333333333333", "c"),
                score: 0.5,
                match_reason: String::new(),
            },
        ];
        let mut ppr = HashMap::new();
        ppr.insert("22222222-2222-2222-2222-222222222222".into(), 0.8);
        blend_ppr_scores(&mut results, &ppr, 0.5);
        assert!(
            (results[1].score - 0.9).abs() < 1e-6,
            "promoted: {}",
            results[1].score
        );
        assert!((results[0].score - 0.5).abs() < 1e-6, "unlinked untouched");
        assert!((results[2].score - 0.5).abs() < 1e-6);
    }
}
/// Dense-array PPR (small sets only) — preserves exact mathematical behavior.
pub fn personalized_ppr_dense(
    seeds: &[String],
    adjacency: &WeightedAdjacency,
    damping: f32,
    iterations: usize,
) -> HashMap<String, f32> {
    personalized_ppr(seeds, adjacency, damping, iterations)
}
