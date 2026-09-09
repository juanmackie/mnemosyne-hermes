//! Temporal validity: the point-in-time recall path, and the consolidation sweep
//! that archives rows once their window has closed.
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
    use chrono::{Datelike, TimeDelta, Utc};
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
        note_with_expiry(content, created_hours_ago, Some(expires_hours_ago))
    }

    fn note_with_expiry(
        content: &str,
        created_hours_ago: i64,
        expires_hours_ago: Option<i64>,
    ) -> MemoryNote {
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
            expires_at: expires_hours_ago.map(|h| now + TimeDelta::hours(-h)),
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

    /// Assertions that need the raw table (archival flags, the journal) go straight
    /// to the file; the storage API deliberately hides both.
    async fn scalar(path: &std::path::Path, sql: &str) -> i64 {
        let database = libsql::Builder::new_local(path).build().await.unwrap();
        let connection = database.connect().unwrap();
        let mut rows = connection.query(sql, ()).await.unwrap();
        match rows.next().await.unwrap() {
            Some(row) => row.get::<i64>(0).unwrap(),
            None => -1,
        }
    }

    #[tokio::test]
    async fn sweep_archives_only_rows_whose_window_has_closed() {
        let (store, tmp) = storage().await;
        let db = tmp.path().join("test_asof_expiry.db");

        store
            .store_memory(&note(
                "Temporary locum cover at the Estarreja clinic",
                120,
                24,
            ))
            .await
            .unwrap();
        store
            .store_memory(&note_with_expiry(
                "Standing rule at the clinic: two staff on every duty rota",
                120,
                Some(-1),
            ))
            .await
            .unwrap();
        store
            .store_memory(&note_with_expiry(
                "The clinic opened in Estarreja in 2019",
                120,
                None,
            ))
            .await
            .unwrap();
        // A row that lapsed a few minutes ago, i.e. earlier on the SAME calendar day.
        // Timestamps are written as RFC 3339 ("...T...+00:00"), so a raw string compare
        // against datetime('now') ranks the whole row as future (the 'T' byte outranks
        // the space) and misses it; datetime() normalisation is what catches it. Skipped
        // near UTC midnight, where "a few minutes ago" is legitimately yesterday.
        let lapsed = Utc::now() - TimeDelta::minutes(10);
        let same_day = lapsed.date() == Utc::now().date();
        let mut lapsed_note =
            note_with_expiry("Cross-cover rota note at the Estarreja clinic", 5, None);
        lapsed_note.expires_at = Some(lapsed);
        store.store_memory(&lapsed_note).await.unwrap();

        let expected: usize = if same_day { 2 } else { 1 };
        assert_eq!(store.count_memories(None).await.unwrap(), 4);

        // A dry run counts without touching anything.
        assert_eq!(
            store.archive_expired("dry", true, None).await.unwrap(),
            expected
        );
        assert_eq!(
            scalar(&db, "SELECT COUNT(*) FROM memories WHERE is_archived = 1").await,
            0
        );
        assert_eq!(
            scalar(&db, "SELECT COUNT(*) FROM consolidation_tombstones").await,
            0
        );

        assert_eq!(
            store.archive_expired("sweep-1", false, None).await.unwrap(),
            expected
        );
        assert_eq!(store.count_memories(None).await.unwrap(), 4 - expected);
        assert_eq!(
            scalar(&db, "SELECT COUNT(*) FROM memories WHERE is_archived = 1").await,
            expected as i64
        );
        // The sweep is reversible only because it is journalled; assert that record.
        assert_eq!(
            scalar(
                &db,
                "SELECT COUNT(*) FROM consolidation_tombstones \
             WHERE run_id = 'sweep-1' AND reason = 'expired_row' \
               AND json_valid(payload)"
            )
            .await,
            expected as i64
        );
        // Nothing left to collect: still valid and eternal rows stay put.
        assert_eq!(
            store.archive_expired("sweep-2", false, None).await.unwrap(),
            0
        );
    }
}
