#!/usr/bin/env python3
"""Generalization probe: fresh paraphrases + phrasings never used in the
eval sets or during development. Run once per kept change as an audit;
never optimize directly against this file."""
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

# Must match the corpus ingestion model or query embeddings are incompatible.
os.environ.setdefault("MNEMOSYNE_EMBEDDING_MODEL", "bge-small-en-v1.5")
BIN = "target/release/mnemosyne"
DB = ".auto/data/template.db"
NS = "project:personal-agent-eval"

# (query, [relevant substrings]) — authored fresh, different surface forms
PROBES = [
    ("hey what email should people reach me at", ["alex@riveralabs.dev"]),
    ("which address do I give out for mail these days", ["alex@riveralabs.dev"]),
    ("remind me who handles my dns records", ["Cloudflare dashboard zone"]),
    ("where are my domain's nameservers pointed", ["Cloudflare dashboard zone"]),
    ("what am I supposed to install deps with in the dashboard repo", ["pnpm is the package manager there now"]),
    ("do I run npm install or something else for hermes-dashboard", ["pnpm is the package manager there now"]),
    ("how old is my sister and when is her birthday", ["Maya Rivera was born on March 14"]),
    ("when do I need a gift for my sibling", ["Maya Rivera was born on March 14"]),
    ("what computer do I carry around", ["ThinkPad X1 Carbon Gen 12"]),
    ("my main machine specs", ["ThinkPad X1 Carbon Gen 12"]),
    ("graphics card for the ai stuff", ["RTX 4080 Super"]),
    ("what renders my llms locally", ["RTX 4080 Super"]),
    ("am i vegetarian or vegan", ["vegetarian", "Vegetarian"]),
    ("can I order fish for me", ["shellfish allergy", "vegetarian"]),
    ("hotel name and confirmation for porto", ["Casa do Fado"]),
    ("where am I sleeping during the porto trip", ["Casa do Fado"]),
    ("next dentist visit date", ["Dentist appointment October 3"]),
    ("dental cleaning scheduled?", ["Dentist appointment October 3"]),
    ("what shell do I type into", ["zsh with starship"]),
    ("which terminal app is configured", ["Ghostty"]),
    ("who hosts my source code now", ["gitlab.com/arivera"]),
    ("github or gitlab for my repos", ["gitlab.com/arivera"]),
    ("database engine on the home box", ["Postgres 16 runs on the home server"]),
    ("what sql server runs at home and on which port", ["port 5433"]),
    ("cache thing after the license drama", ["replaced Redis with Valkey 8"]),
    ("valkey or redis at home?", ["replaced Redis with Valkey 8"]),
    ("phone os lockdown tweaks", ["GrapheneOS"]),
    ("android privacy rom on my phone", ["Pixel 9 Pro on GrapheneOS", "moved from iPhone 13 to Pixel 9 Pro"]),
    ("job title on paper nowadays", ["platform engineer since June"]),
    ("what do I put as my role on forms", ["platform engineer since June"]),
    ("file storage after dropping dropbox", ["Synology Drive replaced Dropbox"]),
    ("nas sync app name", ["Synology Drive replaced Dropbox"]),
]

def relevant(result, targets):
    text = (str(result.get("summary", "")) + " " + str(result.get("content", ""))).lower()
    return any(t.strip().lower() in text for t in targets if t.strip())

def main():
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for i, (query, targets) in enumerate(PROBES):
            qdb = f"{td}/q{i}.db"
            shutil.copy2(DB, qdb)
            p = subprocess.run(
                [BIN, "--db-path", qdb, "recall", "--query", query,
                 "--namespace", NS, "--limit", "5", "--format", "json"],
                capture_output=True, text=True)
            if p.returncode:
                print(f"FAIL running recall: {query!r}", file=sys.stderr)
                continue
            res = json.loads(p.stdout)["results"]
            rank = None
            for r_i, r in enumerate(res, 1):
                if relevant(r, targets):
                    rank = r_i
                    break
            rows.append((query, rank))
    n = len(rows)
    mrr = sum(1.0 / r if r else 0.0 for _, r in rows) / max(n, 1)
    hit1 = sum(r == 1 for _, r in rows) / max(n, 1)
    hit5 = sum(r is not None and r <= 5 for _, r in rows) / max(n, 1)
    print(f"probe_queries={n} hit1={hit1:.4f} hit5={hit5:.4f} mrr={mrr:.4f}")
    for q, r in rows:
        if r != 1:
            print(f"  MISS rank={r} {q!r}")

if __name__ == "__main__":
    main()
