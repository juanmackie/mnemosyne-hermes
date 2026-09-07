"""Price the recursive graph walk's edge join.

`graph_traverse_with_limit` joins memory_links with `source_id = gw.id OR
target_id = gw.id`, a shape SQLite cannot satisfy from an index. This probe builds
a 2k-memory / 12k-link database and compares that join against indexed shapes.
"""
import hashlib
import shutil
import sqlite3
import tempfile
import time
import pathlib

tmp = pathlib.Path(tempfile.mkdtemp()) / "g.db"
shutil.copy2(".auto/data/template.db", tmp)
conn = sqlite3.connect(tmp)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")


def schema(table):
    return {r[1]: (r[2].upper(), bool(r[3]), r[4]) for r in conn.execute(f"pragma table_info({table})")}


def typed(type_name, fallback):
    for prefix, value in (("TEXT", "x"), ("TIME", "2026-01-01T00:00:00"), ("INTE", 0), ("REAL", 0.5)):
        if type_name.startswith(prefix):
            return value
    return fallback


mem_schema = schema("memories")
link_schema = schema("memory_links")
ids = ["%04d-memory" % i for i in range(2000)]


def insert(table, tbl_schema, overrides_per_row):
    """Fill every NOT NULL column that has no default; let SQLite default the rest."""
    wanted = set(overrides_per_row[0])
    columns = [name for name, (_t, notnull, dflt) in tbl_schema.items()
               if (notnull and dflt is None) or name in wanted]
    rows = []
    for overrides in overrides_per_row:
        rows.append(tuple(overrides[name] if name in overrides
                          else typed(tbl_schema[name][0], 0)
                          for name in columns))
    try:
        conn.executemany(f"INSERT INTO {table} ({', '.join(columns)}) "
                         f"VALUES ({', '.join('?' * len(columns))})", rows)
    except Exception as error:
        print(f"{table}: insert failed -> {error}")


insert("memories", mem_schema, [{"id": mid, "namespace": "project:graph-probe",
                                 "content": "content %d" % i, "importance": 5,
                                 "memory_type": "entity", "confidence": 0.9,
                                 "embedding_model": "probe"} for i, mid in enumerate(ids)])
insert("memory_links", link_schema,
       [{"source_id": ids[i], "target_id": ids[(i * 7 + step) % len(ids)],
         "link_type": "references", "strength": 0.5 + (step % 5) * 0.1}
        for i in range(len(ids)) for step in range(1, 7)])
conn.commit()
print("memories=%d links=%d" % (conn.execute("select count(*) from memories").fetchone()[0],
                                conn.execute("select count(*) from memory_links").fetchone()[0]))

TAIL = """
SELECT DISTINCT m.id, gw.depth FROM memories m JOIN graph_walk gw ON m.id = gw.memory_id
WHERE gw.depth > 0 AND m.is_archived = 0 ORDER BY gw.depth, m.importance DESC LIMIT 50
"""


def or_join_sql(count):
    head = "SELECT id, 0 FROM memories WHERE id IN (%s)" % ",".join("?" * count)
    return ("WITH RECURSIVE graph_walk(memory_id, depth) AS (%s UNION SELECT CASE "
            "WHEN ml.source_id = gw.memory_id THEN ml.target_id ELSE ml.source_id END, "
            "gw.depth + 1 FROM graph_walk gw JOIN memory_links ml ON "
            "(ml.source_id = gw.memory_id OR ml.target_id = gw.memory_id) "
            "WHERE gw.depth < 2 LIMIT 150) %s" % (head, TAIL))


def outbound_sql(count):
    return or_join_sql(count).replace(
        "ON (ml.source_id = gw.memory_id OR ml.target_id = gw.memory_id)",
        "ON ml.source_id = gw.memory_id")


def edges_sql(count):
    head = "SELECT id, 0 FROM memories WHERE id IN (%s)" % ",".join("?" * count)
    return ("""WITH RECURSIVE
 edges(from_id, to_id) AS (
   SELECT source_id, target_id FROM memory_links
   UNION ALL
   SELECT target_id, source_id FROM memory_links
 ),
 graph_walk(memory_id, depth) AS (%s
   UNION
   SELECT e.to_id, gw.depth + 1 FROM graph_walk gw JOIN edges e ON e.from_id = gw.memory_id
   WHERE gw.depth < 2 LIMIT 150
)""" % head) + TAIL


def run(label, builder, count, plan=False):
    sql, params = builder(count), ids[:count]
    started = time.perf_counter()
    cursor = conn.execute(sql, params)
    first = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    out = cursor.fetchall()
    walked = (time.perf_counter() - started) * 1000
    digest = hashlib.md5(repr(sorted(out)).encode()).hexdigest()[:8]
    print("%-14s first=%7.2fms step=%7.2fms rows=%3d hash=%s"
          % (label, first, walked, len(out), digest))
    if plan:
        for line in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall():
            print("      |", line[3][:104])


for count in (200, 100, 50):
    run("or-join/%d" % count, or_join_sql, count, plan=(count == 200))
    run("outbound/%d" % count, outbound_sql, count)
    run("edges-cte/%d" % count, edges_sql, count, plan=(count == 200))
conn.close()
