//! Embedding generation services for vector similarity search
//!
//! Provides both remote (Voyage AI) and local (fastembed) embedding generation.

pub mod local;
pub mod remote;

/// Active-memory count above which deterministic fallback embeddings can
/// materially reduce semantic retrieval quality.
pub const FALLBACK_EMBEDDING_WARNING_THRESHOLD: usize = 1_000;

/// Explain the retrieval risk of deterministic hash embeddings for large stores.
pub fn fallback_embedding_warning(memory_count: usize) -> Option<String> {
    (memory_count > FALLBACK_EMBEDDING_WARNING_THRESHOLD).then(|| {
        format!(
            "Semantic search is using deterministic hash fallback embeddings for {} active memories; retrieval quality may degrade at this scale. Build with `--features local-embeddings` and run `mnemosyne embed --all` to generate model-backed vectors.",
            memory_count
        )
    })
}

pub use local::LocalEmbeddingService;
pub use remote::{EmbeddingService, RemoteEmbeddingService, VOYAGE_EMBEDDING_DIM};

/// Environment variable holding an explicit Voyage embedding credential.
///
/// Voyage is a separate provider from the Anthropic LLM used for enrichment. An
/// LLM key is never a valid Voyage credential, so Voyage is only used when this
/// dedicated variable is present.
pub const VOYAGE_API_KEY_ENV: &str = "MNEMOSYNE_VOYAGE_API_KEY";
/// Optional Voyage model override (defaults to \"voyage-3-large\").
pub const VOYAGE_MODEL_ENV: &str = "MNEMOSYNE_VOYAGE_MODEL";
/// Optional Voyage base URL override (defaults to the Voyage AI endpoint).
pub const VOYAGE_BASE_URL_ENV: &str = "MNEMOSYNE_VOYAGE_BASE_URL";

/// Resolve the remote (Voyage) embedding credential, if explicitly configured.
///
/// Returns `None` unless `MNEMOSYNE_VOYAGE_API_KEY` is set to a non-empty
/// value. This deliberately decouples Voyage credentials from the Anthropic
/// LLM key: a configured `ANTHROPIC_API_KEY` never enables (or blocks) the
/// remote embedding path, which defaults to the local provider instead.
///
/// Returns `(api_key, model, base_url)` suitable for
/// `RemoteEmbeddingService::new`.
pub fn remote_embedding_config() -> Option<(String, Option<String>, Option<String>)> {
    let api_key = std::env::var(VOYAGE_API_KEY_ENV).ok()?;
    if api_key.trim().is_empty() {
        return None;
    }
    let model = std::env::var(VOYAGE_MODEL_ENV)
        .ok()
        .filter(|s| !s.trim().is_empty());
    let base_url = std::env::var(VOYAGE_BASE_URL_ENV)
        .ok()
        .filter(|s| !s.trim().is_empty());
    Some((api_key, model, base_url))
}

/// Calculate cosine similarity between two vectors
pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() {
        return 0.0;
    }

    let dot_product: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let magnitude_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let magnitude_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();

    if magnitude_a == 0.0 || magnitude_b == 0.0 {
        return 0.0;
    }

    dot_product / (magnitude_a * magnitude_b)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Env-var tests share the process environment and run on parallel threads;
    // serialize them so one test's set_var cannot leak into another's assertion.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn fallback_warning_starts_above_threshold() {
        assert!(fallback_embedding_warning(FALLBACK_EMBEDDING_WARNING_THRESHOLD).is_none());
        assert!(fallback_embedding_warning(FALLBACK_EMBEDDING_WARNING_THRESHOLD + 1).is_some());
    }

    #[test]
    fn test_cosine_similarity() {
        let vec1 = vec![1.0, 0.0, 0.0];
        let vec2 = vec![1.0, 0.0, 0.0];
        let vec3 = vec![0.0, 1.0, 0.0];

        // Same vectors
        assert!((cosine_similarity(&vec1, &vec2) - 1.0).abs() < 0.01);

        // Orthogonal vectors
        assert!((cosine_similarity(&vec1, &vec3) - 0.0).abs() < 0.01);
    }

    #[test]
    fn remote_embedding_config_requires_explicit_voyage_key() {
        let _guard = ENV_LOCK.lock().unwrap();
        // A configured Anthropic key must NOT enable the remote (Voyage) path.
        std::env::set_var("ANTHROPIC_API_KEY", "sk-ant-test");
        std::env::remove_var(VOYAGE_API_KEY_ENV);

        assert!(remote_embedding_config().is_none());

        std::env::remove_var("ANTHROPIC_API_KEY");
        std::env::remove_var(VOYAGE_API_KEY_ENV);
    }

    #[test]
    fn remote_embedding_config_ignores_empty_voyage_key() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::set_var(VOYAGE_API_KEY_ENV, "   ");
        assert!(remote_embedding_config().is_none());
        std::env::remove_var(VOYAGE_API_KEY_ENV);
    }

    #[test]
    fn remote_embedding_config_returns_overrides() {
        let _guard = ENV_LOCK.lock().unwrap();
        std::env::set_var(VOYAGE_API_KEY_ENV, "pa-voyage-test");
        std::env::set_var(VOYAGE_MODEL_ENV, "voyage-3.5");
        std::env::set_var(VOYAGE_BASE_URL_ENV, "https://voyage.example/v1");

        let cfg = remote_embedding_config().expect("voyage key set");
        assert_eq!(cfg.0, "pa-voyage-test");
        assert_eq!(cfg.1.as_deref(), Some("voyage-3.5"));
        assert_eq!(cfg.2.as_deref(), Some("https://voyage.example/v1"));

        std::env::remove_var(VOYAGE_API_KEY_ENV);
        std::env::remove_var(VOYAGE_MODEL_ENV);
        std::env::remove_var(VOYAGE_BASE_URL_ENV);
    }

    #[test]
    fn test_cosine_similarity_different_lengths() {
        let vec1 = vec![1.0, 2.0, 3.0];
        let vec2 = vec![1.0, 2.0];

        assert_eq!(cosine_similarity(&vec1, &vec2), 0.0);
    }

    #[test]
    fn test_cosine_similarity_zero_vectors() {
        let vec1 = vec![0.0, 0.0, 0.0];
        let vec2 = vec![1.0, 2.0, 3.0];

        assert_eq!(cosine_similarity(&vec1, &vec2), 0.0);
    }
}
