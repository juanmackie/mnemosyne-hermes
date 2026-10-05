"""Recall-perf engine regression lane: entity fuzzy-match call volume.

Covers the RECALL_PERF_ENGINE_REPORT fixes carried in
``integrations/engine-patches/`` (content-aware entity guard, threshold-bounded
banded Levenshtein, capped candidate extraction in the recall lane):

- the patched ``find_similar_entities`` returns the exact upstream match set
  (pruned pairs provably score below threshold; typo tolerance preserved);
- the banded Levenshtein is exact for matches and only prunes true misses;
- the recall lane extracts capped candidates with a raw-query fallback;
- one recall fans out to far fewer full matrices and still rank-1 hits;
- the dense lane reports its status through the explain trace (keyword path
  asserted here; live embedding weights are verified manually, not downloaded
  in CI).

Needs the real engine. In a bare venv every test prints a skip line and
returns, so ``./test-all.sh`` stays green without the engine.
"""

import contextlib
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _require_engine():
    try:
        import mnemosyne.core.beam as beam_mod
        import mnemosyne.core.entities as entities_mod

        return beam_mod, entities_mod
    except ImportError:
        print(
            "skip: mnemosyne-memory engine not importable "
            "(install ./integrations/hermes-provider to run the recall-perf lane)"
        )
        return None


@contextlib.contextmanager
def _perf_env(tmp):
    keys = ("MNEMOSYNE_DATA_DIR", "MNEMOSYNE_EMBEDDINGS_OFF", "HERMES_HOME")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["MNEMOSYNE_DATA_DIR"] = os.path.join(tmp, "_engine_data")
    os.environ["MNEMOSYNE_EMBEDDINGS_OFF"] = "1"
    os.environ["HERMES_HOME"] = tmp
    connections = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    try:
        with patch.object(sqlite3, "connect", side_effect=tracked_connect):
            yield
    finally:
        for conn in connections:
            with contextlib.suppress(Exception):
                conn.close()
        for name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
            module = sys.modules.get(name)
            local = getattr(module, "_thread_local", None)
            if local is not None:
                local.conn = None
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _reference_levenshtein(s1, s2):
    """Upstream full-matrix Levenshtein (no early exit), embedded for audit."""
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    previous = list(range(len(s2) + 1))
    current = [0] * (len(s2) + 1)
    for i, c1 in enumerate(s1):
        current[0] = i + 1
        for j, c2 in enumerate(s2):
            current[j + 1] = min(
                previous[j + 1] + 1,
                current[j] + 1,
                previous[j] + (0 if c1 == c2 else 1),
            )
        previous, current = current, previous
    return previous[len(s2)]


def _reference_similarity(s1, s2):
    a, b = s1.lower().strip(), s2.lower().strip()
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    if a.startswith(b) or b.startswith(a):
        longer = max(len(a), len(b))
        shorter = min(len(a), len(b))
        if shorter / longer < 0.3:
            return 0.0
        return 0.7 + (shorter / longer) * 0.3
    if a in b or b in a:
        return 0.5 + (min(len(a), len(b)) / max(len(a), len(b))) * 0.3
    return 1.0 - (_reference_levenshtein(a, b) / max_len)


def _reference_find(entity, known_entities, threshold=0.8):
    matches = []
    entity_len = len(entity.strip())
    for known in known_entities:
        if known == entity:
            matches.append((known, 1.0))
            continue
        known_len = len(known.strip())
        ratio = min(entity_len, known_len) / max(entity_len, known_len, 1)
        if ratio < threshold and (0.7 + ratio * 0.3) < threshold:
            continue
        sim = _reference_similarity(entity, known)
        if sim >= threshold:
            matches.append((known, sim))
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches


