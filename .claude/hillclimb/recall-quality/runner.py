#!/usr/bin/env python3
"""Runner for the recall-quality eval: grades `mnemosyne-lite recall` on a fixed corpus.

The unit under test is the real shipped CLI, invoked as a subprocess against a
real store — not an import of `recall()` — so retry logic, argument parsing, JSON
emission and the FTS index all sit on the measured path. Setup (building the
corpus DB) uses the storage API directly; that is fixture work, not measurement.

Contract (see report/SCHEMA.md and the build-eval guide):
  <variant>/results.jsonl            one row per (case, rep), written as it completes
  <variant>/traces/<id>_rep<k>.json the full exchange, for the transcript view
  <variant>/errors.jsonl             attempts that produced no scorable output
Resume is idempotent at the (case, rep) key. A failed attempt never occupies that
key, so a re-run retries it instead of skipping it forever.

    python3 runner.py --probe                 # oracle + null, before any paid claim
    python3 runner.py --reps 2 --variant baseline
    python3 runner.py --reps 2 --only test    # the held-out split, once
"""

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
SRC = REPO / "src"
DATA = HERE / "data"
HARNESS_SHA = DATA / "harness.sha"
CASES = HERE / "cases.jsonl"
CORPUS = HERE / "corpus.jsonl"

HARNESS_FILES = ["runner.py", "build_cases.py", "cases.jsonl", "corpus.jsonl"]

# Grading conventions, mirrored in metrics.md. The polarity differs by case kind,
# which is why tags[1] is "positive"/"negative" — never read a mean without the
# split: a negative case scoring 1.0 means "correctly returned nothing".
METRICS = ["hit_at_5", "hit_at_1", "mrr", "precision_at_5"]

sys.path.insert(0, str(SRC))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def harness_digest():
    h = hashlib.sha256()
    for name in HARNESS_FILES:
        h.update(name.encode())
        h.update(hashlib.sha256((HERE / name).read_bytes()).digest())
    return h.hexdigest()


def check_harness(approve=False):
    digest = harness_digest()
    if HARNESS_SHA.exists():
        recorded = HARNESS_SHA.read_text().split()[0]
        if recorded != digest:
            if not approve:
                print(
                    f"harness changed since it was recorded\n  recorded {recorded}\n"
                    f"  now      {digest}\nrefusing to run: pass --approve-harness to "
                    "re-record it (the operator's call, not the runner's)",
                    file=sys.stderr,
                )
                sys.exit(2)
    elif approve:
        pass
    HARNESS_SHA.parent.mkdir(parents=True, exist_ok=True)
    HARNESS_SHA.write_text(f"{digest}  {' '.join(HARNESS_FILES)}\n")


def source_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_store(variant, force=False):
    """Build the corpus DB for `variant`, if missing or stale.

    One DB per variant, not one shared DB: a variant may change the schema or
    migrations, and scoring it against a database built by *baseline* code would
    measure the wrong artifact. Each variant's DB also makes its own build
    auditable, which is the larger term in this eval's noise.
    """
    from mnemosyne_lite.storage import PythonMemoryStorage

    db_path = DATA / variant / "corpus.db"
    rows = read_jsonl(CORPUS)
    sha = source_sha(CORPUS)
    if db_path.exists() and not force:
        recorded = dict(read_meta(db_path))
        if recorded.get("corpus_sha") == sha and recorded.get("corpus_rows") == str(len(rows)):
            return db_path
        print(f"corpus changed ({recorded.get('corpus_sha')} -> {sha}); rebuilding",
              file=sys.stderr)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    store = PythonMemoryStorage(str(db_path))
    ages = []
    try:
        now = time.time()
        for row in rows:
            written = store.remember(
                row["content"],
                namespace=row.get("namespace", "default"),
                importance=row.get("importance", 5),
            )
            age = float(row.get("age_days", 0))
            if age:
                ages.append((now - age * 86400.0, written["id"]))
    finally:
        store.close()

    # age_days is fixture control, not a product behaviour: without it every row
    # shares a timestamp and the "correction" cases (a recent fact superseding
    # an older one) cannot test anything. Written on our own connection so the
    # runner never touches a private attribute of the store.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany("UPDATE memories SET created_at = ? WHERE id = ?", ages)
        conn.execute("CREATE TABLE IF NOT EXISTS eval_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT OR REPLACE INTO eval_meta (key, value) VALUES (?, ?)",
            [("corpus_sha", sha), ("corpus_rows", str(len(rows)))],
        )
        conn.commit()
    finally:
        conn.close()
    print(f"built {db_path} from {len(rows)} memories", file=sys.stderr)
    return db_path


