-- Outcome-aware reasoning memories.
-- Strategies and failure guardrails remain ordinary knowledge memories, while
-- this metadata keeps them out of generic factual recall and preserves the
-- completed-task outcome that produced them.

CREATE TABLE IF NOT EXISTS reasoning_experiences (
    id TEXT PRIMARY KEY NOT NULL,
    namespace TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    task_summary TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failure', 'uncertain')),
    verifier TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    outcome_evidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reasoning_memory_items (
    memory_id TEXT PRIMARY KEY NOT NULL,
    experience_id TEXT NOT NULL,
    lesson_kind TEXT NOT NULL CHECK(lesson_kind IN ('strategy', 'guardrail')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    applicability TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (experience_id) REFERENCES reasoning_experiences(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reasoning_experiences_namespace_time
    ON reasoning_experiences(namespace, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reasoning_experiences_outcome
    ON reasoning_experiences(outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reasoning_items_experience
    ON reasoning_memory_items(experience_id);

-- Purging a trajectory must not leave its distilled children as unclassified
-- factual memories. The trigger runs when the source FK cascades the
-- experience row and removes both the item rows and their metadata.
CREATE TRIGGER IF NOT EXISTS reasoning_experience_cleanup
BEFORE DELETE ON reasoning_experiences
BEGIN
    DELETE FROM audit_log
    WHERE memory_id IN (
        SELECT memory_id FROM reasoning_memory_items WHERE experience_id = OLD.id
    );
    DELETE FROM memories
    WHERE id IN (
        SELECT memory_id FROM reasoning_memory_items WHERE experience_id = OLD.id
    );
END;
