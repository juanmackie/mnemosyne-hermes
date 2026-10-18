# Local-offline smoke test — shipped memory path, executed

Date: 2026-10-18. Machine: Windows 11 workstation (Python 3.11.15,
`mnemosyne-memory` **3.15.1** installed, Hermes v0.18.2). Remote deployment
inspected read-only via the Z640 gateway's public `/api/status` (Hermes
**v0.22.1**, gateway running).

Verdict: **PASS — 7/7 checks, zero outbound network attempts.**

---

## 1. What this proves, and what it does not

**Proves.** With `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and
`socket.socket.connect` / `socket.create_connection` replaced by a function that
records and raises **before any `mnemosyne` import**, the pinned engine still:

1. loads its local `fastembed` ONNX embedder (`BAAI/bge-small-en-v1.5`, 384-dim)
   and embeds a query;
2. `remember()`s and `recall()`s working memory with both dense and FTS signal;
3. runs the bitemporal `TripleStore` — current truth, `as_of` history, and
   `end()` — offline;
4. applies typed Weibull forgetting across 21 memory types;
5. parses a natural-language date;
6. extracts and stores an episodic gist with no LLM;
7. writes and reads a canonical profile fact.

**Does not prove.** (a) The vendored provider wrapper's tool dispatch; that is
covered by `tests/test_provider_engine_contract.py` (needs the provider
installed). (b) A first-ever install with no cached model — the ONNX file must
be fetched once (see §5). (c) Any Z640-only figure; the remote gateway is
reachable but its authenticated memory tools were not exercised (see §6).

---

## 2. Method

The guard is the point: an offline claim made by setting env vars alone can hide
a network call that silently falls back. Here any connect attempt fails loudly
and increments a counter; the summary must report `network_attempts: []` and
`verdict: PASS`.

```python
_real_connect = socket.socket.connect
def _blocked(self, address, *a, **k):
    ATTEMPTS.append(str(address))
    raise OSError(f"offline-smoke: outbound network blocked -> {address}")
socket.socket.connect = _blocked
socket.create_connection = _blocked_create
```

`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and an explicit
`MNEMOSYNE_FASTEMBED_CACHE_DIR` are set at the top of the script, before the
engine import. The three `MNEMOSYNE_*EMBEDDINGS*` opt-out flags are removed so
the dense path cannot be silently disabled and then reported as "offline".

Script: `.levelup-work/D-smoke/smoke.py` (scratch, git-ignored).
Raw log: `.levelup-work/D-smoke/smoke.log`. Machine-readable:
`.levelup-work/D-smoke/smoke_result.json`.

---

## 3. Results

Second run (first run warms the OS file cache; timings are informational, not a
benchmark):

| # | Check | Result | Detail |
|---|---|---|---|
| 1 | embeddings local ONNX | PASS | `model=BAAI/bge-small-en-v1.5 dim=384` (2,870 ms cold) |
| 2 | remember/recall | PASS | `score=0.6263 dense=0.878 fts=1.0 importance=0.8` (563 ms) |
| 3 | bitemporal triples | PASS | `current=billing`, `as_of(2026-02-01)=auth-migration`, `end_closed=1` |
| 4 | typed Weibull forgetting | PASS | age 200 h: `event=0.2915` vs `profile=0.7249`; 21 types; halflife 168 h |
| 5 | NL date parsing | PASS | `"March 3, 2026"` → `2026-03-03` |
| 6 | episodic gist (zero LLM) | PASS | gist stored; `stats_keys=[edges,facts,gists,total_nodes]` |
| 7 | canonical profile | PASS | `preference/editor = "Neovim"` |

```
"network_attempts": [],
"passed": 7, "failed": 0, "verdict": "PASS"
```

Exit code 0. The only runtime model dependency is the local ONNX file, already
cached at
`...\hermes\cache\fastembed\models--qdrant--bge-small-en-v1.5-onnx-q\...\model_optimized.onnx`
(66,465,124 bytes).

### Honest caveats found while running it

- **Episodic fact extraction returned 0 facts** for `"Maya is assigned_to
  auth-migration."` The rule-based extractor is conservative; the gist path
  worked and the check passes on that. Do not read this test as evidence that
  fact extraction is high-recall.
