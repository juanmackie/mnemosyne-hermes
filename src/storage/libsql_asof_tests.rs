//! Temporal validity on the point-in-time recall path.
//!
//! `keyword_search_as_of` backs `MemoryManager::recall_as_of`. Every other read
//! path compares `expires_at` to `now`; this one must compare it to the read
//! instant, otherwise a historical read resurrects facts that had already expired
//! at that moment, and a read of the present leaks tomorrow's expired rows.

#[cfg(test)]
mod as_of_expiry_tests {
    use crate::storage::StorageBackend;
    use crate::types::{MemoryClass, MemoryId, MemoryNote, MemoryType, Namespace};
    use crate::{ConnectionMode, LibsqlStorage};
    use chrono::{TimeDelta, Utc};
    use tempfile::TempDir;

    async fn storage() -> (LibsqlStorage, TempDir) {
        let temp_dir = TempDir::new().unwrap();
        let db_path = temp_dir.path().join("test_asof_expiry.db");
        let store = LibsqlStorage::new_with_validation(
            ConnectionMode::Local(db_path.to_str().unwrap().to_string()),
            true,
        )
        .await
        .expect("test storage");
        (store, temp_dir)
    }

    /// A memory created `created_hours_ago` in the past whose validity window
    /// ends at `expires_hours_ago` (negative = already lapsed in wall-clock time).
    fn note(content: &str, created_hours_ago: i64, expires_hours_ago: i64) -> MemoryNote {
        let now = Utc::now();
        let created_at = now - TimeDelta::hours(created_hours_ago);
        MemoryNote {
            id: MemoryId::new(),
            namespace: Namespace::Global,
            created_at,
            updated_at: created_at,
            content: content.to_string(),
            summary: content.to_string(),
            keywords: vec!["duty".to_string()],
            tags: vec!["duty".to_string()],
            context: String::new(),
            memory_type: MemoryType::Insight,
            memory_class: MemoryClass::Knowledge,
            provenance: None,
            importance: 7,
            confidence: 0.9,
            links: vec![],
            related_files: vec![],
            related_entities: vec![],
            access_count: 0,
            last_accessed_at: now,
            expires_at: Some(now + TimeDelta::hours(-expires_hours_ago)),
            is_archived: false,
            superseded_by: None,
            embedding: None,
            embedding_model: String::new(),
        }
    }

    async fn titles(store: &LibsqlStorage, as_of: chrono::DateTime<Utc>) -> Vec<String> {
        store
            .keyword_search_as_of("duty phone", &Namespace::Global, as_of, 10)
            .await
            .unwrap()
            .into_iter()
            .map(|r| r.memory.content)
            .collect()
    }

    #[tokio::test]
    async fn expired_rows_leave_the_as_of_lane_at_and_after_expiry() {
        let (store, _tmp) = storage().await;
        store
            .store_memory(&note(
                "Tonight I am holding the out-of-hours data duty phone",
                0,
                -24,
            ))
            .await
            .unwrap();
        let now = Utc::now();

        // Inside the validity window: recalled.
        assert_eq!(titles(&store, now).await.len(), 1);
        // After it lapsed: gone from the same query.
        assert!(titles(&store, now + TimeDelta::days(2)).await.is_empty());
    }

    #[tokio::test]
    async fn historical_read_from_before_expiry_still_sees_the_row() {
        let (store, _tmp) = storage().await;
        // Written three hours ago, valid until one hour ago.
        store
            .store_memory(&note(
                "I am locum cover at the Estarreja clinic on duty",
                3,
                1,
            ))
            .await
            .unwrap();

        // A read pinned two hours ago sits inside the window, so it keeps the row.
        assert_eq!(
            titles(&store, Utc::now() - TimeDelta::hours(2)).await.len(),
            1
        );
        // The same query now, after the window closed, does not.
        assert!(titles(&store, Utc::now()).await.is_empty());
    }
}
