"""Price commit durability: how long does one fsync'd single-row insert cost?

Times 20 single-row commits against a copy of the eval DB under different
PRAGMA settings, using the retrieval_traces table the recall path writes to.
"""
import shutil
import sqlite3
import tempfile
import time
import pathlib

tmp = pathlib.Path(tempfile.mkdtemp())
db = tmp / "s.db"
shutil.copy2(".auto/data/template.db", db)
print("journal_mode at rest:", end=" ")
c = sqlite3.connect(db)
print(c.execute("pragma journal_mode").fetchone()[0],
      "| synchronous:", c.execute("pragma synchronous").fetchone()[0])
c.close()

for journal, sync in [("wal", 2), ("wal", 1), ("delete", 2), ("memory", 0)]:
    db2 = tmp / f"{journal}-{sync}.db"
    shutil.copy2(".auto/data/template.db", db2)
    conn = sqlite3.connect(db2)
    conn.execute(f"pragma journal_mode={journal}")
    conn.execute(f"pragma synchronous={sync}")
    rows = [(f"probe-{journal}-{sync}-{i}", "h" * 64, i) for i in range(20)]
    started = time.perf_counter()
    for rid, digest, n in rows:
        conn.execute(
            "INSERT OR REPLACE INTO retrieval_traces (id, query_hash, namespace,"
            " rewritten_terms, keyword_candidates, vector_candidates, graph_candidates,"
            " effective_weights, fallback_reasons, result_ids, created_at)"
            " VALUES (?, ?, ?, '[]', ?, 0, 0, '{}', '[]', '[]', ?)",
            (rid, digest, "probe", n, 1700000000))
        conn.commit()
    elapsed = (time.perf_counter() - started) * 1000 / len(rows)
    count = conn.execute("select count(*) from retrieval_traces").fetchone()[0]
    print("journal=%-7s synchronous=%d -> %.2f ms per commit (traces=%d)"
          % (journal, sync, elapsed, count))
    conn.close()
