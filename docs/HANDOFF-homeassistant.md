# HANDOFF — Home Assistant integration

The JTS speaker delegates smart-home control to whatever Home Assistant
the household already has running on the LAN. JTS is a relay: it captures
the user's utterance, hands it to HA's conversation pipeline, and speaks
back whatever HA returns. HA owns NLU, entity resolution, automation
dispatch, sentence triggers — everything that makes "turn on the bedroom
lights" or "bedroom medium" work for THIS household.

This doc explains why we chose that architecture, how requests flow end to
end, where each piece lives in the codebase, and what to look at when
something breaks.

## TL;DR

```sh
# Configure (one-time, household-facing):
http://jts.local/ha/

# Try it:
"Hey Jarvis, turn on the bedroom lights."

# See connection status:
http://jts.local/system/

# Diagnose:
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "Home Assistant"

# Read the live state:
curl -s http://jts.local:8780/state | jq .home_assistant

# Tail HA-specific events:
ssh pi@jts.local 'journalctl -u jasper-voice -f' | grep "event=ha\."
```

When `JASPER_HA_URL` and `JASPER_HA_TOKEN` are both set in
[`/var/lib/jasper/home_assistant.env`](../deploy/systemd/jasper-voice.service),
the `home_assistant` voice tool is registered and the model can call it.
Env keys (defined as constants in
[`jasper/home_assistant.py`](../jasper/home_assistant.py), imported
everywhere they're read so a rename touches one file):

| Key | Purpose |
|---|---|
| `JASPER_HA_URL` | Base URL, e.g. `http://homeassistant.local:8123` |
| `JASPER_HA_TOKEN` | Long-Lived Access Token (JWT, ~180-220 chars) |
| `JASPER_HA_AGENT_ID` | Optional `conversation.*` entity to route through |
| `JASPER_HA_VERIFY_SSL` | `"0"`/`"false"`/`"no"` disables TLS verification (HTTPS-self-signed HA installs). Absent or `"1"` = verify (safe default). Wizard renders the toggle only when URL is `https://`. |
| `JASPER_HA_RECENT_URLS` | JSON-encoded list of last 3 successful URLs — quick-pick in state 1 for households moving between networks |

When either URL or token is missing, the tool isn't registered, the model can't see
it, and smart-home requests get answered conversationally ("smart home
isn't set up yet — visit jts.local/ha").

Setup is the wizard at `http://jts.local/ha/`. The full
three-state walkthrough is in [Setup walkthrough](#setup-walkthrough).

## Why the conversation API, not MCP

This was the load-bearing architecture decision. HA exposes three
viable external surfaces in May 2026 — REST conversation API,
WebSocket Assist Pipeline, and the MCP server at `/api/mcp`. We pick
the REST conversation API. The reasons, grounded in primary source:

**1. HA's MCP server cannot trigger automations.** Verified by reading
`homeassistant/helpers/llm.py:_async_get_tools()` and
`homeassistant/components/intent/__init__.py`. HA's MCP server is a
thin passthrough over `AssistAPI.tools`, which exposes:
- `IntentTool` wrappers for the built-in intent handlers (`HassTurnOn`,
  `HassLightSet`, `HassClimateSetTemperature`, etc.)
- One `ScriptTool` per exposed script
- `GetLiveContextTool` for state snapshots

What's NOT in that list: any tool that calls `automation.trigger`.
Calling `HassTurnOn(name="<automation>")` against an `automation.*`
entity calls `automation.turn_on`, which **enables** a disabled
automation rather than running it. So with MCP, a household that has
wired voice commands to HA automations — "good morning", "bedtime",
"movie time", "I'm leaving" — has those commands structurally
unreachable.

**2. Sentence triggers (custom phrases) only fire through the
conversation pipeline.** HA's `default_agent._async_handle_message`
runs in this order:

```python
if trigger_result := await self.async_recognize_sentence_trigger(user_input):
    response_text = await self._handle_trigger_result(...)
    response.async_set_speech(response_text)
if response is None:
    intent_result = await self.async_recognize_intent(user_input)
```

Sentence triggers (the `trigger: conversation` automation type — HA's
documented mechanism for arbitrary household phrases like "bedroom
medium") are step 1. They're not registered as intent handlers, so
the AssistAPI's intent loop in MCP can never see them.

**3. HA team direction.** As of HA 2026.5, the MCP server integration
sits at ~2.1% of active installs and has had only bug fixes in
2026.1-2026.5 (PR #162319 unicode escape, PR #168187 ResourceWarnings).
The Voice Chapter 10 (June 2025) + 11 (Oct 2025) work and HA 2026.4's
thinking-steps UI all invested in the in-HA conversation agent path.
The MCP server is stable but not the team's strategic surface for
external LLM-driven devices.

**4. Conversation API works across all three JTS realtime providers.**
It's an HTTPS POST returning JSON. The same one-tool schema serializes
identically to Gemini's `function_declarations`, OpenAI Realtime's
flat `{type: "function", ...}`, and Grok's OpenAI-compat shape. MCP
would require per-provider schema translation plus a workaround for
Gemini 3.1 Flash Live Preview's mid-session restriction
(`send_client_content` rejected with 1007 after the first turn — see
[livekit/agents#5496](https://github.com/livekit/agents/issues/5496)).

The full research that fed this decision is in the project history;
the case for client-side MCP as a v1.1+ alternative is in [Future
work](#future-work-v11) below.

## End-to-end request flow

```
User: "Hey Jarvis, bedroom medium."
   │
   ▼
[ openWakeWord fires on "Jarvis" → Silero VAD captures speech window ]
   │
   ▼
[ Realtime provider (Gemini Live / OpenAI Realtime / Grok) transcribes
  + decides smart-home + emits tool call ]
   │
   ▼
home_assistant(query="bedroom medium")
   │
   │  jasper/tools/home_assistant.py → HAClient.process()
   │
   ▼
POST http://homeassistant.local:8123/api/conversation/process
Body: {"text": "bedroom medium", "language": "en"}
Authorization: Bearer <LLAT>
   │
   ▼
[ HA's default_agent runs async_recognize_sentence_trigger
  → matches the household's "bedroom medium" sentence trigger
  → runs the automation → lights set to ~50% brightness ]
   │
   ▼
Response: {"response": {"response_type": "action_done",
                        "speech": {"plain": {"speech": "Done."}},
                        ...},
           "conversation_id": "01HX...",
           "continue_conversation": false}
   │
   ▼
HAClient.process() returns HAResponse(speech="Done.", success=true, ...)
   │
   ▼
Tool returns {"spoken_response": "Done.", "success": true, ...}
   │
   ▼
[ Realtime model speaks "Done." ]
```

JTS doesn't know what entities exist. JTS doesn't know what "bedroom
medium" means. HA does, and HA owns the routing. JTS is a relay.

## Consequential-action confirmation (prompt-injection defense)

HA is the only tool on this speaker that performs a real-world action,
which makes it the confused-deputy target: untrusted content the model
has read (an email body, a device name) could steer it into
`home_assistant("unlock the front door")` with no human intent. The
durable control for that — per OWASP LLM01 "human oversight for
high-risk operations" and the agent-security design-patterns literature
(sources in [HANDOFF-prompting.md](HANDOFF-prompting.md) "Untrusted
tool-result fencing") — is **least-privilege + confirmation on
consequential actions**, not fencing HA's reply text.

How it works, in [`jasper/tools/home_assistant.py`](../jasper/tools/home_assistant.py):

- `classify_consequential(query)` flags high-impact, hard-to-reverse,
  security-relevant actions (unlock, disarm, turn off the alarm/security,
  open a garage/gate/door). Conservative, English-keyword, base-verb
  forms so state queries ("is the door unlocked?") don't over-fire. It
  errs toward confirming.
- **Conditional on recent untrusted content (the cost lands only in the
  risk window).** The gate fires only when an `UntrustedContentMonitor`
  (shared with the gmail/calendar tools — they stamp it when they return
  third-party text) reports untrusted content was read within ~10 minutes.
  A clean voice-only session runs "unlock the door" **directly, no
  prompt** — confirming every consequential command regardless of context
  would tax the common case for a ~1%-of-the-time risk. It's a
  deliberately dumb wall-clock window, NOT tied to the model's context
  window or per-provider session persistence (`UNTRUSTED_CONTENT_WINDOW_SEC`
  in [`jasper/tools/__init__.py`](../jasper/tools/__init__.py)).
  `monitor=None` is the fail-safe (always confirm), so a wiring miss errs
  toward caution. Voice/acoustic injection is out of scope by design.
- **Structural gate:** when a consequential query arrives in that window,
  `home_assistant` does NOT relay it to HA. It stashes the request in a
  single-slot, TTL-bounded, single-use store and returns
  `{needs_confirmation: true, action, spoken_response}` — a yes/no
  question. The action is never executed in the call that requests it.
- `home_assistant_confirm()` (no args) runs the stashed action — and
  only that action — after the user audibly confirms. The
  `needs_confirmation` cross-tool rule in `SYSTEM_INSTRUCTION` tells the
  model to speak the question, wait, and call confirm only on a clear
  "yes" in a later turn (never same-turn).

So after the household reads an email, a silent injected unlock becomes an
audible "Do you want me to unlock the door?" they answer — while an ordinary
spoken "unlock the door" in a clean session just works. It also catches
mishears in the tainted window.

**Limits (documented, not hidden).** The classifier is a best-effort
safety net: an obfuscated household sentence-trigger (e.g. "good night"
wired in HA to unlock a door) carries no consequential keyword and
bypasses it — JTS can't know what a household phrase *does* because HA
owns NLU. And because JTS's realtime loop has a single model mediating
everything (the trace/turn machinery isn't wired into production
dispatch — see `jasper/voice/trace.py`), a fully-hijacked model could in
principle call `home_assistant` then `home_assistant_confirm` in one
breath; the gate raises the bar and defeats the *silent* attack, but the
complete fix is privilege separation / dual-LLM, tracked as future work
in [HANDOFF-prompting.md](HANDOFF-prompting.md). A pending confirmation is
also bounded in time, not just by context: the tool can't see intervening
turns, so a stale "yes" much later could in theory fire an abandoned
action — `_ConfirmationStore` mitigates this two ways (a ~90 s TTL, and
`clear()` whenever a *different* home_assistant command supersedes the
pending), but a long-delayed bare "yes" inside the window is the residual.
Observability: `event=ha.confirm_gate` / `event=ha.confirm_execute`
(action label only, never the utterance); a consequential action that ran
*without* asking (clean session) logs `event=ha.consequential_direct` at
DEBUG.

## File map

```
jasper/home_assistant.py            HAClient + HAResponse + probe_status + build_ha_client
jasper/tools/home_assistant.py      make_home_assistant_tools(ha, monitor) → [home_assistant, home_assistant_confirm] + classify_consequential
jasper/tools/__init__.py            UntrustedContentMonitor (taint window) + fence_untrusted
jasper/web/home_assistant_setup.py  Wizard at /ha/ (port 8778)
jasper/config.py                    ha_url / ha_token / ha_agent_id / ha_enabled
jasper/voice_daemon.py              Registry wiring + SYSTEM_INSTRUCTION addition
jasper/control/server.py            /state.home_assistant + /system/snapshot section
jasper/web/system_setup.py          /system/ dashboard card
jasper/cli/doctor.py                check_home_assistant() (skip-if-not-configured)

deploy/systemd/jasper-voice.service EnvironmentFile=-/var/lib/jasper/home_assistant.env
deploy/jasper-web.socket            ListenStream=127.0.0.1:8778
deploy/nginx-jasper.conf            location /ha/ → 127.0.0.1:8778
deploy/index.html                   Integrations section has the HA row

tests/test_home_assistant.py        HAClient unit tests (37)
tests/test_home_assistant_probe.py  probe_status + check_home_assistant (10)
tests/test_home_assistant_setup.py  Wizard handler tests (35)
tests/test_tools_home_assistant.py  Tool dispatch + provider-schema (12)
tests/test_control_server.py        /state.home_assistant fail-soft tests (3)
```

## Setup walkthrough

Visit `http://jts.local/ha/` from any device on the LAN.
The wizard is socket-activated (idle-exits after 10 min of no
requests; first request takes ~500 ms to cold-start), so it costs
zero RAM when nobody's using it.

The page is a three-state form driven by what's in
`/var/lib/jasper/home_assistant.env`:

### State 1: nothing configured

The page shows:
- **Find Home Assistant on this network** button (mDNS browse for
  `_home-assistant._tcp.local.` for 4 seconds; presents each hit with
  `location_name + version + URL`).
- **Manual URL** field side-by-side. mDNS is link-local; if HA is on
  a different subnet (~30% of HA installs per community signal), the
  scan returns empty and the manual path is the primary path.
- **Recent URLs** chips if any were previously connected.

Tapping a discovered instance or entering a URL → POST `/save` →
URL persisted, state transitions to State 2.

### State 2: URL set, no/invalid token

The page shows the saved URL plus a Long-Lived Access Token paste field
(`<textarea>`, not `<input type="password">` — tokens are ~180-220
chars and the most common setup failure is a truncated copy; showing
the pasted text lets the user self-diagnose).

Inline instruction with a deep link:

> In Home Assistant, open `<HA URL>/profile/security`, scroll to the
> bottom, click Create Token, name it "JTS Speaker", and paste the
> value here.

POST `/save` → wizard validates against the live HA (`GET /api/`,
expects `{"message": "API running."}`) before persisting. Invalid
token → URL stays, token dropped, user lands back in State 2 with
the error. Valid → State 3.

**HTTPS with self-signed certs.** HA's standard local-install posture
is plain HTTP on port 8123. Households that have configured HTTPS
with HA's self-signed cert (a real and common configuration — HA's
docs walk through it) get a checkbox in State 2 saying "Accept a
self-signed certificate". The checkbox is **only rendered when the
URL is https://**; plain HTTP has no TLS to verify. Default off
(verify enabled). Checking it writes `JASPER_HA_VERIFY_SSL=0` and
propagates through to HAClient, probe_status, and the wizard's own
verify step — so the household sees the same TLS behaviour at every
layer of the stack.

### State 3: connected

Status card showing instance name + version, the URL (masked),
the token (`prefix…suffix` via `mask_secret`), the current agent
override (or "Home Assistant default"). Inline:

- **Test connection** button → re-runs `/verify` and displays result
- **Conversation agent (advanced)** disclosure → on open, fetches
  `GET /api/states` filtered to `conversation.*`, populates a picker.
  Defaults to empty (let HA's UI-configured default win).
- **Disconnect** button (confirm-gated) → clears URL + token, keeps
  recent-URLs around for one-tap reconnect, restarts `jasper-voice`.

**Post-save restart UX.** When the user lands on State 3 from a
fresh `/save`, the redirect carries `restarting=1`. The page shows
a "Configuring… the speaker is finishing its restart. Voice commands
will work in a few seconds." chip and polls `/verify` every 1 s for
up to 15 s. Once `/verify` returns ok, the chip flips to "✓ Ready"
and the URL is cleaned via `history.replaceState`. On timeout, a
friendly fallback chip with an inline Test button takes over. This
prevents the user from speaking "Hey Jarvis, turn on the bedroom
lights" against a still-rebooting daemon.

The wizard does NOT poll outside the restart window. To re-test
after the chip clears, click the Test button.

## The HAClient

[`jasper/home_assistant.py:HAClient`](../jasper/home_assistant.py)
is the persistent async client for one HA instance.

**Key design choices:**

- **Persistent `httpx.AsyncClient`.** Lazily constructed on first
  call, reused across the daemon's lifetime, aclose()d at shutdown.
  Per-call AsyncClient instantiation (the dominant prior-art
  anti-pattern in surveyed Python HA integrations) rebuilds TCP+TLS
  every turn.
- **Split timeouts:** `connect=3s, total=90s`. Connect failures
  should fail FAST (HA down → model speaks "I can't reach Home
  Assistant"); read failures need patience because LLM-backed HA
  agents (OpenAI Conversation, Anthropic, Google Generative AI
  inside HA) legitimately take 30-60s for a tool-using turn.
- **No retry on 5xx.** The conversation endpoint is non-idempotent
  — a retried "turn off the lights" could double-fire an associated
  script. Universal pattern in surveyed code: try once, fail soft.
- **Six-bucket outcome classification:** every call is tagged with
  one of `ok / network / timeout / auth / agent_error / intent_miss
  / parse_error`. Used in structured log lines so
  `jasper-trace.sh | grep ha\\.call` slices cleanly by category.
- **`conversation_id` lifecycle is opaque to callers.** See
  [Conversation ID lifecycle](#conversation-id-lifecycle) below.
- **Footgun guard.** We POST to `/api/conversation/process`, NOT
  `/api/services/conversation/process`. The latter returns no
  response body (HA core issues #93754 and #104122 — still live in
  2026).

**Public surface:**

```python
class HAClient:
    def __init__(self, url, token, *, agent_id=None, language="en",
                 verify_ssl=True, timeout=None, http=None, clock=None): ...

    async def process(self, query: str) -> HAResponse: ...
    async def healthcheck(self) -> bool: ...
    async def config(self) -> dict | None: ...
    async def list_agents(self) -> list[dict]: ...
    async def aclose(self) -> None: ...

    @property
    def url(self) -> str: ...
    @property
    def conversation_id(self) -> str | None: ...
```

**`HAResponse`:**

```python
@dataclass(frozen=True)
class HAResponse:
    speech: str                  # speak this verbatim
    success: bool                # response_type != "error" AND speech non-empty
    response_type: str           # action_done | query_answer | error | ""
    error_code: str | None       # no_intent_match | no_valid_targets | ...
    outcome: str                 # one of OUTCOME_OK / NETWORK / TIMEOUT / ...
    conversation_id: str | None  # canonical from HA's response
    continue_conversation: bool  # hint only — HA's heuristic is known-flaky
    targets_success: list[dict]
    targets_failed: list[dict]
    latency_ms: int
    error_detail: str            # short text for logging
```

## probe_status — the one-shot helper

[`jasper/home_assistant.py:probe_status`](../jasper/home_assistant.py)
is a stateless one-shot reachability probe used by three callers:

- `jasper-control`'s `/state.home_assistant` section
- `jasper-control`'s `/system/snapshot.home_assistant` section
- `jasper-doctor`'s `check_home_assistant`

It's distinct from `HAClient.process()` — no conversation state, no
per-call structured logging, and crucially **never touches
`/api/conversation/process`**, because that endpoint costs real money
on LLM-backed HA agents (OpenAI Conversation, Anthropic, etc.). It
only hits `GET /api/` and `GET /api/config`.

**Cached by default.** Results are cached process-globally for
`PROBE_CACHE_TTL_SEC = 15.0` seconds keyed by `(url, token)`. Without
the cache, the dashboard polling `/system/snapshot` every 5 s with
HA unreachable would block each poll for the full 5 s `HEALTH_TIMEOUT`,
hammering a dead URL. With the cache, one probe per 15 s is the
worst case. Pass `force=True` to bypass the cache when fresh ground
truth matters — `jasper-doctor` does this so its output reflects
state-at-invocation-time, not the last cached value.

**State-transition logging.** `probe_status` emits one log line per
`(configured, connected)` transition, not per call:

```
event=ha.reachable url=http://homeassistant.local:8123 instance=Home version=2026.5.1
event=ha.unreachable url=http://homeassistant.local:8123 error=Couldn't reach Home Assistant — check the URL and token.
```

That's the right signal for "when did HA go down?" diagnostics
without per-poll log noise.

Return shape:

```python
{
  "configured":    bool,           # url AND token both present
  "connected":     bool,           # GET /api/ returned 200 + sigil
  "url":           str,            # what we probed (normalized)
  "instance_name": str | None,     # from /api/config.location_name
  "version":       str | None,     # from /api/config.version
  "error":         str | None,     # short human-readable detail
}
```

## The tool

One tool, [`home_assistant(query: str)`](../jasper/tools/home_assistant.py),
that relays the user's utterance to `HAClient.process()` and returns
the parsed result. The tool's docstring is the description the model
sees — it teaches when to call (smart-home control, area-scoped
commands, scenes/scripts, sentence-trigger phrases, state queries)
and when NOT to call (weather, music, time, transit, timers,
calendar, email, general conversation). Conditional rules, per the
OpenAI Realtime Prompting Guide pattern that CLAUDE.md already cites
— absolute "do not" gets partial compliance; conditional rules stick.

`make_home_assistant_tools(ha)` returns `[]` when `ha is None`. So
when HA isn't configured, the tool isn't in the registry, the model
can't see it, and the model handles smart-home requests
conversationally ("smart-home control isn't set up on this speaker
— visit jts.local/ha"). Same gating pattern as
`make_bus_tools` and `make_subway_tools`.

## System prompt

Slotted into `SYSTEM_INSTRUCTION` in
[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) — two static
blocks, plus one dynamic addendum the prompt builder appends only
when HA isn't configured:

1. **When to call** (static, in the tools section): conditional
   rules for smart-home control + a positive list of phrase shapes
   the model should pass through unchanged.
2. **What to say after** (static, in the post-tool-return section):
   speak `spoken_response` verbatim on success; speak `error_detail`
   briefly on failure; don't add 'OK' or 'Done' on top of HA's own
   wording.
3. **Tool-unavailable nudge** (dynamic, only when `ha_configured=False`,
   built into the `_build_system_instruction` addendum next to the
   transit nudge): tells the model to say `'Smart-home control isn't
   set up yet — visit {hostname}/ha to enable it.'` and
   explicitly NOT to call any other tool. This guard exists because
   without it, real-world voice logs showed the model misrouting
   "turn on the bedroom lights" to `get_current_time` +
   `get_now_playing` — calling unrelated tools instead of
   recognising the request as smart-home-shaped-but-unavailable.
   `{hostname}` is `cfg.hostname` so multi-speaker households
   (`jts2.local`, `jts3.local`) see the speaker the user is actually
   talking to.

Provider-agnostic per CLAUDE.md. No mention of Gemini, OpenAI, or
Grok. Conditional ("skip the preamble when…") not absolute.

## Observability surfaces

### Structured logs

`HAClient.process()` emits one `event=ha.call` line per call:

```
event=ha.call outcome=ok response_type=action_done error_code=- speech_len=27 latency_ms=132 conv_id=01HX0001abc continue=false targets_success=1 targets_failed=0
event=ha.call outcome=auth status=401 latency_ms=12
event=ha.call outcome=network query_len=29 detail='ConnectError'
event=ha.call outcome=intent_miss response_type=error error_code=no_intent_match speech_len=68 latency_ms=89 conv_id=- continue=false targets_success=0 targets_failed=0
```

Filter via `jasper-trace.sh`:

```sh
bash scripts/jasper-trace.sh | grep "event=ha\."
```

### `/state.home_assistant` (port 8780)

Cross-daemon snapshot read by the dashboard, jasper-doctor, and any
HTTP client:

```sh
curl -s http://jts.local:8780/state | jq .home_assistant
```

```json
{
  "configured": true,
  "connected": true,
  "url": "http://homeassistant.local:8123",
  "instance_name": "Home",
  "version": "2026.5.1",
  "error": null
}
```

Fail-soft per the existing section pattern: probe runs in
`/state`'s asyncio.gather fan-out alongside camilla / airplay /
voice probes; an unconfigured or unreachable HA returns a clean
shape with `configured` / `connected` flags rather than breaking
the whole `/state` response.

### `/system/` dashboard card

The Home Assistant card on `http://jts.local/system/` polls
`/system/snapshot` every 5 seconds. Shows:

| Status                    | UI                             |
|---------------------------|--------------------------------|
| Not configured            | "Not configured"               |
| Configured + connected    | "✓ Connected" (green) + name/version |
| Configured + unreachable  | "✗ Unreachable" (red) + error  |

Plus the URL and a link to `/ha`.

### jasper-doctor

`check_home_assistant(cfg)` follows the skip-if-not-configured
pattern. Three states:

- Unset → `ok` with hint pointing at the wizard
- Configured + reachable → `ok` with instance name + version
- Configured + unreachable → `fail` with probe error + pointer to
  the wizard

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "Home Assistant"
```

## Conversation ID lifecycle

`HAClient` decides when to send a `conversation_id` based on HA's
`continue_conversation` response field plus a 4-minute idle TTL
(`CONVERSATION_ID_TTL_SEC = 240.0`, under HA's empirical ~5min
contract with safety margin).

- First call: no `conversation_id` sent. HA mints one.
- Subsequent call within 4 min AND prior response said
  `continue_conversation=true`: reuse the cached `conversation_id`.
- After 4 min idle OR `continue_conversation=false`: drop the cache.
- HA may rotate the ID silently in its response. Whatever it
  returns is canonical; we update our cache to match.

After a daemon restart (deploy, watchdog), the in-memory cache is
lost and the next call starts fresh. HA gracefully mints a new ID.
The household just loses the implicit "we were talking about
lights" context, which is expected and not worth persisting across
restarts.

## Agent ID selection

`agent_id` is a per-call override that routes the conversation to a
specific HA conversation entity instead of HA's UI-configured
default. The field is **undocumented in HA's REST API surface** but
functional — verified by reading
`homeassistant/components/conversation/http.py` (the validator
accepts it; `async_converse` passes it through).

Wizard exposes it as an advanced disclosure (default empty → HA's
default agent wins). Households use the override for cost / latency
trade-offs: e.g. cheap rule-based agent for JTS, LLM-backed agent
for the HA dashboard.

The field is a known-untested risk for schema-tightening. Test
[`test_agent_id_pass_through_when_set`](../tests/test_home_assistant.py)
asserts our code sends the field. If HA ever 4xxs on unknown fields,
CI catches it.

## Failure mode taxonomy

Six outcome buckets from `HAClient.process()`:

| Outcome       | Trigger                                       | User experience                              |
|---------------|-----------------------------------------------|----------------------------------------------|
| `ok`          | response_type != error AND speech non-empty   | Model speaks HA's response                   |
| `intent_miss` | response_type == "error"                      | Model speaks the error briefly               |
| `network`     | httpx.ConnectError, DNS, etc.                 | Model: "I can't reach Home Assistant"        |
| `timeout`     | httpx.TimeoutException                        | Model: "Home Assistant didn't respond"       |
| `auth`        | HTTP 401                                      | Model: "I'm not authorized — reconnect at…"  |
| `agent_error` | HTTP 5xx                                      | Model: "Home Assistant had an internal error"|
| `parse_error` | non-200 OR malformed body OR empty speech     | Model: "Home Assistant returned a response I couldn't understand" |

`no_valid_targets` is NOT a hard error. In multi-satellite homes,
another device may have answered the same utterance; HA's speech
text ("I couldn't find a device matching that") is still useful to
surface. We tag the outcome as `intent_miss` but the model gets the
text either way.

## Performance characteristics

**Quantified on a Pi 5 1GB:**

| State                                          | RAM cost                  | CPU per minute        |
|-----------------------------------------------|---------------------------|-----------------------|
| HA unconfigured                                | 0 (HAClient never built)  | 0                     |
| HA configured, daemon idle                     | ~30 KB (httpx pool)       | ~0                    |
| HA configured, voice session active            | +~5 KB per turn           | ~5ms per tool call    |
| HA configured + healthy, dashboard open        | +~80 KB transient         | ~10ms per 5s poll     |
| HA configured + unreachable, dashboard open    | +~80 KB transient         | ~5000ms per 15s poll   |

The unreachable-HA-with-dashboard-open scenario is the worst case.
With the 15s probe cache (see `probe_status` above), one probe per
15 seconds is the floor we can hit without changing the
architecture. The dashboard still updates other cards every 5
seconds — only the HA card pays the cache TTL.

## Resilience model

- **Voice loop never blocks on HA at startup.** HAClient is built
  lazily on first call. Daemon boots fine even when HA is down.
- **Tool errors propagate as natural text.** The model speaks the
  error; no exception escapes the tool dispatcher.
- **No retry on 5xx.** The conversation endpoint is non-idempotent;
  retrying "turn off the lights" could double-fire a script.
- **No background supervisor probing HA.** Decided against — HA
  isn't on JTS's wake-to-response critical path, so a continuous
  health probe would burn bandwidth + log noise for no real win.
  On-demand probing via `/state`, `/system/`, and doctor is
  enough.
- **HAClient.aclose() on daemon shutdown** so the httpx pool
  closes cleanly.
- **Wizard validation before persist.** Save never writes a broken
  config — `verify_sync()` runs against the live HA before the env
  file is touched. Invalid token → URL stays, token dropped, user
  retries.
- **Atomic env file writes** via `write_env_file` (tempfile +
  os.replace). A half-written `home_assistant.env` is impossible.

## Future work (v1.1+)

**Client-side MCP for cross-tool orchestration.** When a household
wants to issue "play jazz AND dim the bedroom lights" in a single
realtime turn, MCP becomes attractive — our model can dispatch
across HA tools and JTS tools in parallel. Note this would have to
be **client-side** MCP (JTS hosts the MCP client, talks to HA's
MCP server over the LAN), NOT remote MCP (OpenAI's edge connects
directly to HA, requiring HA to be publicly reachable). The
Gemini 3.1 Flash Live Preview mid-session restriction
([livekit/agents#5496](https://github.com/livekit/agents/issues/5496))
means tool lists are frozen at session start, so an
entity-exposure change during a session would need a restart.

**MCP `homeassistant://assist/context-snapshot` resource for
prompt augmentation.** Hybrid: use the conversation API for
execution AND pull the snapshot at session start to enrich the
system prompt with the household's exposed entity list. Lets the
model answer "is the front door locked?" without a tool round-trip.
Adds a polling path and is solving a problem we don't yet have.

**OAuth Device Flow (RFC 8628).** Accepted in
[home-assistant/architecture#1299](https://github.com/home-assistant/architecture/discussions/1299)
Jan 2026 but prerequisite PR
[core#161715](https://github.com/home-assistant/core/pull/161715)
was still open as of May 2026. Realistic ETA late 2026 / early
2027. Replaces LLAT paste with "scan QR / enter 8-digit code"
flow, much better UX for headless devices. Wizard would gain a
state 1.5 that bounces to HA's device-authorization endpoint.

**`switch-home-assistant.sh` laptop helper.** Mirrors the existing
`switch-voice-provider.sh` / `switch-wake-word.sh` family for
operators who want enable / disable / status / test from the
laptop without opening a browser.

**Voice-eval regression scenario.** A mocked-HA scenario at
`tests/voice_eval/regression/test_home_assistant.py` runs a
"turn on the bedroom lights" round-trip end-to-end through the
realtime provider. PASS_K=3 against Gemini is ~$0.075 per run;
manual nightly per the cost discipline.

## Manual smoke test (release checklist)

Required before each release that touches the HA path:

1. Open `http://jts.local/ha/` — wizard renders state 1
2. Click **Find Home Assistant** — at least one instance appears
   (or "No instances found" if the test HA is on a different
   subnet; manual URL fallback works)
3. Enter a real HA URL → wizard transitions to state 2
4. Paste a real LLAT → wizard transitions to state 3, status
   card shows instance name + version
5. Click **Test connection** → green "✓ Connected" appears
6. Open the **Conversation agent (advanced)** disclosure →
   picker populates with at least `conversation.home_assistant`
7. Say "Hey Jarvis, what time is it" — confirm get_current_time
   tool still fires (HA tool didn't break the registry)
8. Say "Hey Jarvis, turn on the [REAL DEVICE NAME]" — confirm
   the device toggles AND the model speaks HA's response
9. Say "Hey Jarvis, [HOUSEHOLD SENTENCE TRIGGER PHRASE]" — confirm
   the automation runs AND the model speaks the response
10. `curl -s http://jts.local:8780/state | jq .home_assistant` —
    `connected: true`, instance name correct
11. Open `http://jts.local/system/` — Home Assistant card shows
    green ✓ Connected with the right URL + version
12. `sudo /opt/jasper/.venv/bin/jasper-doctor | grep "Home Assistant"`
    — shows ok
13. Click **Disconnect** on the wizard → wizard returns to state 1
    AND the home_assistant tool stops being registered (verify via
    next "Hey Jarvis, turn on the lights" — model says smart home
    isn't set up)

## Troubleshooting cookbook

### Wake responds but smart-home commands don't fire

Check whether the tool is registered:

```sh
ssh pi@jts.local 'journalctl -u jasper-voice -n 200' | grep "home_assistant:"
```

Look for either `home_assistant: enabled url=...` (good) or
`home_assistant: disabled (set JASPER_HA_URL...)`. If disabled, the
wizard didn't write the env file or the daemon didn't restart;
re-run the wizard, confirm `restart_voice_daemon()` fired.

### Tool fires but HA reports "no matching device"

The model sent a query HA's NLU couldn't resolve. Either:

- Entity isn't exposed to Assist. In HA: Settings → Voice
  Assistants → Expose → enable the entity.
- Alias doesn't match the user's phrasing. Add aliases under the
  entity's more-info dialog.
- LLM-backed HA agent doesn't have entity context. Configure
  the agent in HA's settings; verify it sees the entity list.

### Tool fires but takes 10+ seconds

The household has set HA to an LLM-backed conversation agent
(OpenAI / Anthropic / Gemini-inside-HA). The realtime provider
pays for one LLM hop; HA pays for another. Switch HA's default
agent to the rule-based one in HA Settings, OR set
`JASPER_HA_AGENT_ID=conversation.home_assistant` in the wizard's
advanced disclosure to route JTS specifically to the fast path.

### `/system/` dashboard shows red ✗ Unreachable

Three buckets:

- **Auth.** Token revoked or rotated in HA. Visit
  `/ha/`, click Disconnect, re-paste a fresh LLAT.
- **Network.** HA host changed IP, moved subnets, or shut down.
  Visit `/ha/`, click "Use a different URL", re-discover.
- **TLS** (HTTPS HA installs). Self-signed cert isn't trusted. See
  the HTTPS section below if it's been added.

### "Hey Jarvis, bedroom medium" doesn't trigger my automation

Confirm the sentence trigger is configured in HA:

1. In HA, Settings → Automations → find your automation
2. Check the trigger is type "Sentence" with the phrase pattern
3. Try the phrase directly in HA's Assist UI — if it doesn't work
   there, it won't work via JTS (we're a relay through HA's
   pipeline)

Then check JTS:

```sh
ssh pi@jts.local 'journalctl -u jasper-voice -n 50' | grep "event=ha\.call"
```

Look for the actual `query=` the model sent. If it's not your exact
phrase, the model may have paraphrased — the system prompt explicitly
says to pass household phrases verbatim, but realtime LLMs sometimes
drift. File a regression scenario.

### Daemon restarts in a loop after save

Most likely: the saved URL is malformed (rare; `_normalize_url`
should reject) or the token is corrupt. Check:

```sh
ssh pi@jts.local 'sudo cat /var/lib/jasper/home_assistant.env'
```

Then either re-save via the wizard or `sudo rm /var/lib/jasper/home_assistant.env`
to fully reset.

## Sources

Primary sources informing this work (cite in PRs / future ADRs):

- HA Conversation API: [developers.home-assistant.io/docs/intent_conversation_api](https://developers.home-assistant.io/docs/intent_conversation_api/)
- HA Conversation integration: [home-assistant.io/integrations/conversation](https://www.home-assistant.io/integrations/conversation/)
- HA MCP Server integration: [home-assistant.io/integrations/mcp_server](https://www.home-assistant.io/integrations/mcp_server/)
- HA LLM API (developer docs): [developers.home-assistant.io/docs/core/llm](https://developers.home-assistant.io/docs/core/llm/)
- Built-in intents: [developers.home-assistant.io/docs/intent_builtin](https://developers.home-assistant.io/docs/intent_builtin/)
- Voice Chapter 10 (June 2025): [home-assistant.io/blog/2025/06/25/voice-chapter-10](https://www.home-assistant.io/blog/2025/06/25/voice-chapter-10/)
- Voice Chapter 11 (Oct 2025): [home-assistant.io/blog/2025/10/22/voice-chapter-11](https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/)
- HA 2026.4 thinking-steps UI: [home-assistant.io/blog/2026/04/01/release-20264](https://www.home-assistant.io/blog/2026/04/01/release-20264/)
- OAuth Device Flow proposal: [github.com/home-assistant/architecture/discussions/1299](https://github.com/home-assistant/architecture/discussions/1299)
- Gemini 3.1 Live mid-session restriction: [livekit/agents#5496](https://github.com/livekit/agents/issues/5496)
- HA services/conversation/process bug: [home-assistant/core#93754](https://github.com/home-assistant/core/issues/93754), [home-assistant/core#104122](https://github.com/home-assistant/core/issues/104122)
- LLAT-too-long thread: [community.home-assistant.io/t/543626](https://community.home-assistant.io/t/long-lived-access-token-too-long/543626)
- "WTH are all new entities exposed" thread: [community.home-assistant.io/t/803889](https://community.home-assistant.io/t/wth-are-all-new-entities-exposed-to-assist-by-default/803889)

Last verified: 2026-05-27 (footer/status check; Home Assistant code
paths not changed in this PR)
