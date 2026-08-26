# HANDOFF — Home Assistant integration

JTS delegates smart-home control to whatever Home Assistant the household
already runs on the LAN. It captures the utterance, hands it to HA's
conversation pipeline, and speaks back what HA returns. **HA owns NLU, entity
resolution, automation dispatch, and sentence triggers; JTS is a relay** — see
[ADR-0116](adr/0116-smart-home-relays-through-has-conversation-api.md) for why
the conversation API rather than MCP or the Assist Pipeline.

Neighbouring owners: [HANDOFF-prompting.md](HANDOFF-prompting.md) (the untrusted
tool-result fencing this integration's gate sits beside, and the privilege-
separation work that would complete it) ·
[historical/home-assistant-integration-design-2026-05.md](historical/home-assistant-integration-design-2026-05.md)
(the surface comparison as read in 2026-05, the wizard walkthrough, the
pre-child-cache performance numbers, the release smoke test, and the sources).

```sh
http://jts.local/ha/                                        # configure
http://jts.local/system/                                    # connection status
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "Home Assistant"
curl -s http://jts.local:8780/state | jq .home_assistant
ssh pi@jts.local 'journalctl -u jasper-voice -f' | grep "event=ha\."
```

## Configuration

The `home_assistant` voice tool is registered only when `JASPER_HA_URL` **and**
`JASPER_HA_TOKEN` are both set in `/var/lib/jasper-intsecrets/home_assistant.env`
(sourced by `deploy/systemd/jasper-voice.service`). Otherwise the tool is not in
the registry, the model cannot see it, and smart-home requests get answered
conversationally. Key names are constants in
[`jasper/home_assistant.py`](../jasper/home_assistant.py), imported everywhere
they are read so a rename touches one file:

| Key | Purpose |
|---|---|
| `JASPER_HA_URL` | Base URL, e.g. `http://homeassistant.local:8123` |
| `JASPER_HA_TOKEN` | Long-Lived Access Token (JWT, ~180-220 chars) |
| `JASPER_HA_AGENT_ID` | Optional `conversation.*` entity to route through |
| `JASPER_HA_VERIFY_SSL` | `"0"`/`"false"`/`"no"` disables TLS verification for self-signed HA installs. Absent or `"1"` = verify. The wizard renders the toggle only when the URL is `https://`. |
| `JASPER_HA_RECENT_URLS` | JSON list of the last 3 successful URLs — quick-pick for households moving between networks |

Setup is the socket-activated wizard at `http://jts.local/ha/` (port 8778,
idle-exits after 10 min, so it costs zero RAM when nobody is using it). It is a
three-state form — discover or enter a URL, paste a token, then a connected
status card with Test / agent override / Disconnect. **Save validates against
the live HA before persisting**, so an invalid token drops the token and keeps
the URL rather than writing a broken config, and the env file is written
atomically. Full walkthrough in the
[design record](historical/home-assistant-integration-design-2026-05.md#setup-walkthrough).

## Request flow

```
"Hey Jarvis, bedroom medium."
  → wake + VAD → realtime provider decides smart-home → home_assistant(query=…)
  → HAClient.process() → POST {HA}/api/conversation/process
       {"text": "bedroom medium", "language": "en"}  Authorization: Bearer <LLAT>
  → HA's default_agent matches the household's sentence trigger, runs the
    automation, and answers {"response": {"response_type": "action_done",
    "speech": {"plain": {"speech": "Done."}}}, "conversation_id": …}
  → HAResponse(speech="Done.", success=True, …)
  → tool returns {"spoken_response": "Done.", "success": true, …}
  → the model speaks "Done."
```

JTS never learns what entities exist or what "bedroom medium" means.

## Consequential-action confirmation

HA is the only tool on this speaker that performs a real-world action, which
makes it the confused-deputy target. `classify_consequential(query)` in
[`jasper/tools/home_assistant.py`](../jasper/tools/home_assistant.py) flags
unlock / disarm / alarm-off / open-a-door actions, and the gate fires **only
when a shared `UntrustedContentMonitor` reports third-party text was read within
`UNTRUSTED_CONTENT_WINDOW_SEC`** (`jasper/tools/__init__.py`, 600 s). In that
window the tool does not relay the request: it stashes it in a single-slot,
single-use, ~90 s TTL store and returns `{needs_confirmation: true, action,
spoken_response}` — a question the model must speak and answer in a later turn
via the no-argument `home_assistant_confirm()`. A clean voice-only session runs
"unlock the door" directly. `monitor=None` fails safe to always-confirm.

Rationale, limits, and the residual risks are
[ADR-0117](adr/0117-consequential-actions-confirm-only-inside-the-taint-window.md).
Observability is action-label only: `event=ha.confirm_gate`,
`event=ha.confirm_execute`, and `event=ha.consequential_direct` at DEBUG. The
utterance is never logged.

## The HAClient

[`jasper/home_assistant.py:HAClient`](../jasper/home_assistant.py) is the
persistent async client for one HA instance. Four choices are load-bearing:

- **One persistent `httpx.AsyncClient`**, lazily built on first call, reused for
  the daemon's lifetime, `aclose()`d at shutdown. Per-call instantiation (the
  dominant anti-pattern in surveyed Python HA integrations) rebuilds TCP+TLS
  every turn.