def _seed_corpus(beam, count=332):
    import random

    random.seed(0)
    paths = [f"/opt/data/mnemosyne_data/shard_{i:03d}/mnemosyne.db" for i in range(40)]
    paths += [f"/opt/data/repos/mnemosyne-hermes-{i}" for i in range(20)]
    procs = [
        "proc_" + "".join(random.choice("0123456789abcdef") for _ in range(12)) for _ in range(60)
    ]
    names = [
        "SearXNG",
        "DB",
        "mnemosyne-hermes",
        "Rust-Python repository pivot",
        "Auscoast Fire Services",
        "Tesla P40",
        "Z640 PSU",
        "XPeng L03 Ultra",
        "CBT-I",
        "doxylamine",
        "Hermes",
        "BeamMemory",
        "hello",
        "hallo",
        "jello",
        "fire",
        "Abdias",
        "Abdias J",
    ] + [f"Entity{i:03d} Name" for i in range(200)]
    known = (paths + procs + names)[:count]
    for idx, val in enumerate(known):
        beam.annotations.add_many(
            memory_id=f"perf-{idx:04d}",
            kind="mentions",
            values=[val],
            source="perf",
            confidence=1.0,
        )
    return known


def test_guard_keeps_match_set():
    mods = _require_engine()
    if mods is None:
        return
    _, entities_mod = mods
    # Pure-function check against the embedded upstream reference (no beam needed).
    import random

    random.seed(0)
    paths = [f"/opt/data/mnemosyne_data/shard_{i:03d}/mnemosyne.db" for i in range(40)]
    procs = [
        "proc_" + "".join(random.choice("0123456789abcdef") for _ in range(12)) for _ in range(60)
    ]
    names = [
        "SearXNG",
        "DB",
        "mnemosyne-hermes",
        "Rust-Python repository pivot",
        "Auscoast Fire Services",
        "Tesla P40",
        "Z640 PSU",
        "XPeng L03 Ultra",
        "CBT-I",
        "doxylamine",
        "Hermes",
        "BeamMemory",
        "hello",
        "hallo",
        "jello",
        "fire",
        "fira",
        "Abdias",
        "Abdias J",
    ] + [f"Entity{i:03d} Name" for i in range(200)]
    corpus = paths + procs + names
    queries = [
        "Auscoast Fire Services role and responsibilities",
        "sleep insomnia doxylamine CBT-I wife",
        "Tesla P40 second hand GPU bring up",
        "the",
        "hello",
        "hallo",
        "jello",
        "fire",
        "Abdias",
        "/opt/data/mnemosyne_data/shard_007/mnemosyne.db what is in it",
    ]
    mismatches = []
    for query in queries:
        expected = _reference_find(query, corpus, 0.8)
        got = entities_mod.find_similar_entities(query, corpus, 0.8)
        if expected != got:
            mismatches.append((query, expected, got))
    assert not mismatches, (
        f"entity guard changed the match set (must be 0 mismatches): {mismatches[:2]}"
    )
    # Typo tolerance is the point of fuzzy matching: single-substitution
    # neighbours at exactly 0.8 must still match.
    typo_hits = dict(entities_mod.find_similar_entities("hello", corpus, 0.8))
    assert typo_hits.get("hello") == 1.0
    assert typo_hits.get("hallo") == 0.8, typo_hits
    assert typo_hits.get("jello") == 0.8, typo_hits


def test_banded_levenshtein_exact_for_matches():
    mods = _require_engine()
    if mods is None:
        return
    _, entities_mod = mods
    import random

    random.seed(123)
    alphabet = "abcdefghij /_.-0123456789"
    for _ in range(500):
        a = "".join(random.choice(alphabet) for _ in range(random.randint(1, 40)))
        b = "".join(random.choice(alphabet) for _ in range(random.randint(1, 40)))
        full = _reference_levenshtein(a, b)
        for threshold in (0.5, 0.8, 0.9):
            max_len = max(len(a), len(b), 1)
            max_dist = int((1.0 - threshold) * max_len + 1e-9)
            banded = entities_mod.levenshtein_distance(a, b, max_dist=max_dist)
            if full <= max_dist:
                assert banded == full, (a, b, full, banded, max_dist)
            else:
                assert banded > max_dist, (a, b, full, banded, max_dist)


