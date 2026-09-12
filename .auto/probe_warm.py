import json, shutil, subprocess, sys, tempfile, time, pathlib, statistics

db = ".auto/data/template.db"
dataset = sys.argv[1] if len(sys.argv) > 1 else ".auto/eval_heldout_a.jsonl"
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8
tmp = pathlib.Path(tempfile.mkdtemp()) / "m.db"
shutil.copy2(db, tmp)
items = [json.loads(l) for l in open(dataset) if l.strip()][:limit]
p = subprocess.Popen(["target/release/mnemosyne", "--db-path", str(tmp), "mcp"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, bufsize=1)
p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}) + "\n")
p.stdin.flush()
start_time = time.time()
while True:
    line = p.stdout.readline()
    if not line:
        if time.time() - start_time > 30:
            print("timeout: subprocess died after 30s")
        else:
            print("died:", p.stderr.read()[-400:])
        raise SystemExit(1)
    try:
        if json.loads(line).get("id") == 1:
            break
    except json.JSONDecodeError:
        pass
lat, hits = [], 0
for i, it in enumerate(items):
    rid = i + 2
    started = time.perf_counter()
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "tools/call", "id": rid,
        "params": {"name": "mnemosyne_recall", "arguments": {
            "query": it["query"], "namespace": "project:personal-agent-eval",
            "max_results": 5, "compact": False}}}) + "\n")
    p.stdin.flush()
    while True:
        line = p.stdout.readline()
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == rid:
            break
    lat.append((time.perf_counter() - started) * 1000)
    payload = json.loads(resp["result"]["content"][0]["text"])
    results = payload.get("results", [])
    parts = []
    for item in results:
        mem = item.get("memory", item)
        parts.extend(str(mem.get(key, "")) for key in ("id", "summary", "content"))
    text = " ".join(parts).lower()
    hit = any(t.strip().lower() in text for t in it["relevant"])
    hits += hit
    print("%-44s %7.1fms results=%2d rel=%s" % (it["query"][:42], lat[-1], len(results), hit))
p.stdin.close()
p.kill()
print("hits=%d/%d p50=%.1fms max=%.1fms" % (hits, len(items), statistics.median(lat), max(lat)))