- **Split timeouts, `connect=3s` / `read=90s`.** Connect failures must fail fast
  (HA down → the model says so); read failures need patience because LLM-backed
  HA agents legitimately take 30-60 s for a tool-using turn. The wizard/probe
  cascade uses a 5 s health read instead.
- **No retry on 5xx.** The conversation endpoint is not idempotent — a retried
  "turn off the lights" could double-fire a script. Try once, fail soft.
- **POST `/api/conversation/process`, never `/api/services/conversation/process`.**
  The latter returns no response body (home-assistant/core#93754, #104122).

```python
class HAClient:
    def __init__(self, url, token, *, agent_id=None, language="en",
                 verify_ssl=True, timeout=None, http=None, clock=None): ...
    async def process(self, query: str) -> HAResponse: ...
    async def healthcheck(self) -> bool: ...
    async def config(self) -> dict | None: ...
    async def list_agents(self) -> list[dict]: ...
    async def aclose(self) -> None: ...
    url: str                      # property
    conversation_id: str | None   # property

@dataclass(frozen=True)
class HAResponse:
    speech: str                  # speak this verbatim
    success: bool                # response_type != "error" AND speech non-empty
    response_type: str           # action_done | query_answer | error | ""
    error_code: str | None       # no_intent_match | no_valid_targets | ...
    outcome: str                 # one of the OUTCOME_* constants
    conversation_id: str | None  # canonical, from HA's response
    continue_conversation: bool  # hint only — HA's heuristic is known-flaky
    targets_success: list[dict]
    targets_failed: list[dict]
    latency_ms: int
    error_detail: str            # short text for logging
```

### Failure taxonomy

Every call is tagged with one of seven `OUTCOME_*` buckets, which is what makes
`grep 'event=ha\.call'` slice cleanly:

| Outcome | Trigger | User experience |
|---|---|---|
| `ok` | `response_type != "error"` and speech non-empty | Model speaks HA's response |
| `intent_miss` | `response_type == "error"` | Model speaks the error briefly |
| `network` | `httpx.ConnectError`, DNS, … | "I can't reach Home Assistant" |
| `timeout` | `httpx.TimeoutException` | "Home Assistant didn't respond" |
| `auth` | HTTP 401 | "I'm not authorized — reconnect at…" |
| `agent_error` | HTTP 5xx | "Home Assistant had an internal error" |
| `parse_error` | non-200, malformed body, or empty speech | "…a response I couldn't understand" |

`no_valid_targets` is **not** a hard error. In a multi-speaker home another
device may have answered the same utterance, and HA's speech ("I couldn't find a
device matching that") is still useful. It tags as `intent_miss`; the model gets
the text either way.

### Conversation ID lifecycle

`HAClient` decides when to send a `conversation_id` from HA's
`continue_conversation` field plus a `CONVERSATION_ID_TTL_SEC = 240.0` idle TTL
(under HA's empirical ~5 min contract, with margin). First call sends none and
HA mints one; a call within the TTL whose prior response said
`continue_conversation=true` reuses the cached id; anything else drops the
cache. Whatever HA returns is canonical, including a silent rotation. A daemon
restart loses the cache and HA mints a fresh id — the household loses the
implicit "we were talking about lights" context, which is not worth persisting.

### Agent ID selection

`agent_id` routes a call to a specific HA conversation entity instead of HA's
UI-configured default; households use it for cost/latency trade-offs (a cheap
rule-based agent for JTS, an LLM-backed one for the HA dashboard). The field is
**undocumented in HA's REST surface but functional** — the validator accepts it
and `async_converse` passes it through. Because that is untested ground for
schema tightening, `test_agent_id_pass_through_when_set` pins that we send it,
so CI catches an HA release that starts 4xx-ing on unknown fields.

## probe_status — the one-shot helper

`probe_status` is a stateless reachability probe with three callers:
`jasper-control`'s `/state.home_assistant` and `/system/snapshot`, and
`jasper-doctor`'s `check_home_assistant`. It is distinct from
`HAClient.process()` — no conversation state, no per-call structured logging,
and crucially **it never touches `/api/conversation/process`**, because that
endpoint costs real money on LLM-backed HA agents. It hits only `GET /api/` and
`GET /api/config`.

Results are cached process-globally for `PROBE_CACHE_TTL_SEC = 15.0` keyed by
URL, token, and verify-SSL. Without the cache a dashboard polling every 5 s
against an unreachable HA would block each poll for the full health timeout.
`force=True` bypasses it — `jasper-doctor` does this so its output reflects
state at invocation. It logs one line per `(configured, connected)` transition,
not per call: `event=ha.reachable` / `event=ha.unreachable`, which is the right
signal for "when did HA go down?" without per-poll noise.

```python
{"configured": bool,        # url AND token both present
 "connected": bool,         # GET /api/ returned 200 + sigil
 "url": str,                # normalized
 "instance_name": str | None, "version": str | None,  # from /api/config
 "error": str | None}
```

## Status surfaces

`jasper-control` exposes reachability at `/state.home_assistant` and in the Home
Assistant card on `/system/`, which polls `/system/snapshot` every 5 s. **It
never runs the probe inline on either request path.** A small
`HomeAssistantStatusCache` refreshes stale status by starting
`python -m jasper.control.ha_probe_child`; the child imports
`jasper.home_assistant`, reads the env file, runs `probe_status_from_env()`,
writes JSON, and exits. The parent keeps only the JSON status and a redacted
env-file signature — a wizard save changes that signature, so `/state` or
`/system/` starts a refresh even inside the normal TTL.

Reads return immediately with `checking=true` or stale cached status while a
refresh is in flight. Child failure logs `event=ha.status_probe_failed` and both
surfaces still render. **The parent cache owns the `event=ha.reachable` /
`ha.unreachable` / `ha.unconfigured` transition logging for these surfaces**;
the child's stderr is captured and not replayed, because each short-lived child
has no durable transition history.

The card shows Checking · Refresh failed (with the last known URL/name) · Not
configured · ✓ Connected (green, with name and version) · ✗ Unreachable (red,
with the error), plus the URL and a link to `/ha/`.

`/state.home_assistant` is fail-soft per the section pattern: the probe runs in
`/state`'s `asyncio.gather` fan-out beside camilla / airplay / voice, and an
unconfigured or unreachable HA returns a clean shape with `configured` /
`connected` flags rather than breaking the whole response.

`check_home_assistant(cfg)` follows skip-if-not-configured: unset → `ok` with a
hint pointing at the wizard; configured and reachable → `ok` with instance name
and version; configured and unreachable → `fail` with the probe error and the
wizard pointer.

## The tool and the prompt

One tool, `home_assistant(query: str)`, relays the utterance and returns the
parsed result. **The tool's docstring is the description the model sees** — it
teaches when to call (smart-home control, area-scoped commands, scenes/scripts,
sentence-trigger phrases, state queries) and when not to (weather, music, time,
transit, timers, calendar, email, general conversation), as conditional rules
rather than absolute prohibitions. `make_home_assistant_tools(ha)` returns `[]`
when `ha is None`, the same gating pattern as `make_bus_tools` /
`make_subway_tools`.

`SYSTEM_INSTRUCTION` in [`jasper/voice_daemon.py`](../jasper/voice_daemon.py)
carries two static blocks and one dynamic addendum: when to call; what to say
after (speak `spoken_response` verbatim on success, `error_detail` briefly on
failure, and do not add "OK" or "Done" on top of HA's own wording); and, only
when `ha_configured=False`, a tool-unavailable nudge telling the model to say
smart-home control is not set up and explicitly **not** to call any other tool.
That last guard exists because real voice logs showed the model misrouting "turn
on the bedroom lights" to `get_current_time` + `get_now_playing` instead of
recognising a smart-home-shaped request it could not serve. `{hostname}` comes
from `cfg.hostname`, so multi-speaker households see the speaker they are
talking to.

## Resilience

The voice loop never blocks on HA at startup — the client is lazy, so the daemon
boots fine with HA down. Tool errors propagate as natural text; no exception
escapes the dispatcher. There is deliberately **no background supervisor
probing HA**: it is not on the wake-to-response critical path, so a continuous
health probe would burn bandwidth and log noise for no real win. On-demand
probing via `/state`, `/system/`, and doctor is enough.

## Troubleshooting

**Wake responds but smart-home commands don't fire.** Check registration:
`journalctl -u jasper-voice -n 200 | grep "home_assistant:"` — `enabled url=…`
is good, `disabled (set JASPER_HA_URL…)` means the wizard did not write the env
file or the daemon did not restart.

**Tool fires but HA reports "no matching device".** HA's NLU could not resolve
it: the entity is not exposed to Assist (HA → Settings → Voice Assistants →
Expose), an alias does not match the phrasing, or an LLM-backed HA agent has no
entity context.

**Tool fires but takes 10+ seconds.** The household set HA to an LLM-backed
conversation agent, so two LLM hops are being paid. Switch HA's default to the
rule-based agent, or set `JASPER_HA_AGENT_ID=conversation.home_assistant` in the
wizard's advanced disclosure to route JTS specifically to the fast path.

**`/system/` shows ✗ Unreachable.** Auth (token revoked or rotated — Disconnect
and re-paste), network (HA changed IP or subnet — re-discover), or TLS (a
self-signed cert with `JASPER_HA_VERIFY_SSL` unset).

**A sentence trigger doesn't fire.** Confirm it works in HA's own Assist UI
first — if it fails there it will fail through JTS, which is a relay. Then check
`journalctl -u jasper-voice | grep "event=ha\.call"` for the actual `query=` the
model sent: the prompt says to pass household phrases verbatim, but realtime
models sometimes paraphrase.

**Daemon restarts in a loop after save.** Most likely a malformed URL (rare;
`_normalize_url` should reject) or a corrupt token. Inspect
`/var/lib/jasper-intsecrets/home_assistant.env`, then re-save via the wizard or
delete the file to fully reset.

## File map

```
jasper/home_assistant.py            HAClient + HAResponse + probe_status + build_ha_client
jasper/tools/home_assistant.py      make_home_assistant_tools() + classify_consequential
jasper/tools/__init__.py            UntrustedContentMonitor (taint window) + fence_untrusted
jasper/web/home_assistant_setup.py  the /ha/ wizard (port 8778)
jasper/config.py                    ha_url / ha_token / ha_agent_id / ha_enabled
jasper/voice_daemon.py              registry wiring + SYSTEM_INSTRUCTION
jasper/control/ha_status_cache.py   the parent cache + transition logging
jasper/control/ha_probe_child.py    the short-lived probe process
jasper/control/server.py            /state.home_assistant + /system/snapshot section
jasper/web/system_setup.py          the /system/ dashboard card
jasper/cli/doctor/integrations.py   check_home_assistant()

deploy/systemd/jasper-voice.service EnvironmentFile=-/var/lib/jasper-intsecrets/home_assistant.env
deploy/jasper-web.socket            ListenStream=127.0.0.1:8778
deploy/nginx-jasper.conf            location /ha/ → 127.0.0.1:8778
deploy/index.html                   the Integrations section's HA row
```

Last verified: 2026-08-26 (triage pass — env-key constants, timeouts, outcome
buckets, `CONVERSATION_ID_TTL_SEC`, `PROBE_CACHE_TTL_SEC`,
`UNTRUSTED_CONTENT_WINDOW_SEC`, the confirmation-store TTL, the status-cache
event names, and every file-map path rechecked against their owning files. Test
counts were dropped rather than re-verified. The MCP research and the
confirmation-gate rationale became ADR-0116 and ADR-0117; the wizard
walkthrough, superseded performance table, release smoke test, v1.1 options, and
sources moved to `docs/historical/home-assistant-integration-design-2026-05.md`.)
