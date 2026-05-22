# HANDOFF — voice provider abstraction

The voice loop runs against any of three real-time speech-to-speech APIs
behind a single `JASPER_VOICE_PROVIDER` env var. This doc explains the
architecture, the per-provider trade-offs, and the contract a future
fourth backend would need to honour.

## TL;DR

Three ways to switch backends, any of them work:

```sh
# 1. Web UI — open the speaker's web settings, paste keys, pick a
#    provider in the radio group, hit save. The page is nginx-routed
#    on the same host that serves /spotify/.
http://jts.local/voice/

# 2. Helper script (laptop → Pi over SSH):
bash scripts/switch-voice-provider.sh openai

# 3. Edit /var/lib/jasper/voice_provider.env directly on the Pi:
JASPER_VOICE_PROVIDER=gemini   # gemini-3.1-flash-live-preview
JASPER_VOICE_PROVIDER=openai   # gpt-realtime-2 (released 2026-05-07)
JASPER_VOICE_PROVIDER=grok     # grok-voice-think-fast-1.0
```

`JASPER_VOICE_PROVIDER` lives in **exactly one file** since PR #166:
`/var/lib/jasper/voice_provider.env`. The web UI writes it;
`jasper-voice.service` sources it via `EnvironmentFile=`. `install.sh`
actively migrates any stale value out of `/etc/jasper/jasper.env`
on each run — having a default in BOTH led to stale-vs-runtime
confusion. There is **no fallback default**: fresh installs leave
the variable unset and `jasper-voice` refuses to start until the
wizard writes one. Same pattern as `/spotify/` writes
`spotify_credentials.env`. Implementation:
[`jasper/web/voice_setup.py`](../jasper/web/voice_setup.py).

The abstraction lives in [`jasper/voice/session.py`](../jasper/voice/session.py)
as the `LiveConnection` and `LiveTurn` Protocols. Daemon code at
[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) speaks only to
those interfaces; the per-provider adapters are:

- [`jasper/voice/gemini_session.py`](../jasper/voice/gemini_session.py) — `GeminiLiveConnection`
- [`jasper/voice/openai_session.py`](../jasper/voice/openai_session.py) — `OpenAIRealtimeConnection`
- [`jasper/voice/grok_session.py`](../jasper/voice/grok_session.py) — `GrokRealtimeConnection` (subclass of the OpenAI adapter)

The single switch point is `_make_connection(cfg)` at the top of
[`voice_daemon.py`](../jasper/voice_daemon.py).

## Why three, not one

Each backend has a real strength and at least one real cost:

| Provider | Strengths | Costs |
|---|---|---|
| **Gemini Live** (gemini-3.1-flash-live-preview / gemini-2.5-flash-native-audio) | Cheapest by ~5×; mature 24-language voice catalogue; session resumption (2 h handle); the existing Jasper deployment runs on it | Sequential tool calls only on 3.1; occasional silent-session-2 failures requiring a fall-back to `2.5-flash-native-audio-preview-12-2025`; 15-min audio cap on a single session |
| **OpenAI Realtime** (gpt-realtime-2, GA 2026-05-07) | Reasoning levels (minimal/low/medium/high/xhigh); 128K context; multi-tool-at-once; image input; MCP; SIP; arguably tightest tool/instruction following | $32/$64/$0.40 per 1M tokens — about 5× Gemini per minute; 60-min hard session cap with NO resumption; PCM-input only at 24 kHz (we upsample 16 kHz mic) |
| **xAI Grok** (grok-voice-think-fast-1.0) | Sub-second TTFA; flat $3/hour billing (cheapest at sustained chat); first-class web/x/file/MCP search built-ins; OpenAI-protocol-compatible so it rides the same adapter | Token-based spend cap under-counts (logs a warning at startup); voice catalogue is disjoint from OpenAI's (eve / ara / rex / sal / leo); fewer guarantees on event-shape stability — xAI documents one rename today (`response.text.delta` → `response.output_text.delta`) and we normalise it in `grok_session.py` |

Anthropic is **not** on the list. As of 2026-05-09 there is no public
real-time speech-to-speech API from Anthropic — only push-to-talk Voice
Mode in the consumer apps and dictation in Claude Code.

## Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │             jasper/voice_daemon.py             │
                    │  WakeLoop → acquire_turn → send_audio →        │
                    │  end_input → audio_out → release               │
                    └────────────────────┬──────────────────────────┘
                                         │   speaks only to:
                            ┌────────────▼─────────────┐
                            │ jasper/voice/session.py  │
                            │   LiveConnection (ABC)   │
                            │   LiveTurn       (ABC)   │
                            └────────────┬─────────────┘
                                         │
       ┌──────────────────┬──────────────┴──────────────┬─────────────────┐
       │                  │                             │                 │
       ▼                  ▼                             ▼                 ▼
  GeminiLive        OpenAIRealtime              GrokRealtime         <future>
  Connection         Connection                 Connection           …
  (Google SDK)     (openai>=2.36 SDK)        (subclass +
                                              base URL swap)
       │                  │                             │
       └──────┬───────────┘                             │
              │                                         │
              ▼                                         ▼
     jasper/voice/_supervisor.py                Same module — Grok
       FailureFingerprint                       inherits the supervisor
       reconnect_backoff_delay                  unchanged.
       ESCALATION_* constants
```

### Shared between providers

- **Reconnect supervisor primitives** (`_supervisor.py`): exponential
  backoff with ±25% jitter, tight-retry-loop escalation cue at 5
  consecutive identical failures (rate-limited to 1/hour). Used by
  every adapter so the user-facing failure UX is consistent.
- **Tool registry** ([`jasper/tools/__init__.py`](../jasper/tools/__init__.py)):
  one tool definition, two serializers (`function_declarations()` for
  Gemini, `openai_tools()` for OpenAI/Grok). Tools may opt in to a
  subset of providers via `@tool(providers={"openai"})` — hidden tools
  are filtered out of the per-provider declaration list, so a model
  literally can't see what it can't call.
- **Audible feedback cues** ([`jasper/cues/registry.py`](../jasper/cues/registry.py)):
  the same three slugs (`spend_cap_reached`, `cant_connect`,
  `cant_reach_cloud`) cover every provider's failure modes. Cue text
  is provider-agnostic by design — no "Google" or "Gemini" or
  "OpenAI" mentions ever bake into the audio.
- **Spend-cap pricing** ([`jasper/usage.py`](../jasper/usage.py)):
  `pricing_for_provider(provider, model=...)` returns a `Pricing`
  snapshot; `UsageStore` accepts it on construction and applies it at
  session-close time. Switching providers mid-day naturally
  aggregates — older sessions retain whichever pricing was active
  when they closed.

### Provider-specific in each adapter

- **Wire format**. Gemini speaks Google's `BidiGenerateContent*`
  envelopes; OpenAI/Grok speak OpenAI's `session.update` /
  `input_audio_buffer.*` / `response.*` event grammar. Each adapter
  hides its protocol from the daemon.
- **Audio rate**. Gemini accepts 16 kHz PCM directly (matches the XVF
  chip's native rate). OpenAI/Grok accept ONLY 24 kHz on `audio/pcm`
  — `openai_session.py` upsamples 16→24 kHz with `audioop.ratecv`
  inside the turn's `send_audio` so the rest of the daemon stays at
  16 kHz everywhere.
- **Manual VAD signalling**. Both Gemini and OpenAI run with manual
  VAD, but the markers differ: Gemini sends `activity_start` /
  `activity_end` realtime-input events; OpenAI sends
  `input_audio_buffer.commit` followed by `response.create`. The
  `LiveTurn.end_input()` method abstracts this — daemon code is
  identical.
- **Lifecycle**. Gemini's connection has a 15-min audio cap with a
  2-hour resumption handle (`session_resumption_update.new_handle`).
  OpenAI has a 60-min hard cap with no resumption. The `_supervisor`
  primitives are shared, but the per-provider supervisor loops handle
  these specifics differently — Gemini drops a stale handle on
  certain failures, OpenAI just reconnects.
- **Pricing**. See `Pricing` in `jasper/usage.py`.

## Adding a fourth provider

When a new real-time backend lands (a self-hostable Ultravox-class
model, Mistral Voxtral, future Anthropic, etc.), the integration
should be:

1. New module `jasper/voice/<provider>_session.py` with a class
   implementing `LiveConnection` (and a corresponding `LiveTurn`).
2. New `Pricing` row in `jasper/usage.py` and an entry in
   `pricing_for_provider`.
3. New env-var block in `Config` (api key, model, voice, anything
   provider-specific) with a sane default and an explicit
   "required only when active provider" validation.
4. New branch in `_make_connection(cfg)` in `voice_daemon.py`.
5. New contract test in `tests/test_<provider>_session.py` modeled on
   `tests/test_openai_session.py`. Pin: connect → tool round-trip →
   reconnect → manual-VAD payload shape → tool round advances the
   turn's idle anchor (see "Idle anchor + tool rounds" below).
6. New row in this doc's tradeoff table.

If the wire format is OpenAI-Realtime-compatible (Grok pattern), most
of step 1 is "subclass `OpenAIRealtimeConnection` and override
`PROVIDER_NAME` / base URL / event-name normalisations". Otherwise, the
Gemini adapter is the better template — it shows the full state
machine, supervisor loop, idle context-reset, and tool dispatch in one
place.

### Idle anchor + tool rounds

The daemon's pre-response idle watchdog
(`jasper/voice_daemon.py:_idle_watchdog`) reads `turn.last_activity_at()`
and abandons the turn if `idle_for > JASPER_IDLE_TIMEOUT_SEC` *and* no
audio has been received yet. The watchdog is protocol-agnostic — all
adapters share this one timer.

That makes the turn class's idle anchor a cross-provider contract:
**any event from the server that means "model is still working" must
advance the anchor**, not just audio deltas and the final
`response.done`. In particular, a tool-call response.done (or
equivalent) starts a multi-second round trip (client → tool → response
2) during which no audio arrives. Forget to reset and the watchdog
fires mid-dispatch at small `JASPER_IDLE_TIMEOUT_SEC` (production runs
10 s). Production hit this on 2026-05-21: a weather-tool turn ended
~0.6 s after the tool result was sent, with the orphan-response
warning logging 48 dropped audio tokens.

Adapter wiring today:
- **OpenAI** (`openai_session.py`): `OpenAIRealtimeTurn._note_activity()`
  is called from the function_calls branch of `_handle_response_done`
  and from `_on_response_done`. Grok inherits this path verbatim.
- **Gemini** (`gemini_session.py`): `GeminiLiveTurn._note_activity()`
  is called on tool_call arrival in `_on_response`, inside the
  per-tool loop in `_handle_tool_call`, and once more after
  `send_tool_response` lands. Mirrors OpenAI's coverage — every
  tool-round milestone resets the anchor.

New providers should either expose a `_note_activity()` (or
equivalent) and call it on every tool-round server event, or document
why they don't need one (e.g. the wire format streams a heartbeat
that satisfies the anchor naturally).

## Anti-patterns

These have all been surfaced and rejected in design reviews:

- **Don't auto-fall-back across providers**. If `gemini` errors,
  the daemon plays `cant_reach_cloud` and stays on `gemini`. Cross-
  provider auto-failover hides bugs (silent-session-2 on Gemini 3.x)
  and surprises the user — different voice, different latency,
  different conversational style mid-conversation.
- **Don't add cue text per provider**. Cues are pre-rendered Gemini
  TTS WAVs and never mention which backend is behind the failure. The
  user cares that "I can't reach the cloud", not which cloud.
- **Don't share the supervisor LOOP across providers**. The
  primitives (backoff, fingerprint) are shared; the loop body differs
  enough (handle drop on Gemini, no handle on OpenAI) that abstracting
  it has consistently produced bigger diffs than two parallel loops.
- **Don't make tools fully neutral by default**. The default is
  visible-to-everyone, which is right for our subsystem-call tools.
  If a future tool needs OpenAI's image input or Gemini's grounding,
  tag it explicitly: `@tool(providers={"openai"})`. The model on
  another provider then literally cannot see or call it — the safest
  failure mode.

## Related docs

- [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md) — Gemini-side reconnect supervisor + idle context reset
- [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md) — the cue subsystem, including the pre-rendered TTS used by all providers
- [audio-paths.md](audio-paths.md) — why TTS bypasses CamillaDSP and how the dongle dmix sums TTS + music
