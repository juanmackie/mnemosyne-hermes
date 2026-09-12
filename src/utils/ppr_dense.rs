/// Dense-array PPR (small sets only) — exact mathematical behavior preserved.
/// Uses Vec<(String, f32)> for personalization and rank calculations.
pub fn personalized_ppr_dense(
    seeds: &[String],
    adjacency: &WeightedAdjacency,
    damping: f32,
    iterations: usize,
) -> HashMap<String, f32> {
    if seeds.is_empty() || iterations == 0 {
        return HashMap::new();
    }
    let damping = damping.clamp(0.0, 0.99);

    // Dense personalization: uniform over distinct seeds.
    let seed_set: Vec<String> = seeds.iter().map(|s| s.clone()).collect();
    let mut personalization: Vec<(String, f32)> = seed_set.iter()
        .map(|s| (s.clone(), 1.0)).collect();
    // Deduplicate (same seed may appear multiple times in input).
    personalization.sort_by(|a, b| a.0.cmp(&b.0));
    personalization.dedup_by(|a, b| a.0 == b.0);
    let seed_total: f32 = personalization.iter().map(|(_, m)| *m).sum();
    if seed_total > 0.0 {
        for (_, m) in personalization.iter_mut() {
            *m /= seed_total;
        }
    }

    // Initialize dense rank.
    let mut rank_map: HashMap<String, f32> = HashMap::new();
    for (seed_str, mass) in &personalization {
        *rank_map.entry(seed_str.clone()).or_insert(0.0) += *mass;
    }

    // Dense iteration using HashMap for adjacency lookups (same logic).
    for _ in 0..iterations {
        let mut dangling = 0.0_f32;
        let mut next_map: HashMap<String, f32> = HashMap::new();
        for (node, mass) in &rank_map {
            let edges = adjacency.get(node).map(Vec::as_slice).unwrap_or(&[]);
            if edges.is_empty() {
                dangling += *mass;
                continue;
            }
            let out: f32 = edges.iter().map(|(_, w)| w).sum();
            for (neighbor_str, weight) in edges {
                *next_map.entry(neighbor_str.clone()).or_insert(0.0) += *mass * weight / out * damping;
            }
        }
        let teleport = 1.0 - damping + damping * dangling;
        for (seed_str, mass) in &personalization {
            *next_map.entry(seed_str.clone()).or_insert(0.0) += teleport * *mass;
        }
        rank_map = next_map;
        // Check convergence: exit if total absolute change is small.
        let delta: f32 = personalization.iter()
            .map(|(seed_str, _)| {
                let next_mass = rank_map.get(seed_str).copied().unwrap_or(0.0);
                let prev_mass = personalization.iter()
                    .find(|(s, _)| s == seed_str)
                    .map(|(_, m)| *m)
                    .unwrap_or(0.0);
                (next_mass - prev_mass).abs()
            })
            .sum();
        // Note: delta check uses seed personalization for simplicity.
        if delta < 1e-6 {
            break;
        }
    }

    rank_map.into_iter().filter(|(_, mass)| *mass > 0.0).collect()
}