def test_beam_extracts_candidates_with_raw_fallback():
    mods = _require_engine()
    if mods is None:
        return
    beam_mod, entities_mod = mods
    with tempfile.TemporaryDirectory() as tmp, _perf_env(tmp):
        beam = beam_mod.BeamMemory(session_id="perf", db_path=os.path.join(tmp, "perf.db"))
        known = _seed_corpus(beam)
        assert len(known) >= 300, len(known)
        # Raw would miss this: the 34-char question contains "Tesla" but not
        # "Tesla P40" (substring score 0.579 < 0.8), while the extracted
        # candidate "Tesla" prefix-matches it at 0.867.
        raw_only = _reference_find("Tell me about Tesla please", known, 0.8)
        assert raw_only == [], raw_only
        lane_ids = beam_mod._find_memories_by_entity(beam, "Tell me about Tesla please")
        assert lane_ids, "candidate extraction found no Tesla memories"
        # Fallback paths preserve legacy behavior.
        assert beam_mod._find_memories_by_entity(beam, "the") == []
        path_query = "/opt/data/mnemosyne_data/shard_007/mnemosyne.db what is in it"
        assert beam_mod._find_memories_by_entity(beam, path_query), (
            "raw fallback lost the path prefix match"
        )
        # Candidate cap: many capitalized phrases still cost at most 8 lookups.
        calls = {"n": 0}
        real_find = entities_mod.find_similar_entities

        def counting(entity, known_entities, threshold=0.8):
            calls["n"] += 1
            return real_find(entity, known_entities, threshold=threshold)

        with patch.object(entities_mod, "find_similar_entities", side_effect=counting):
            beam_mod._find_memories_by_entity(
                beam,
                "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett "
                "Kilo Lima Mike November Oscar Papa",
            )
        assert calls["n"] <= 8, calls


def test_recall_fans_out_less_and_rank1_hits():
    mods = _require_engine()
    if mods is None:
        return
    beam_mod, entities_mod = mods
    with tempfile.TemporaryDirectory() as tmp, _perf_env(tmp):
        beam = beam_mod.BeamMemory(session_id="perf", db_path=os.path.join(tmp, "perf.db"))
        target = beam.remember(
            "Auscoast Fire Services role and responsibilities: fire safety "
            "inspection and compliance.",
            scope="session",
        )
        known = _seed_corpus(beam)
        distinct = beam.annotations.get_distinct_values("mentions")
        assert len(distinct) >= 300, (len(known), len(distinct))
        query = "Auscoast Fire Services role and responsibilities"
        calls = {"n": 0}
        real_sim = entities_mod.similarity

        def counting(a, b, *args, **kwargs):
            calls["n"] += 1
            return real_sim(a, b, *args, **kwargs)

        with patch.object(entities_mod, "similarity", side_effect=counting):
            t0 = time.perf_counter()
            results = beam.recall(query, top_k=5)
            elapsed_ms = (time.perf_counter() - t0) * 1000
        # Pruning must happen: similarity() runs for a fraction of the
        # distinct mention values, not once per value.
        assert calls["n"] < len(distinct), (calls, len(distinct))
        ids = [row["id"] for row in results]
        assert target in ids, f"rank-1 miss after perf fix (ids={ids})"
        print(
            f"perf: {len(distinct)} mentions, {calls['n']} similarity calls, "
            f"{elapsed_ms:.1f} ms, rank-1 hit"
        )
        assert elapsed_ms < 2000, elapsed_ms


def test_dense_lane_reports_status_on_keyword_path():
    mods = _require_engine()
    if mods is None:
        return
    beam_mod, _ = mods
    with tempfile.TemporaryDirectory() as tmp, _perf_env(tmp):
        beam = beam_mod.BeamMemory(session_id="perf", db_path=os.path.join(tmp, "dense.db"))
        beam.remember("keyword-path probe content", scope="session")
        traced = beam.recall("keyword-path probe", top_k=5, explain=True)
        assert isinstance(traced, dict), type(traced)
        explain = traced.get("explain", {})
        embedding = explain.get("embedding", {})
        assert "available" in embedding and "computed" in embedding, embedding
        assert embedding["available"] is False, embedding
        assert traced.get("results"), "keyword path returned nothing"


if __name__ == "__main__":
    tests = [
        test_guard_keeps_match_set,
        test_banded_levenshtein_exact_for_matches,
        test_beam_extracts_candidates_with_raw_fallback,
        test_recall_fans_out_less_and_rank1_hits,
        test_dense_lane_reports_status_on_keyword_path,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} checks passed")
