//! Query-term coverage rescoring for retrieval fusion.
//!
//! Hybrid recall unions an OR-expansion FTS channel with vector similarity.
//! Both channels can rank a record highly on a SINGLE lucky token ("name",
//! "store", "current") while the record that covers most of the query's
//! content terms sits lower. Coverage rescoring multiplies each fused score
//! by a factor that grows with the fraction of distinct query terms the
//! candidate actually covers (content + summary + keywords + tags), so
//! one-token OR matches lose ties against multi-term coverage.

use crate::types::{MemoryNote, SearchResult};

/// Default multiplier applied when a candidate covers none of the query's
/// content terms. Below 1.0 so pure noise matches are demoted.
pub const COVERAGE_FLOOR: f32 = 0.6;

/// Default multiplier applied when a candidate covers every distinct query
/// content term. Above 1.0 so comprehensive matches are promoted.
pub const COVERAGE_CEILING: f32 = 1.4;

/// Default multiplier for candidates whose fact has been superseded by a
/// newer record (`superseded_by` set). Corrections must outrank the stale
/// facts they replaced even when the stale text is lexically rich; history
/// questions can still surface them when nothing current matches.
pub const SUPERSEDED_PENALTY: f32 = 0.35;

/// Conversational/meta words that appear constantly in personal-agent
/// questions ("I already told you", "you remember right?", "what is it
/// called again?") and match episodic chatter far more often than the fact
/// being asked for. Filtered from coverage counting; FTS query building has
/// its own stopword list.
const QUERY_META_STOPS: &[&str] = &[
    "again", "already", "asked", "ask", "back", "before", "call", "come", "correct", "did", "ever",
    "exactly", "get", "goes", "going", "keep", "kept", "know", "like", "mean", "meant", "multiple",
    "new", "now", "off", "old", "once", "one", "please", "really", "remember", "right", "said",
    "say", "saying", "see", "set", "still", "stuff", "sure", "tell", "telling", "told", "thing",
    "things", "think", "time", "times", "today", "way", "well", "wondered", "yes", "yeah", "yet",
];

/// Function words: pure grammar, never discriminative for coverage.
const QUERY_FUNCTION_STOPS: &[&str] = &[
    "a", "an", "and", "are", "at", "be", "been", "being", "but", "by", "can", "could", "did", "do",
    "does", "for", "from", "had", "has", "have", "he", "her", "here", "hers", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "may", "me", "might", "more", "most", "must", "my",
    "of", "on", "or", "our", "ours", "she", "should", "so", "some", "than", "that", "the", "their",
    "theirs", "them", "then", "there", "these", "they", "this", "those", "to", "was", "we", "were",
    "what", "when", "where", "which", "who", "whom", "why", "will", "with", "would", "you", "your",
    "yours",
];

fn is_stop_word(candidate: &str) -> bool {
    QUERY_META_STOPS.contains(&candidate) || QUERY_FUNCTION_STOPS.contains(&candidate)
}

/// Canonicalize ordinary inflection/synonym variants for coverage. This is
/// deliberately narrow: it joins only forms that name the same retrieval
/// concept and does not attempt open-ended language understanding.
fn canonical_alias(term: &str) -> &str {
    match term {
        "serv" | "serve" | "served" | "hosting" | "hosted" => "host",
        _ => term,
    }
}

/// Light normalization approximating porter-style stems for coverage
/// counting. Conservative by design: it only strips common inflections so
/// "passwords" counts for "password" without conflating unrelated words.
fn normalize_term(raw: &str) -> String {
    let trimmed: String = raw
        .trim_matches(|c: char| !c.is_alphanumeric())
        .to_ascii_lowercase();
    let stripped = trimmed.strip_suffix("'s").unwrap_or(&trimmed);
    // Stop filtering happens BEFORE stemming so plural/singular forms of
    // stop words ("yes" -> "ye") cannot leak through as content terms.
    if stripped.len() < 2 || is_stop_word(stripped) {
        return String::new();
    }
    let mut stem = stripped.to_string();
    if stem.ends_with("ies") && stem.len() > 4 {
        stem.truncate(stem.len() - 3);
        stem.push('y');
    } else if (stem.ends_with("ses")
        || stem.ends_with("xes")
        || stem.ends_with("zes")
        || stem.ends_with("ches")
        || stem.ends_with("shes"))
        && stem.len() > 4
    {
        stem.truncate(stem.len() - 2);
    } else if stem.ends_with('s') && !stem.ends_with("ss") && !stem.ends_with("us") {
        stem.truncate(stem.len() - 1);
    }
    if stem.ends_with("ing") && stem.len() > 5 {
        stem.truncate(stem.len() - 3);
    } else if stem.ends_with("ed") && stem.len() > 4 {
        stem.truncate(stem.len() - 2);
    }
    // A stem can collapse onto another stop word; re-check so stems stay
    // content-only.
    if is_stop_word(&stem) {
        return String::new();
    }
    stem
}

/// Distinct, normalized content terms from a natural-language query.
/// Hyphen/slash compounds ("hermes-dashboard", "and/or") split into
/// subtokens so compound entity names still count as term coverage.
fn query_content_terms(query: &str) -> Vec<String> {
    let mut seen = std::collections::HashSet::new();
    let mut terms = Vec::new();
    let expanded = query.replace(['-', '/', '_'], " ");
    for raw in expanded.split_whitespace() {
        let term = normalize_term(raw);
        if term.is_empty() || term.len() < 2 {
            continue;
        }
        let term = canonical_alias(&term).to_string();
        if seen.insert(term.clone()) {
            terms.push(term);
        }
    }
    terms
}

