# Home Assistant integration — design record (2026-05)

> **Archived research and design detail, not current guidance.** The
> operational reference is [HANDOFF-homeassistant.md](../HANDOFF-homeassistant.md).
> The two load-bearing decisions here became
> [ADR-0116](../adr/0116-smart-home-relays-through-has-conversation-api.md)
> (conversation API, not MCP) and
> [ADR-0117](../adr/0117-consequential-actions-confirm-only-inside-the-taint-window.md)
> (the confirmation gate). This file keeps the primary-source reading behind
> them, the wizard walkthrough, the pre-child-cache performance numbers, the
> release smoke test, the v1.1 options, and the sources.

## The surface comparison, as read in 2026-05


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

## End-to-end request flow, in full

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

## The confirmation gate, as designed (prompt-injection defense)

HA is the only tool on this speaker that performs a real-world action,
which makes it the confused-deputy target: untrusted content the model
has read (an email body, a device name) could steer it into
`home_assistant("unlock the front door")` with no human intent. The
durable control for that — per OWASP LLM01 "human oversight for
high-risk operations" and the agent-security design-patterns literature
(sources in [HANDOFF-prompting.md](../HANDOFF-prompting.md) "Untrusted
tool-result fencing") — is **least-privilege + confirmation on
consequential actions**, not fencing HA's reply text.

How it works, in [`jasper/tools/home_assistant.py`](../../jasper/tools/home_assistant.py):

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
  in [`jasper/tools/__init__.py`](../../jasper/tools/__init__.py)).
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
in [HANDOFF-prompting.md](../HANDOFF-prompting.md). A pending confirmation is
also bounded in time, not just by context: the tool can't see intervening
turns, so a stale "yes" much later could in theory fire an abandoned
action — `_ConfirmationStore` mitigates this two ways (a ~90 s TTL, and
`clear()` whenever a *different* home_assistant command supersedes the
pending), but a long-delayed bare "yes" inside the window is the residual.
Observability: `event=ha.confirm_gate` / `event=ha.confirm_execute`
(action label only, never the utterance); a consequential action that ran
*without* asking (clean session) logs `event=ha.consequential_direct` at
DEBUG.

## Setup walkthrough

Visit `http://jts.local/ha/` from any device on the LAN.
The wizard is socket-activated (idle-exits after 10 min of no
requests; first request takes ~500 ms to cold-start), so it costs
zero RAM when nobody's using it.

The page is a three-state form driven by what's in
`/var/lib/jasper-intsecrets/home_assistant.env`:

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

## Performance characteristics (pre-child-cache, superseded)

**Quantified on a Pi 5 1GB before the jasper-control child-cache change:**

| State                                          | RAM cost                  | CPU per minute        |
|-----------------------------------------------|---------------------------|-----------------------|
| HA unconfigured                                | 0 (HAClient never built)  | 0                     |
| HA configured, daemon idle                     | ~30 KB (httpx pool)       | ~0                    |
| HA configured, voice session active            | +~5 KB per turn           | ~5ms per tool call    |
| HA configured + healthy, dashboard open        | superseded; remeasure     | child refresh on TTL/env change |
| HA configured + unreachable, dashboard open    | superseded; remeasure     | child timeout bounded by cache |

The unreachable-HA-with-dashboard-open scenario used to be the worst case.
`/state` and `/system/snapshot` now start a child process for stale HA status
and return the rest of their payloads immediately. The parent no longer
retains the HA/httpx probe graph for these status surfaces, but the child
process's transient peak RSS still needs Pi-side measurement before replacing
the table with fresh numbers.

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

---

Archived 2026-08-26 from `docs/HANDOFF-homeassistant.md`. Dates, version
numbers, and upstream issue states inside are as-written at the time.
