#!/bin/bash
set -euo pipefail

# Latency benchmark — all 4 pipelines
# Outputs METRIC name=value lines for autoresearch.

DB_TEMP=".auto/data/bench_$$"

cleanup() {
    rm -f "$DB_TEMP" "$DB_TEMP-journal" 2>/dev/null || true
}
trap cleanup EXIT

python3 - "$DB_TEMP" <<'PYEOF'
import sys, os, time, statistics
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "mnemosyne"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "integrations", "hermes-memory-provider"))

from storage import PythonMemoryStorage

db_path = sys.argv[1]

# --- Populate fresh DB with 100 memories ---
s = PythonMemoryStorage(db_path)
for i in range(100):
    s.remember(f"Memory number {i} about the project and its goals", namespace="agent:hermes", importance=(i % 10) + 1)
s.close()

# --- Search latency ---
s = PythonMemoryStorage(db_path)
search_times = []
queries = ["project", "goals", "memory", "number", "agent"]
for q in queries:
    for _ in range(20):
        t0 = time.perf_counter()
        s.recall(q, namespace="agent:hermes", max_results=10)
        t1 = time.perf_counter()
        search_times.append((t1 - t0) * 1000)

# --- Remember latency ---
remember_times = []
for i in range(50):
    t0 = time.perf_counter()
    s.remember(f"benchmark memory {i}", namespace="agent:hermes", importance=5)
    t1 = time.perf_counter()
    remember_times.append((t1 - t0) * 1000)
s.close()

def pct(data, p):
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])

search_p50 = pct(search_times, 50)
search_p99 = pct(search_times, 99)
remember_p50 = pct(remember_times, 50)
remember_p99 = pct(remember_times, 99)
all_latencies = search_times + remember_times
p99_all = pct(all_latencies, 99)

print(f"METRIC search_latency_p50={search_p50:.3f}")
print(f"METRIC search_latency_p99={search_p99:.3f}")
print(f"METRIC remember_latency_p50={remember_p50:.3f}")
print(f"METRIC remember_latency_p99={remember_p99:.3f}")
print(f"METRIC p99_latency_ms={p99_all:.3f}")
PYEOF