#!/bin/bash
set -euo pipefail

# Search-speed benchmark — recall() latency on a deterministic 3000-row corpus.
# Outputs METRIC name=value lines. Exits nonzero if recall semantics break.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
DB_TEMP="$ROOT/.auto/data/bench_search_$$"

cleanup() {
    rm -f "$DB_TEMP" "$DB_TEMP-journal" "$DB_TEMP-wal" "$DB_TEMP-shm" 2>/dev/null || true
}
trap cleanup EXIT

python3 - "$DB_TEMP" "$ROOT" <<'PYEOF'
import sys, os, time, random
sys.path.insert(0, os.path.join(sys.argv[2], "src"))

from lib.storage import PythonMemoryStorage

db_path = sys.argv[1]
random.seed(42)

WORDS = ("project alpha milestone review deploy staging cache index query router "
         "agent memory session token stream buffer ledger atlas beacon cipher drift "
         "ember flux grove harbor ivy junction kernel lumen meadow north oak pine "
         "quartz ridge stone timber umbra vale wheat xenon yarn zenith amber brass "
         "copper delta echo frost glacier helm inlet jade kite lotus magnet nest "
         "onyx prism quilt raven solar terra unity vapor willow pixel frame dock "
         "lane port signal tower bridge cloud rain snow wind leaf root branch seed "
         "bloom field river ocean sand cliff cave forge anvil hammer nail plank "
         "beam arch dome spire stair hall gate wall floor roof window door key "
         "lock chain rope sail mast oar helm deck hull anchor buoy tide wave foam "
         "shell pearl coral reef shark whale dolphin seal otter crab shrimp squid "
         "octopus turtle frog toad newt snake lizard gecko eagle hawk falcon owl "
         "robin sparrow finch wren lark dove crow raven goose duck swan heron "
         "crane stork ibis flamingo pelican gull tern puffin penguin albatross").split()

def sentence():
    return " ".join(random.choice(WORDS) for _ in range(random.randint(8, 14))).capitalize() + "."

# --- Populate fresh DB ---
s = PythonMemoryStorage(db_path)
planted_xyl, planted_phrase = set(random.sample(range(3000), 7)), set(random.sample(range(3000), 2)) - set()
for i in range(3000):
    text = f"Memory {i}: " + sentence() + " " + sentence()
    if i in planted_xyl:
        text += " xylophone"
    if i in planted_phrase:
        text += " midnight lantern protocol"
    ns = "agent:hermes" if random.random() < 0.8 else random.choice(["session:x", "agent:other"])
    s.remember(text, namespace=ns, importance=random.randint(1, 10))

# --- Correctness assertions (recall semantics must not change) ---
checks = [
    (len(s.recall("xylophone", max_results=100)), 7, "rare term count"),
    (len(s.recall("midnight lantern protocol", max_results=100)), 2, "phrase count"),
    (len(s.recall("zzzqqqnomatch", max_results=100)), 0, "no-match count"),
]
common_n = len(s.recall("memory", max_results=100))
assert common_n == 100, f"common term should saturate the 100-result cap: {common_n}"
for got, want, name in checks:
    assert got == want, f"{name}: got {got}, want {want}"
print("METRIC assert_ok=1")

# --- Timed recall workload ---
QUERIES = [
    ("memory", "agent:hermes", 10, None),                 # common + namespace
    ("xylophone", None, 10, None),                        # rare, no namespace
    ("midnight lantern protocol", None, 10, None),        # phrase
    ("zzzqqqnomatch", None, 10, None),                    # full-scan miss
    ("project", "agent:hermes", 10, 5),                   # common + importance floor
    ("memory", None, 50, None),                           # wide, large limit
]
REPS = 30
all_times, per_query = [], {}
for qi, (q, ns, lim, imp) in enumerate(QUERIES):
    for _ in range(5):  # warmup
        s.recall(q, namespace=ns, max_results=lim, min_importance=imp)
    ts = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        s.recall(q, namespace=ns, max_results=lim, min_importance=imp)
        ts.append((time.perf_counter() - t0) * 1000)
    all_times.extend(ts)
    ts.sort()
    per_query[qi] = (ts[len(ts) // 2], ts[int((len(ts) - 1) * 0.99)])
s.close()

def pct(data, p):
    d = sorted(data)
    k = (len(d) - 1) * p / 100.0
    f = int(k)
    return d[f] if f + 1 >= len(d) else d[f] + (k - f) * (d[f + 1] - d[f])

p50, p99 = pct(all_times, 50), pct(all_times, 99)
print(f"METRIC search_p50_ms={p50:.4f}")
print(f"METRIC search_p99_ms={p99:.4f}")
for qi, (m50, m99) in per_query.items():
    print(f"METRIC q{qi}_p50_ms={m50:.4f}")
    print(f"METRIC q{qi}_p99_ms={m99:.4f}")
PYEOF
