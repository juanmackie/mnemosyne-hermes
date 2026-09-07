"""One-shot timeline probe: per-stage debug timestamps for a single recall call.

RUST_LOG=debug makes the storage path log stage boundaries with sub-second
timestamps; the deltas between those lines price each stage of a real query
without adding permanent instrumentation.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import pathlib

db = ".auto/data/template.db"
dataset = sys.argv[1] if len(sys.argv) > 1 else ".auto/eval_heldout_a.jsonl"
item = json.loads(next(l for l in open(dataset) if l.strip()))
tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
shutil.copy2(db, tmp)
env = dict(os.environ)
env["MNEMOSYNE_EMBEDDING_MODEL"] = "bge-small-en-v1.5"
env["RUST_LOG"] = "debug"
proc = subprocess.run(["target/release/mnemosyne", "--db-path", str(tmp), "recall",
                       "--query", item["query"], "--namespace", "project:personal-agent-eval",
                       "--limit", "5", "--format", "json"],
                      capture_output=True, text=True, env=env)
events = []
for line in proc.stderr.splitlines():
    match = re.match(r"\S*?(\d{2}):(\d{2}):(\d{2})\.(\d{3})", line)
    if match:
        h, m, s, ms = (int(x) for x in match.groups())
        events.append((h * 3600 + m * 60 + s + ms / 1000.0,
                       re.sub(r"^\S+\s+", "", line)[:96]))
print("query:", item["query"])
if not events:
    print(proc.stderr[-900:])
    raise SystemExit(1)
first = events[0][0]
previous = first
for when, message in events:
    print("+%7.1f (%6.1f) %s" % ((when - first) * 1000, (when - previous) * 1000, message))
    previous = when
