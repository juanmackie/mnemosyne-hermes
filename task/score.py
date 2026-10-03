#!/usr/bin/env python3
"""Dream-RSI scorer for the mnemosyne-hermes recall store.

Runs the repo's OWN harness (bench/measure.sh) against the candidate's code and
writes eval/score.json. Lives at <repo>/task/, which is OUTSIDE the seed
workspace (src/), so candidates can never edit the file that scores them.

The harness is copied pristine into a per-run temp dir and the attempt
workspace is symlinked in as its src/, because measure.sh derives its root from
$0 and does sys.path.insert(0, ROOT + "/src"). Every run gets its own temp dir
so concurrent workers never share a corpus DB.

Objective (write throughput): the candidate's cold-write cost, lower is better.

Three gates, because write throughput is trivially cheatable -- dropping
synchronous, or removing checkpointing, both score ~5x better and both are
regressions the benchmark's correctness asserts cannot see:

  1. recall semantics -- the harness's own assert_ok (7 rare-term / 2 phrase /
     0 miss / 100 saturation).
  2. durability contract -- journal_mode=wal and synchronous=NORMAL on the
     connection the candidate actually opens.
  3. WAL bound -- peak WAL across the run stays under WAL_PEAK_LIMIT. Removing
     checkpointing is a 5x win on the metric and a 200 MiB WAL on disk; this is
     the gate that catches it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "bench", "measure.sh")
FIELD = os.environ.get("MNEMOSYNE_METRIC", "write_5000_p50_ms")

# The corpus itself is 3000 rows; shipped policy keeps the WAL in single-digit
# MiB. 64 MiB is ~16x the shipped steady state and still far under the 200 MiB
# an unbounded WAL reaches, so this fails cheaters without failing honest work.
WAL_PEAK_LIMIT = int(os.environ.get("MNEMOSYNE_WAL_PEAK_LIMIT", 64 * 1024 * 1024))


def _wal_bytes(root):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith("-wal"):
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    return total


def watch_wal(root, stop, peak):
    """Sample total -wal size under `root` until `stop` is set."""
    while not stop.is_set():
        peak[0] = max(peak[0], _wal_bytes(root))
        stop.wait(0.02)
    peak[0] = max(peak[0], _wal_bytes(root))


def check_contract(workspace, scratch):
    """Confirm the durability pragmas the store ships with are intact."""
    saved = list(sys.path)
    sys.path.insert(0, workspace)
    try:
        from mnemosyne_lite.storage import PythonMemoryStorage
        store = PythonMemoryStorage(os.path.join(scratch, "contract.db"))
        try:
            conn = store._conn()
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            sync = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - any failure here is a contract failure
        return f"contract_check_error: {exc!r}"
    finally:
        sys.path[:] = saved
    if mode != "wal":
        return f"journal_mode={mode!r}, expected 'wal'"
    if sync == 0:
        return "synchronous=OFF: writes are no longer durable across a power loss"
    return None


def fail(fail_class, detail):
    out = os.path.join(os.getcwd(), "eval", "score.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"score": None, "fail_class": fail_class, "detail": detail}, fh, indent=2)
    print(f"FAIL {fail_class}: {detail}")
    sys.exit(1)


def main():
    if not os.path.isfile(HARNESS):
        fail("harness_missing", HARNESS)
    workspace = os.getcwd()
    if not os.path.isdir(os.path.join(workspace, "mnemosyne_lite")):
        fail("no_candidate", f"expected mnemosyne_lite/ under {workspace}")

    with tempfile.TemporaryDirectory(prefix="mnemosyne-score-") as tmp:
        os.makedirs(os.path.join(tmp, "bench"))
        os.makedirs(os.path.join(tmp, "bench", "data"))
        shutil.copy2(HARNESS, os.path.join(tmp, "bench", "measure.sh"))
        os.symlink(workspace, os.path.join(tmp, "src"))

        stop, peak = threading.Event(), [0]
        watcher = threading.Thread(target=watch_wal, args=(tmp, stop, peak), daemon=True)
        watcher.start()
        try:
            proc = subprocess.run(
                ["bash", "bench/measure.sh"],
                cwd=tmp, capture_output=True, text=True, timeout=1800,
            )
        finally:
            stop.set()
            watcher.join(timeout=5)
        out = proc.stdout + proc.stderr

    metrics = dict(re.findall(r"^METRIC\s+(\w+)=(\S+)", out, re.M))
    if proc.returncode != 0 and not metrics:
        fail("harness_error", out.strip()[-800:])
    if metrics.get("assert_ok") != "1":
        fail("assert_failed", out.strip()[-800:])
    if FIELD not in metrics:
        fail("no_metric", f"{FIELD} not emitted")

    try:
        score = float(metrics[FIELD])
    except ValueError:
        fail("bad_metric", f"{FIELD}={metrics[FIELD]!r}")
    if score <= 0:
        fail("bad_metric", f"{FIELD}={score}")

    # Gate 2: durability contract.
    with tempfile.TemporaryDirectory(prefix="mnemosyne-contract-") as scratch:
        broken = check_contract(workspace, scratch)
    if broken:
        fail("contract_broken", broken)

    # Gate 3: peak WAL across the run.
    if peak[0] > WAL_PEAK_LIMIT:
        fail("wal_unbounded",
             f"peak WAL {peak[0]/1048576:.1f} MiB > limit "
             f"{WAL_PEAK_LIMIT/1048576:.0f} MiB (checkpoint policy regressed?)")

    out_path = os.path.join(workspace, "eval", "score.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(
            {"score": score, "fail_class": "ok", "metric": FIELD,
             "assert_ok": True, "wal_peak_bytes": peak[0],
             "context": {k: metrics[k] for k in
                         ("write_5000_p50_ms", "search_p50_ms", "search_p99_ms",
                          "cold_init_p50_ms", "store_5000_bytes") if k in metrics}},
            fh, indent=2,
        )
    print(f"score {score} ms ({FIELD}), assert_ok=1")


if __name__ == "__main__":
    main()