# Live back-and-forth with the production Hermes agent

Status: **working end-to-end, verified 2026-10-18.** A real agent-turn memory
round-trip passed on the Z640 deployment (Hermes 0.22.1, provider `mnemosyne`).

## What was investigated

The brief's suggested tool, [`Linuxmaster14/telepipe`](https://github.com/Linuxmaster14/telepipe),
cannot carry a conversation. Verified from its source
(`.levelup-work/telepipe/telepipe`):

- only three Telegram calls: `sendMessage`, `sendDocument`, `sendVideo`;
- **no `getUpdates` and no receive path** — it is send-only;
- it authenticates with a `BOT_TOKEN` (Bot API), and Bot API messages are never
  delivered to another bot, so it cannot reach `@truenas_hermesbot`.

Telepipe is usable only for outbound notifications. It is not a bridge.

## The channel that works

The gateway's **api_server** on `:30432` (0.0.0.0, reachable on the Tailscale IP;
no Serve mapping needed) authenticates with
`Authorization: Bearer <API_SERVER_KEY>` and exposes two transports:

| Transport | Endpoints | Memory scoping |
|---|---|---|
| OpenAI-compatible | `GET /v1/models`, `POST /v1/chat/completions` | `X-Hermes-Session-Id`, `X-Hermes-Session-Key` |
| REST session | `POST /api/sessions`, `POST /api/sessions/{id}/chat`, `GET /api/sessions/{id}/messages` | provider defaults (turn capture) |

The dashboard (`:30433`) is separate: Nous session cookie, PTY chat over a JSON-RPC
WebSocket. It is useful for memory *configuration*, not iterative memory work.

## Why the first attempts failed (keep for the next person)

1. **Wrong key source.** The authoritative key is the 64-hex `API_SERVER_KEY` in
   `/opt/data/.env`. The dashboard's `/api/config` reported a *different* value
   under both `gateway.api_server_key` and `env.API_SERVER_KEY`, so every request
   with that value returned `401 "Invalid gateway API key (API_SERVER_KEY)"`.
   A gateway drain+restart did not help — the value itself was wrong.
2. **Masked key.** A relayed value arrived as `abcd...ef12` — 14 characters with
   an ellipsis; the real key is 64 hex chars. Masked values cannot authenticate.
3. **Drain stuck.** `POST /api/gateway/drain` parks the gateway in
   `draining` (refuses new turns) and a restart does **not** clear it. Cancel with:
   `POST /api/gateway/drain {"action":"cancel"}` → `{"ok":true,"was_draining":true}`;
   the watcher returns `gateway_state` to `running` within seconds.
4. **PowerShell → curl quoting.** JSON passed as `-d '{"a":"b"}'` from PowerShell
   reaches `curl.exe` with the quotes stripped ("Invalid JSON in request body").
   Use `--data-binary @file` or a Python client.

## Verified flows and raw shapes

```text
GET  /v1/models                  200 {"object":"list","data":[{"id":"hermes-agent",...}]}
POST /api/sessions               201 {"object":"hermes.session","session":{"id":"api_…","source":"api_server",...}}
POST /api/sessions/{id}/chat     200 {"object":"hermes.session.chat.completion","status":"completed",
                                      "session_id":"api_…",
                                      "message":{"role":"assistant","content":"Hello. Ready when you are."},
                                      "usage":{"input_tokens":24861,"output_tokens":253,"total_tokens":25114},
                                      "runtime":{"provider":"","model":"","route_source":"global",...}}
GET  /api/sessions/{id}/messages 200 {"object":"list","session_id":"api_…",
                                      "data":[{"role":"user",...},{"role":"assistant",...},{"role":"session_meta",...}],
                                      "pagination":{"limit":500,"offset":0,"order":"latest","returned":3}}
```

**Memory round-trip (both transports passed):**

```text
OpenAI, shared X-Hermes-Session-Key:
  A "Remember this exact token for later: zephyr-quokka-42. Reply with just OK." -> 'OK'
  B "What exact token did I ask you to remember earlier?"                        -> 'zephyr-quokka-42'  ✓
REST session, no session key:
  S1 remember -> 'OK'
  S2 ask      -> 'zephyr-quokka-42'  ✓
```

That is the end-to-end proof `LIVE_VERIFICATION.md` lacked: capture works for
`api_server` turns, and recall survives a fresh session.

## Usage

```powershell
$env:HERMES_API_KEY = "<full 64-hex API_SERVER_KEY from /opt/data/.env>"
python .levelup-work/live_chat.py "What memory provider are you running?"
python .levelup-work/live_chat.py          # interactive REPL
```

The client auto-detects OpenAI vs REST transport, retries `503 gateway_draining`,
sends `X-Hermes-Session-Id` + `X-Hermes-Session-Key: dev-live`, and polls messages
when using the REST path. Credentials are read from the environment only; nothing
is written to disk.
