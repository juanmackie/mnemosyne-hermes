"""Sanity: does the app put the database into WAL mode, and does recall still work?"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import pathlib

tmp = pathlib.Path(tempfile.mkdtemp())
db = tmp / "w.db"
shutil.copy2(".auto/data/template.db", db)
before = sqlite3.connect(db).execute("pragma journal_mode").fetchone()[0]
item = json.loads(next(line for line in open(".auto/eval_heldout_a.jsonl") if line.strip()))
proc = subprocess.run(["target/release/mnemosyne", "--db-path", str(db), "recall",
                       "--query", item["query"], "--namespace", "project:personal-agent-eval",
                       "--limit", "3", "--format", "json"], capture_output=True, text=True,
                      env={"PATH": "/usr/bin:/bin", "HOME": "/root",
                           "MNEMOSYNE_EMBEDDING_MODEL": "bge-small-en-v1.5"})
payload = json.loads(proc.stdout)
after = sqlite3.connect(db).execute("pragma journal_mode").fetchone()[0]
synchronous = sqlite3.connect(db).execute("pragma synchronous").fetchone()[0]
print("rc=%d results=%d journal_mode: %s -> %s (synchronous reads back as %d; "
      "it is a per-connection setting)" % (proc.returncode, payload["count"], before, after,
                                           synchronous))
print("sidecars:", sorted(p.name for p in tmp.iterdir() if p.name != "w.db"))