def read_meta(db_path):
    """Provenance of the built corpus, or {} when there is none."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='eval_meta'"
        ).fetchone()
        if not table:
            return {}
        return dict(conn.execute("SELECT key, value FROM eval_meta").fetchall())
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def run_recall(db_path, query, k, timeout_s):
    """Call the real CLI. Returns (rows, elapsed_s). Raises RecallFailure."""
    cmd = [
        sys.executable, "-m", "mnemosyne_lite.cli", "recall",
        "--db-path", str(db_path),
        "--query", query,
        "--max-results", str(k),
        "--format", "json",
    ]
    env = dict(os.environ, PYTHONPATH=str(SRC))
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RecallFailure("timeout", f"recall exceeded {timeout_s}s", None, None)
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise RecallFailure("harness_error",
                            f"exit {proc.returncode}: {proc.stderr.strip()[:400]}",
                            None, elapsed)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RecallFailure("bad_output", f"unparseable stdout: {exc}", None, elapsed)
    if not isinstance(payload, list):
        raise RecallFailure("bad_output", f"expected a JSON array, got {type(payload).__name__}",
                            None, elapsed)
    return payload, elapsed


class RecallFailure(Exception):
    def __init__(self, kind, message, cmd=None, elapsed=None):
        super().__init__(message)
        self.kind = kind
        self.cmd = cmd
        self.elapsed = elapsed


def is_relevant(row, relevant):
    content = (row.get("content") or "").lower()
    return any(rel.lower() in content for rel in relevant)


def grade(case, rows, k):
    """Score one case. See metrics.md for the polarity convention."""
    if case["expect"] == "empty":
        correct = len(rows) == 0
        return {
            "hit_at_5": 1.0 if correct else 0.0,
            "hit_at_1": 1.0 if correct else 0.0,
            "mrr": 1.0 if correct else 0.0,
            "precision_at_5": 1.0 if correct else 0.0,
        }, None, len(rows)

    rank = None
    for i, row in enumerate(rows, start=1):
        if is_relevant(row, case["relevant"]):
            rank = i
            break
    top = rows[:k]
    hit5 = 1.0 if rank is not None and rank <= k else 0.0
    hit1 = 1.0 if rank == 1 else 0.0
    mrr = (1.0 / rank) if rank is not None else 0.0
    # "no junk": every returned row is relevant. BM25 can surface off-topic rows
    # alongside the right one; a pure hit@k cannot see that. Vacuously true when
    # nothing is returned — returning nothing is not returning something wrong,
    # and scoring it 0 would conflate "abstained" with "surfaced junk".
    precision = 1.0 if all(is_relevant(r, case["relevant"]) for r in top) else 0.0
    return {
        "hit_at_5": hit5, "hit_at_1": hit1, "mrr": mrr, "precision_at_5": precision,
    }, rank, len(rows)


def trace_for(case, rep, rows, scores, rank, cmd_str):
    verdict = (f"relevant at rank {rank}" if rank else
               ("correctly returned nothing" if case["expect"] == "empty"
                else "no relevant row in the result set"))
    return [
        {"role": "system",
         "content": "Retrieval eval. Graded programmatically by substring containment "
                    "of the case's expected text in the returned memory content. "
                    "No model judges this case."},
        {"role": "user", "content": case["prompt"]},
        {"role": "tool_call", "name": "mnemosyne-lite recall", "content": cmd_str},
        {"role": "tool_result", "content": json.dumps(rows, indent=2, default=str)},
        {"role": "assistant",
         "content": f"{verdict}. scores: {json.dumps(scores)}\n"
                    f"expected: {case['relevant'] or '[] (nothing)'}"},
    ]


def wilson(successes, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (p, max(0.0, center - half), min(1.0, center + half))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", default="baseline",
                    help="baseline, or v1/v2/... (dir name must match exactly)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--timeout-s", type=float, default=30.0,
                    help="hard per-case wall-clock ceiling")
    ap.add_argument("--only", choices=["train", "test", "all"], default="all")
    ap.add_argument("--probe", action="store_true",
                    help="oracle + null check through the runner and grader, then exit")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the corpus DB")
    ap.add_argument("--approve-harness", action="store_true",
                    help="re-record the harness digest after an intentional change")
    args = ap.parse_args()

    check_harness(args.approve_harness)
    cases = read_jsonl(CASES)
    if args.only != "all":
        cases = [c for c in cases if c["split"] == args.only]
    if args.probe:
        # its own database, so probing never disturbs a variant's corpus
        return probe(cases, ensure_store("_probe"), args)
    db = ensure_store(args.variant, args.rebuild)

    out_dir = HERE / args.variant
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    errors_path = out_dir / "errors.jsonl"

    done = set()
    if results_path.exists():
        for row in read_jsonl(results_path):
            done.add((row["prompt_id"], row.get("rep", 0)))
    print(f"{len(done)} (case, rep) rows already present; resuming", file=sys.stderr)

    results_f = results_path.open("a")
    errors_f = errors_path.open("a")
    try:
        for case in cases:
            for rep in range(args.reps):
                if (case["id"], rep) in done:
                    continue
                cmd_str = (f"mnemosyne-lite recall --db-path {db} "
                           f"--query {json.dumps(case['prompt'])} "
                           f"--max-results {args.k} --format json")
                try:
                    rows, elapsed = run_recall(db, case["prompt"], args.k, args.timeout_s)
                    scores, rank, n_rows = grade(case, rows, args.k)
                except RecallFailure as exc:
                    errors_f.write(json.dumps({
                        "prompt_id": case["id"], "rep": rep, "failure_class": exc.kind,
                        "detail": str(exc), "attempts": 1, "tags": case["tags"],
                    }) + "\n")
                    errors_f.flush()
                    print(f"[{exc.kind}] {case['id']}: {exc}", file=sys.stderr)
                    continue

                row = {
                    "prompt_id": case["id"],
                    "prompt": case["prompt"],
                    "tags": case["tags"],
                    "split": case["split"],
                    "status": "ok",
                    "stop_reason": "complete",
                    "grade": scores,
                    "model": f"lite-recall/{os.environ.get('MNEMOSYNE_REV', 'worktree')}",
                    "recall_ms": round(elapsed * 1000, 3),
                    "results_returned": n_rows,
                    "first_relevant_rank": rank,
                    "meta": {"k": args.k, "expect": case["expect"],
                             "relevant": case["relevant"]},
                }
                results_f.write(json.dumps(row) + "\n")
                results_f.flush()
                trace_path = out_dir / "traces" / f"{case['id']}_rep{rep}.json"
                trace_path.write_text(json.dumps(
                    trace_for(case, rep, rows, scores, rank, cmd_str), indent=2))
    finally:
        results_f.close()
        errors_f.close()

    return summarise(args.variant, cases, args.k)


def probe(cases, db, args):
    """Oracle and null, through the runner and the grader.

    Oracle: a query that is the memory's own content must retrieve it (~100%).
    Null:  a negative case must retrieve nothing (~0% rows returned).
    A grader that cannot do both is not measuring anything.
    """
    from mnemosyne_lite.storage import PythonMemoryStorage

    store = PythonMemoryStorage(str(db))
    try:
        memories = store.list_memories(limit=3)
    finally:
        store.close()
    failures = []

    for mem in memories:
        content = mem["content"]
        try:
            rows, _ = run_recall(db, content, args.k, args.timeout_s)
        except RecallFailure as exc:
            failures.append(f"oracle could not run for {content[:40]!r}: {exc}")
            continue
        if not rows or not is_relevant(rows[0], [content]):
            failures.append(
                f"oracle miss: querying a memory's own content did not retrieve it "
                f"(got {len(rows)} rows) for {content[:50]!r}")

    negatives = [c for c in cases if c["expect"] == "empty"]
    for case in negatives:
        rows, _ = run_recall(db, case["prompt"], args.k, args.timeout_s)
        if rows:
            failures.append(f"null probe {case['id']}: expected nothing, got {len(rows)} rows")

    positives = sum(1 for c in cases if c["expect"] == "hit")
    print(f"oracle: {len(memories)} memories queried by their own content")
    print(f"null:   {len(negatives)} negative cases expected to return nothing")
    print(f"cases:  {positives} positive, {len(negatives)} negative")
    if failures:
        print(f"\nPROBE FAILED ({len(failures)}):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nprobe ok: oracle retrieves, null abstains")
    return 0


def summarise(variant, cases, k):
    out_dir = HERE / variant
    results = read_jsonl(out_dir / "results.jsonl")
    errors = read_jsonl(out_dir / "errors.jsonl") if (out_dir / "errors.jsonl").exists() else []
    ok = [r for r in results if r.get("status") == "ok"]

    by_case = {}
    for r in ok:
        by_case.setdefault(r["prompt_id"], []).append(r)

    def mean_for(case_id, metric):
        reps = by_case.get(case_id, [])
        return sum(r["grade"][metric] for r in reps) / len(reps) if reps else None

    print(f"\n=== {variant}: {len(cases)} cases, {len(by_case)} scored, "
          f"{len(errors)} failed attempts, k={k} ===")

    def report_group(label, ids):
        ids = [i for i in ids if i in by_case]
        if not ids:
            return
        cells = []
        for metric in METRICS:
            vals = [mean_for(i, metric) for i in ids]
            vals = [v for v in vals if v is not None]
            cells.append(f"{metric}={sum(vals) / len(vals):.3f}" if vals else f"{metric}=n/a")
        hit5 = [mean_for(i, "hit_at_5") for i in ids]
        hit5 = [v for v in hit5 if v is not None]
        p, lo, hi = wilson(sum(1 for v in hit5 if v >= 0.5), len(hit5))
        reps = max(len(by_case[i]) for i in ids)
        print(f"  {label:22s} n={len(ids):3d}  " + "  ".join(cells))
        print(f"  {'':22s} hit@5 pass {sum(1 for v in hit5 if v>=0.5)}/{len(hit5)}"
              f" = {p:.3f} [95% CI {lo:.3f}-{hi:.3f}]  reps/case={reps}")

    pos = [c["id"] for c in cases if c["expect"] == "hit"]
    report_group("POSITIVE (train)", [c["id"] for c in cases if c["split"] == "train" and c["expect"] == "hit"])
    report_group("POSITIVE (test)", [c["id"] for c in cases if c["split"] == "test" and c["expect"] == "hit"])
    # Negatives sit in both splits: the held-out ones are the honest final
    # specificity number, the train ones are the guardrail a round can see.
    report_group("NEGATIVE (train)", [c["id"] for c in cases if c["split"] == "train" and c["expect"] == "empty"])
    report_group("NEGATIVE (test)", [c["id"] for c in cases if c["split"] == "test" and c["expect"] == "empty"])

    categories = {}
    for c in cases:
        categories.setdefault(c["tags"][0], []).append(c["id"])
    for cat in sorted(categories):
        report_group(f"category: {cat}", categories[cat])

    lat = sorted(r["recall_ms"] for r in ok)
    if lat:
        print(f"\n  recall_ms (CLI wall clock, process-inclusive): "
              f"p50={lat[len(lat)//2]:.1f}  p99={lat[int(len(lat)*0.99)]:.1f}  n={len(lat)}")
    if errors:
        by_kind = {}
        for e in errors:
            by_kind[e["failure_class"]] = by_kind.get(e["failure_class"], 0) + 1
        print(f"  failed attempts (NOT scored): {by_kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())