/// Fraction (0.0-1.0) of the query's distinct content terms covered by a
/// memory's searchable text. Returns 1.0 for queries with no usable terms
/// so rescore becomes a no-op instead of uniformly demoting everything.
pub fn coverage_ratio(terms: &[String], memory: &MemoryNote) -> f32 {
    if terms.is_empty() {
        return 1.0;
    }
    let mut doc_tokens = std::collections::HashSet::new();
    let mut push_text = |text: &str| {
        // Compound tokens (hermes-dashboard, 2019/2020) contribute their
        // parts so entity names match query subterms.
        let expanded = text.replace(['-', '/', '_'], " ");
        for raw in expanded.split_whitespace() {
            let token = normalize_term(raw);
            if !token.is_empty() {
                doc_tokens.insert(canonical_alias(&token).to_string());
            }
        }
    };
    push_text(&memory.content);
    push_text(&memory.summary);
    push_text(&memory.context);
    for keyword in &memory.keywords {
        push_text(keyword);
    }
    for tag in &memory.tags {
        push_text(tag);
    }
    let covered = terms.iter().filter(|t| doc_tokens.contains(*t)).count();
    covered as f32 / terms.len() as f32
}

/// Multiplicative rescore factor for a coverage ratio: linear interpolation
/// from [`COVERAGE_FLOOR`] at ratio 0 to [`COVERAGE_CEILING`] at ratio 1.
pub fn coverage_factor(ratio: f32) -> f32 {
    COVERAGE_FLOOR + (COVERAGE_CEILING - COVERAGE_FLOOR) * ratio.clamp(0.0, 1.0)
}

/// Rescore fused search results in place: supersession demotion plus
/// query-term coverage weighting.
pub fn apply_coverage_rescore(query: &str, results: &mut [SearchResult]) {
    for result in results.iter_mut() {
        if result.memory.superseded_by.is_some() {
            result.score *= SUPERSEDED_PENALTY;
        }
    }
    let terms = query_content_terms(query);
    if terms.is_empty() {
        return;
    }
    for result in results.iter_mut() {
        let ratio = coverage_ratio(&terms, &result.memory);
        result.score *= coverage_factor(ratio);
    }
}

/// Supersession-only demotion for pipelines that score candidates without
/// a query context (e.g. storage-layer fusion before coverage runs).
pub fn apply_supersession_penalty(results: &mut [SearchResult]) {
    for result in results.iter_mut() {
        if result.memory.superseded_by.is_some() {
            result.score *= SUPERSEDED_PENALTY;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note(content: &str) -> MemoryNote {
        MemoryNote {
            id: crate::types::MemoryId::new(),
            namespace: crate::types::Namespace::Global,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            content: content.to_string(),
            summary: content.chars().take(50).collect(),
            keywords: vec![],
            tags: vec![],
            context: String::new(),
            memory_type: crate::types::MemoryType::Insight,
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

    #[test]
    fn normalizes_plurals_and_meta_words() {
        assert_eq!(normalize_term("Passwords,"), "password");
        assert_eq!(normalize_term("Puppies"), "puppy");
        assert_eq!(normalize_term("yes?"), "");
        let terms = query_content_terms("You remember where I keep passwords, yes?");
        // Function/meta words (you/remember/where/keep/yes/i) all filtered.
        assert_eq!(terms, vec!["password".to_string()]);
    }

    #[test]
    fn coverage_joins_host_and_serve_variants() {
        let terms = query_content_terms("how is the site hosted?");
        let target = note("Personal site is served through Cloudflare Pages.");
        assert!(coverage_ratio(&terms, &target) > 0.0);
    }

    #[test]
    fn coverage_prefers_multi_term_match_over_single_token() {
        let terms = query_content_terms("hotel booking reference for the Porto trip?");
        let good =
            note("Porto hotel is Casa do Fado, booking reference CF-2841, check-in after 15:00.");
        let lucky =
            note("The pnpm store directory lives at ~/.local/share/pnpm/store on this machine.");
        let good_ratio = coverage_ratio(&terms, &good);
        let lucky_ratio = coverage_ratio(&terms, &lucky);
        assert!(good_ratio > lucky_ratio * 2.0);
        assert!(coverage_factor(good_ratio) > coverage_factor(lucky_ratio));
    }

    #[test]
    fn superseded_records_are_demoted() {
        let mut results = vec![SearchResult {
            memory: note("Old fact text with dashboard deploy details."),
            score: 0.9,
            match_reason: "keyword".to_string(),
        }];
        results[0].memory.superseded_by = Some(crate::types::MemoryId::new());
        apply_coverage_rescore("dashboard deployment?", &mut results);
        assert!(results[0].score < 0.9 * 0.5);
    }

    #[test]
    fn empty_query_terms_leave_scores_untouched() {
        let mut results = vec![SearchResult {
            memory: note("Anything at all."),
            score: 0.7,
            match_reason: "keyword".to_string(),
        }];
        apply_coverage_rescore("??? !!! ... ,,,", &mut results);
        assert!((results[0].score - 0.7).abs() < 1e-6);
    }
}
