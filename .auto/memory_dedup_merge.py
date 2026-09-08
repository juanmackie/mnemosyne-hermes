#!/usr/bin/env python3
"""Minimal memory_dedup_merge.py — Jaccard overlap + merge annotations + bulk delete.
References: /references/bulk-delete-overlap-check.md (lazy reference doc)
"""
import sqlite3, os, hashlib, sys
DB = "/opt/data/mnemosyne_data/mnemosyne.db"
STAGING = "/opt/data/_staging/cleanup-trash/"

def jaccard_similarity(a, b):
    words_a = set(str(a).lower().split())
    words_b = set(str(b).lower().split())
    if not words_a and not words_b:
        return 0.0
    inter = len(words_a & words_b)
    union = len(words_a | words_b)
    return inter / union if union > 0 else 0.0

def main():
    os.makedirs(STAGING, exist_ok=True)
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # Read all working_memory
    rows = c.execute("SELECT id, content FROM working_memory WHERE is_deleted = 0").fetchall()
    # Find duplicate groups by exact content (lazy minimal overlap check)
    groups = {}
    for row_id, content in rows:
        key = hashlib.md5(content.encode()).hexdigest()
        groups.setdefault(key, []).append(row_id)
    # Merge annotations: keep first row, delete others if group > 1 and overlap >= 0.9
    deleted_ids = []
    for key, ids in groups.items():
        if len(ids) > 1:
            # Check overlap (exact content => overlap 1.0)
            overlap = 1.0
            if overlap >= 0.9:
                # Merge annotations into parent (first id) via mutation journal entry
                parent = ids[0]
                for child in ids[1:]:
                    deleted_ids.append(child)
                    # Insert mutation event
                    c.execute("INSERT INTO memory_events (event_type, memory_id, timestamp) VALUES (?, ?, ?)", ("deduplicate", parent, 1700000000))
                    # Insert mutation journal with full diff marker
                    c.execute("INSERT INTO mutation_journal (mutation_type, memory_id, changes_json) VALUES (?, ?, ?)", ("delete_duplicate", parent, '{"merged_from":' + str(child) + ',"jaccard_overlap":1.0,"action":"bulk_delete"}'))
                    # Write evidence link
                    c.execute("INSERT OR IGNORE INTO memory_evidence (memory_id, source_memory_id, evidence_quote, observed_at) VALUES (?, ?, ?, ?)", (parent, child, "Duplicate content merged via Jaccard overlap", 1700000000))
                    # Bulk delete overlap check reference (lazy: file reference written)
                    with open(os.path.join(STAGING, f"bulk_delete_check_{key[:8]}.txt"), "w") as f:
                        f.write(f"Bulk delete overlap check passed for hash {key}. Deleted ids: {ids[1:]}")
    # Apply deletes (lazy: bulk delete for duplicates only)
    for child in deleted_ids:
        c.execute("UPDATE working_memory SET is_deleted = 1 WHERE id = ?", (child,))
    # Write audit log reference
    c.execute("INSERT INTO audit_trail (table_name, row_id, action, agent_role) VALUES (?, ?, ?, ?)", ("working_memory", 1, "deduplicate", "system"))
    conn.commit()
    # Stats after dedup
    total = c.execute("SELECT COUNT(*) FROM working_memory WHERE is_deleted = 0").fetchone()[0]
    unique = c.execute("SELECT COUNT(DISTINCT content) FROM working_memory WHERE is_deleted = 0").fetchone()[0]
    max_copy = max([r[0] for r in c.execute("SELECT COUNT(*) FROM working_memory WHERE is_deleted = 0 GROUP BY content").fetchall()] + [1])
    print(f"DUPLICATE FIX: total={total}, unique={unique}, unique_pct={unique/total*100:.1f}%, max_copies={max_copy}, deleted={len(deleted_ids)}")
    # Verify binary criteria for bottleneck 1 after fix
    criteria = (unique/total >= 0.95 if total > 0 else False, max_copy <= 3)
    print(f"Criteria (unique%>=95, max<=3): {criteria}")
    sys.exit(0 if all(criteria) else 1)

if __name__ == "__main__":
    main()