- **Engine stderr noise:** every engine import on this machine prints
  `Failed to inspect legacy provider defaults: 'utf-8' codec can't decode byte
  0x97 in position 19`. It is a benign `config.py` warning about a legacy
  provider-defaults file encoded in cp1252, not a memory-path failure — but it
  is stderr noise that a strict wrapper could mistake for an error. Not fixed
  here (upstream/engine concern; out of this task's scope).
- `BeamMemory` has no `close()`; the script closes `bm.conn` best-effort. Minor
  API asymmetry, noted for the spike plan.

---

## 4. Why this matters for the candidate decision

The brief asks for "local-first offline operation" as a candidate differentiator.
The **shipped stack already satisfies it**, keylessly, with one cached 66 MB ONNX
model and SQLite — no server, no API key. That means an integration whose value
proposition is "local-first" (widemem-ai) or "self-hosted graph" (synapse) does
not close an unmet offline gap; it adds a second store, a second process or a
second model runtime. This is the same finding as the engine baseline
([0_baseline_and_premises.md](./0_baseline_and_premises.md) §2).

---

## 5. Z640 runbook (executed here; re-run on the deployment host)

The deployed host is the TrueNAS Z640. To reproduce this proof there:

```bash
# once, with network — populate the ONNX cache
export HERMES_HOME=/path/to/hermes
python -c "from mnemosyne.core import embeddings as e; e.embed_query('warm cache')"

# then, offline — same script, same guard
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export MNEMOSYNE_FASTEMBED_CACHE_DIR="$HERMES_HOME/cache/fastembed"
python smoke.py        # expect: network_attempts [], verdict PASS, exit 0
```

If a run reports any `network_attempts`, it is a real regression: stop and find
the caller before claiming offline support. Do not "fix" it by setting
`MNEMOSYNE_NO_EMBEDDINGS`; that disables the dense path and changes recall
semantics.

---

## 6. Remote deployment state (read-only, authenticated)

Reached the Z640 gateway with a **dashboard session**: the supplied access JWT had
expired, but the `hermes_session_rt` refresh token was accepted and the gateway
rotated a fresh session (`Set-Cookie: hermes_session_at=...`). The session was used
**read-only**; no config was changed and no cookie was written into the repo.

`GET /api/status` (public, HTTP 200):

| Field | Value | Reading |
|---|---|---|
| `version` | `0.22.1`, release `2026.9.24` | deployed Hermes is newer than this repo's dev box (0.18.2) |
| `gateway_state` | `running`; `gateway_platforms.telegram: connected`, `api_server: connected` | live gateway, two platforms |
| `auth_required` / `auth_providers` / `auth_flows` | `true` / `["nous"]` / `["cookie","native_pkce"]` | dashboard uses a Nous session |
| `disk` | `total_mb=2085272`, `free_mb=2045843`, `used_percent=1.9` | 2.08 TB — consistent with the 2 TB tank mirror |
| `memory.system_total_mb` | `32013` | 32 GB RAM |
| `memory.pressure` | `ok`; `gateway_rss_mb≈1300` | gateway healthy |
| `components` | gateway ok, dashboard ok, storage ok, platforms ok | `overall: ok` |

Authenticated reads (HTTP 200 each):

| Request | Result |
|---|---|
| `GET /api/memory` | `active: "mnemosyne"`; provider `mnemosyne` `available:true, configured:true, status:"ready"`; `byterover` unavailable, `retaindb` needs_config; built-in files present (`memory` 1920 B, `user` 1317 B) |
| `GET /api/memory/providers/mnemosyne/config` | `db_path=/opt/data/mnemosyne_data/mnemosyne.db` (set); `profile_isolation=false`; `default_scope="session"`; `tools=["mnemosyne_remember","mnemosyne_recall","mnemosyne_stats","mnemosyne_forget"]` — exactly the canonical four default tools; engine pin `mnemosyne-memory[embeddings]>=3.15.1,<3.16` |
| `GET /api/sessions/stats` | `total: 633`, `messages: 52998`; `by_source` cron 324, telegram 253, oneshot 28, tui 12, api_server 6, cli 5, tool 3, local 2 |
| `GET /api/sessions` | 20 session metadata rows returned (paged view) |

**What this verifies:** the vendored provider is loaded and **active on a live
Hermes 0.22.1 gateway**, the engine pin matches this repo's contract, the DB path
precedence resolved to an explicit path, `default_scope` is the conservative
`session`, only the four core tools are exposed by default, and the gateway carries
real usage (53k messages). That closes the "is the provider actually running
somewhere?" question that `LIVE_VERIFICATION.md` had left open.

**Now verified — live agent-turn memory round-trip.** Once the full 64-hex
`API_SERVER_KEY` from `/opt/data/.env` was used (the dashboard's `/api/config`
reported a *different* key, and a masked value arrived first), both transports
were exercised against the live gateway:

| Transport | Flow | Result |
|---|---|---|
| OpenAI `/v1/chat/completions` + `X-Hermes-Session-Key` | remember in session A → fresh session B asks for it | `'OK'` → **`'zephyr-quokka-42'`** ✓ |
| REST `/api/sessions` + `/chat` | create S1 → remember → create S2 → ask | `'OK'` → **`'zephyr-quokka-42'`** ✓ |

Raw shapes observed:

```text
GET  /v1/models                 200 {"object":"list","data":[{"id":"hermes-agent",...}]}
POST /api/sessions              201 {"object":"hermes.session","session":{"id":"api_...","source":"api_server",...}}
POST /api/sessions/{id}/chat    200 {"object":"hermes.session.chat.completion","status":"completed",
                                     "session_id":"api_...","message":{"role":"assistant","content":"Hello. Ready when you are."},
                                     "usage":{"input_tokens":24861,"output_tokens":253,"total_tokens":25114},
                                     "runtime":{"provider":"","model":"","route_source":"global",...}}
GET  /api/sessions/{id}/messages 200 {"object":"list","session_id":"api_...",
                                     "data":[{"role":"user",...},{"role":"assistant",...},{"role":"session_meta",...}],
                                     "pagination":{"limit":500,"offset":0,"order":"latest","returned":3}}
```

This is the end-to-end proof `LIVE_VERIFICATION.md` lacked: production
**Hermes 0.22.1 + the mnemosyne provider** remembered a token written in one
session and returned it from a fresh session, on **both** the OpenAI and REST
transports. It also confirms capture works for `api_server` turns, not just
Telegram/DM turns.

### Appendix: API-key attempts (why the session was needed)

Two bearer keys supplied during the session were rejected by the API server, and
the dashboard rejects bearers outright:

```text
GET 30432 /v1/models  -> 401
{"error":{"message":"Invalid gateway API key (API_SERVER_KEY)",
          "type":"gateway_auth_error","code":"gateway_auth_failed"}}

GET 30433 /api/memory (no session) -> 401
{"error":"session_expired","reason":"invalid_or_expired_session","login_url":"/login"}
```

`/api/status` returns 200 **without** any credential, so a 200 there does not
validate a key — the loopback check that returned 200 exercised a public route.
