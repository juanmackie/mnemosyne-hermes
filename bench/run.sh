#!/bin/bash
# Run one experiment: measure, log to bench/log.jsonl, print the delta vs best.
#   ./bench/run.sh <keep|discard|crash|checks_failed> "<description>" ["<asi json>"]
#   ./bench/run.sh          # measure WITHOUT logging (dry run)
#
# Environment: AUTO_MEASURE (harness path), AUTO_LOG (ledger path),
# AUTO_PRIMARY (metric name), AUTO_DIRECTION ("lower" or "higher").
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

STATUS="${1:-}"
DESC="${2:-}"
ASI="${3:-}"
DRY=0
[ -z "$STATUS" ] && { STATUS="dry"; DRY=1; }

OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT
"${AUTO_MEASURE:-./bench/measure.sh}" >"$OUT" 2>&1
RC=$?
# RC is captured above, before anything else runs: reading $? after a later
# command records that command's status instead, which used to let a crashed
# measurement be logged as "keep". The raw output is kept for post-mortems.
cp "$OUT" bench/last_measure.txt
grep -E "^(METRIC|note)" "$OUT" || true
if [ "$RC" -ne 0 ] && [ "$STATUS" = "keep" ]; then
    echo "STATUS: cannot keep a failed run"
    exit 1
fi

STATUS_ARG="$STATUS"
[ "$DRY" -eq 1 ] && STATUS_ARG="dry"

COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
DIRTY=$(git status --porcelain -- src bench 2>/dev/null | wc -l)

STATUS="$STATUS_ARG" DESC="$DESC" ASI="$ASI" COMMIT="$COMMIT" DIRTY="$DIRTY" RC="$RC" \
  AUTO_DIRECTION="${AUTO_DIRECTION:-lower}" \
  LOG="${AUTO_LOG:-bench/log.jsonl}" python3 - "$OUT" <<'PY'
import json, os, sys, time

raw = open(sys.argv[1]).read()
metrics = {}
for line in raw.splitlines():
    if line.startswith("METRIC "):
        try:
            k, v = line[7:].split("=")
            metrics[k.strip()] = float(v)
        except ValueError:
            pass

log_path = os.environ["LOG"]
history = []
if os.path.exists(log_path):
    with open(log_path) as f:
        history = [json.loads(l) for l in f if l.strip()]

PRIMARY = os.environ.get("AUTO_PRIMARY", "search_p50_ms")
DIRECTION = os.environ["AUTO_DIRECTION"]  # "lower" or "higher"
cur = metrics.get(PRIMARY)
kept = [h["metrics"].get(PRIMARY) for h in history
        if h.get("status") == "keep" and h.get("metrics", {}).get(PRIMARY)]
baseline = [h["metrics"][PRIMARY] for h in history if h.get("metrics", {}).get(PRIMARY)]
best = (min(kept) if DIRECTION == "lower" else max(kept)) if kept else None

# Noise floor: spread of consecutive re-measurements of the same state.
deltas = [abs(baseline[i] - baseline[i - 1]) / baseline[i - 1]
          for i in range(1, len(baseline)) if baseline[i - 1]]
noise = sorted(deltas)[len(deltas) // 2] if len(deltas) >= 3 else None

entry = {
    "iteration": len(history) + 1,
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "status": os.environ["STATUS"],
    "description": os.environ["DESC"],
    "primary": cur,
    "metrics": metrics,
    "asi": json.loads(os.environ["ASI"]) if os.environ["ASI"] else {},
    "commit": os.environ["COMMIT"],
    "dirty_files": int(os.environ["DIRTY"]),
    "measure_rc": int(os.environ["RC"]),
}
if os.environ["STATUS"] != "dry":
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

print("---")
if cur is None:
    print("PRIMARY: n/a (measurement failed)")
else:
    unit = " ms" if PRIMARY.endswith("_ms") else ""
    line = "PRIMARY %s = %.4f%s" % (PRIMARY, cur, unit)
    if best is not None:
        line += "  | best kept %.4f (%+.1f%%)" % (best, (cur - best) / best * 100.0)
    elif len(baseline) > 1:
        first = baseline[0]
        line += "  | baseline %.4f (%+.1f%%)" % (first, (cur - first) / first * 100.0)
    print(line)
if noise is not None:
    print("noise floor (median |delta|): %.1f%%" % (noise * 100.0))
secondary = {k: v for k, v in metrics.items() if k != PRIMARY}
if secondary:
    print("secondary:", {k: round(v, 4) for k, v in sorted(secondary.items())})
PY
