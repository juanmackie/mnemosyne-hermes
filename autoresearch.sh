#!/bin/bash
# Autoresearch runner: fast syntax pre-check, then the canonical search
# benchmark, aggregated across AR_RUNS invocations (median per metric).
# Prints METRIC name=number lines (the experiment-loop contract).
#
# Single-run noise at the microsecond scale measured ±8-20% on p50 (runs
# 1-3), larger than most real deltas — so one invocation is not a usable
# experiment. AR_RUNS (default 3, odd) medians suppress transient spikes.
set -euo pipefail

SELF="${0//\\//}"
SCRIPT_DIR="${SELF%/*}"
[ "$SCRIPT_DIR" = "$SELF" ] && SCRIPT_DIR="."
case "$SCRIPT_DIR" in
    /*) : ;;
    *) SCRIPT_DIR="$PWD/$SCRIPT_DIR" ;;
esac
ROOT="$SCRIPT_DIR"

# Same pinned-python resolution as .auto/measure.sh (minimal-PATH bash safe).
if [ -n "${MEASURE_PYTHON:-}" ]; then
    PYBIN="$MEASURE_PYTHON"
elif [ -x "$HOME/.local/bin/python3.exe" ]; then
    PYBIN="$HOME/.local/bin/python3.exe"
else
    PYBIN="$(command -v python3 || command -v python || true)"
fi
[ -n "$PYBIN" ] || { echo "no python on PATH" >&2; exit 1; }

# Fast pre-check (<1s): a syntax error must not cost a benchmark run.
"$PYBIN" -m py_compile "$ROOT/src/lib/storage.py"

RUNS="${AR_RUNS:-3}"
case "$RUNS" in
    ''|*[!0-9]*) echo "AR_RUNS must be a positive integer" >&2; exit 1 ;;
esac
[ "$RUNS" -ge 1 ] || { echo "AR_RUNS must be >= 1" >&2; exit 1; }
# Even counts pick the lower median implicitly; force odd so the median is a
# real sample.
[ $((RUNS % 2)) -eq 1 ] || RUNS=$((RUNS + 1))

AR_OUT=""
i=1
while [ "$i" -le "$RUNS" ]; do
    # Each invocation must pass its own asserts; set -e aborts on failure
    # (logged as a crash by the loop).
    out="$(bash "$ROOT/.auto/measure.sh")"
    AR_OUT="${AR_OUT}${out}
"
    i=$((i + 1))
done
export AR_OUT

"$PYBIN" - <<'PYEOF'
import os, statistics, sys

values = {}
order = []
for line in os.environ["AR_OUT"].splitlines():
    line = line.strip()
    if not line.startswith("METRIC "):
        continue
    name, _, val = line[len("METRIC "):].partition("=")
    try:
        v = float(val)
    except ValueError:
        continue
    if name not in values:
        values[name] = []
        order.append(name)
    values[name].append(v)

if not order:
    print("no METRIC lines produced", file=sys.stderr)
    sys.exit(1)

for name in order:
    xs = values[name]
    med = statistics.median(xs)
    if name == "assert_ok":
        med = min(xs)  # every invocation must have asserted
    print(f"METRIC {name}={med:g}")
PYEOF
