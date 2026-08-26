# HANDOFF — voice provider abstraction

The voice loop runs against any of three realtime speech-to-speech APIs
behind a single `JASPER_VOICE_PROVIDER`. This doc owns the architecture, the
per-provider trade-offs, and the contract a fourth backend must honour.

Decisions with their rationale — read the one that covers what you are about
to change: [no cross-provider
failover](adr/0159-a-provider-failure-never-falls-back-to-another-provider.md)
· [catalog
policy](adr/0160-the-model-catalog-is-curated-metadata-not-a-runtime-allow-list.md)
· [pricing is data](adr/0161-an-unpriced-model-costs-zero-and-says-so.md)
· [the idle anchor](adr/0162-the-pre-response-idle-anchor-stays-turn-open.md)
· [provider env
ownership](adr/0165-the-active-voice-provider-lives-in-one-file-and-unconfigured-parks.md)
· [resumption
handles](adr/0166-a-resumption-handle-is-dropped-on-the-first-failure.md).
The 2026-05 brief that scoped the persistent-session rework is archived at
[historical/persistent-live-session-rework-2026-05.md](historical/persistent-live-session-rework-2026-05.md).

## Switching providers

Three ways, any of them work:

```sh
# 1. Web UI: paste keys, pick a provider, save.
http://jts.local/voice/

# 2. Helper script (laptop -> Pi over SSH):
bash scripts/switch-voice-provider.sh openai

# 3. Edit the wizard-owned env file on the Pi:
#    /var/lib/jasper/voice_provider.env
JASPER_VOICE_PROVIDER=gemini   # gemini-3.1-flash-live-preview
JASPER_VOICE_PROVIDER=openai   # gpt-realtime-2
JASPER_VOICE_PROVIDER=grok     # grok-voice-think-fast-1.0
```

`JASPER_VOICE_PROVIDER` lives in **exactly one file**:
`/var/lib/jasper/voice_provider.env`. There is no fallback default, and an
unconfigured speaker parks instead of crash-looping —
[ADR-0165](adr/0165-the-active-voice-provider-lives-in-one-file-and-unconfigured-parks.md)
carries the rule and its rationale. What that means in practice:

- The `/voice/` wizard writes the file
  ([`jasper/web/voice_setup.py`](../jasper/web/voice_setup.py));
  `jasper-voice.service` sources it via `EnvironmentFile=`; `install.sh`
  migrates any stale value out of `/etc/jasper/jasper.env` on each run.
- Unset → `Config.from_env` raises `VoiceProviderNotConfigured`,
  `jasper-voice` exits `EX_CONFIG` (78), and the unit's
  `SuccessExitStatus=78` + `RestartPreventExitStatus=78` keep that out of the
  crash budget. Real crashes still use `Restart=on-failure` and the existing
  `StartLimitAction=reboot` path.
- The pre-daemon reconciler has its own fail-closed projection:
  [`deploy/install.sh`](../deploy/install.sh) renders
  `/var/lib/jasper/voice_provider_ids` from
  `jasper.voice.catalog.provider_ids_manifest_text()`, and
  [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile)
  accepts a provider only when it is an exact line in that file. That file is
  an allow-list projection, never a second home for the active value.
- Operator and diagnostic surfaces consume the catalog rather than mirroring
  provider IDs: `switch-voice-provider.sh` reads the installed runtime
  catalog; `jasper-doctor` derives the key check from it and verifies
  `voice_provider_ids` is in sync; `switch-gemini-model.sh` resolves its
  `3.1` / `2.5` aliases from the Gemini catalog entry and refuses to act when
  that contract is missing, ambiguous, or malformed.
- Everything that is not `jasper-voice` (`/state`, the `/system/` dashboard)
  reads through
  [`jasper/voice/provider_state.py`](../jasper/voice/provider_state.py),
  which re-reads the file fresh — never `os.environ`, frozen at daemon start.
  It returns `""` for unset or invalid, never a guess;
  `read_active_provider_state()` distinguishes configured / unset / missing /
  unreadable / invalid so a permission-denied probe is not reported as
  first-time setup.

## Model catalog policy

[`jasper/voice/catalog.py`](../jasper/voice/catalog.py) is the curated
catalog the `/voice/` wizard reads for provider, model, voice, and
provider-specific knob metadata. Each visible model is labelled `tested`,
`fallback`, or `experimental`. Runtime `Config` reads provider model, voice,
and extra-control defaults from the same helpers; env overrides still win.

The catalog is **not** a runtime allow-list (ADR-0160): adapters pass
whatever `JASPER_<PROVIDER>_MODEL` is configured through to the SDK, and the
wizard preserves unknown values as custom experimental rows.

Every provider network call from `/voice/` is behind an explicit button.
**Refresh available models** is the only discovery path — never on page
render, and only with a configured key;
[`jasper/voice/model_discovery.py`](../jasper/voice/model_discovery.py)
writes `/var/lib/jasper/voice_model_discovery.json` at mode 0600 and the next
render appends unknown IDs as `experimental; discovered`. **Save and Test**
makes exactly one bounded TTS request to seed the loudness profile; plain
**Save and restart voice** never calls a provider TTS API, and daemon-start
seeding is off unless `JASPER_ASSISTANT_LOUDNESS_AUTO_SEED=1`. The invariants
behind those rules: no page-load provider calls · no implicit paid
calibration calls · no auto-promotion of discovered models · refresh never
changes `JASPER_<PROVIDER>_MODEL` · a failed refresh keeps the last
successful list and records a sanitized error that never leaks key-bearing
URLs.

## Why three, not one

| Provider | Strengths | Costs |
|---|---|---|
| **Gemini Live** (gemini-3.1-flash-live-preview / 2.5-flash-native-audio) | Cheapest by ~5×; mature 24-language voice catalogue; session resumption (2 h handle); the existing deployment runs on it | Sequential tool calls only on 3.1; occasional silent-session failures needing the 2.5 fallback; 15-min audio cap on a single session |
| **OpenAI Realtime** (gpt-realtime-2) | Reasoning levels; 128K context; multi-tool-at-once; image input; MCP; SIP; tightest tool/instruction following | ~5× Gemini per minute; 60-min hard session cap with NO resumption; PCM-input only at 24 kHz (we upsample the 16 kHz mic) |
| **xAI Grok** (grok-voice-think-fast-1.0) | Sub-second TTFA; flat $/hour realtime billing (cheapest at sustained active chat); first-class web/x/file/MCP search built-ins; OpenAI-protocol-compatible so it rides the same adapter | Billed on active realtime duration, not tokens, so it is metered separately by `BillableActivityMeter`; voice catalogue disjoint from OpenAI's; fewer guarantees on event-shape stability (xAI documents one rename, normalised in `grok_session.py`) |

Anthropic is not on the list: there is no public realtime
speech-to-speech API to integrate.

## Architecture

```
  jasper/voice_daemon.py + voice/*.py
  WakeLoop → acquire_turn → send_audio → end_input → audio_out → release
                     │  speaks only to:
       jasper/voice/session.py — LiveConnection (ABC), LiveTurn (ABC)
                     │
   ┌─────────────────┼──────────────────┬──────────────────┐
   ▼                 ▼                  ▼                  ▼
 GeminiLive     OpenAIRealtime     GrokRealtime         <future>
 Connection      Connection         Connection
 (Google SDK)    (openai SDK)   (subclass + base URL swap)
   │                 │                  │
   └───────── jasper/voice/_supervisor.py ─────────┘
     FailureFingerprint · ESCALATION_* — shared primitives,
     one supervisor loop per adapter (Grok inherits OpenAI's).
```

The single switch point is `_make_connection(cfg)` in
[`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py). Session
preprocessing is resolved through
[`jasper/voice/input_policy.py`](../jasper/voice/input_policy.py), which
turns the applied mic/AEC runtime config into an input-audio contract before
OpenAI/Grok wire-format fields are chosen.

Two capabilities compose *below* the provider branch rather than branching on
provider name. **Conversation-history capture** is an optional turn
capability: providers that receive text transcripts expose
`ConversationTranscriptTurn.user_transcript()` / `assistant_transcript()`,
which WakeLoop probes at teardown and writes through the daemon-held
`ConversationStore` only when the opt-in gate is on; a provider without
native transcripts may omit the capability or expose
`ConversationMetadataTurn.conversation_metadata()` for bounded metadata, and
`/chat/` renders the missing side honestly. **Wake-funnel response/tool
telemetry** composes in the shared `_play_responses()` drain and the single
`dispatch_tool()` seam, via a lifecycle observer injected into `ToolRegistry`
and capped at 100 ms so telemetry cannot wedge speech
([HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md)).

Daemon-initiated confirmation windows use the provider-neutral
`LiveTurn.send_text_context()` hook: it adds a text-only routing instruction
to an already-acquired turn without asking the provider to generate, so the
normal user-audio VAD path still decides whether to commit input.

### Shared between providers

- **Reconnect supervisor primitives** (`_supervisor.py`): failure-shape
  fingerprints and tight-retry-loop escalation. The generic retry schedule
  lives in `jasper.backoff`.
- **Tool registry** ([`jasper/tools/__init__.py`](../jasper/tools/__init__.py)):
  one tool definition, two serializers — `function_declarations()` for
  Gemini, `openai_tools()` for OpenAI/Grok. A tool may opt into a subset of
  providers via `@tool(providers={"openai"})`; hidden tools are filtered out
  of the declaration list, so the model literally cannot see what it cannot
  call.
- **Audible feedback cues** ([`jasper/cues/registry.py`](../jasper/cues/registry.py)):
  shared slugs cover provider-independent failure modes, and cue text never
  names a backend.
- **Spend-cap pricing** ([`jasper/usage.py`](../jasper/usage.py)):
  `pricing_for_model(model_id, overrides=…)` returns a `Pricing` snapshot
  keyed by exact model ID — there is no provider-level price, and an unknown
  model is unpriced rather than guessed (ADR-0161). Rates ship dated as data;
  the `/voice` editor and `JASPER_PRICING_FILE` overlay them
  ([HANDOFF-pricing-editor.md](HANDOFF-pricing-editor.md)). Three
  provider-shaped facts the adapters are responsible for: per-turn usage is
  **normalised** (OpenAI reports per-response deltas, Gemini a counter
  cumulative for the WebSocket's lifetime, so `GeminiLiveTurn` subtracts the
  turn-start baseline and `SUM()` does not multi-count); **Grok is metered by
  active turn time**, so its token rows price to $0, `BillableActivityMeter`
  records billable intervals while a turn is active, and idle warm-socket
  time is deliberately uncounted (xAI's dashboard stays the billing truth;
  ours is a conservative circuit-breaker estimate); and the cap is a
  **household** number that sums the voice and tuning ledgers, so a voice
  session refuses once the tuning assistant has spent the shared daily budget
  ([HANDOFF-calibration-agent.md](HANDOFF-calibration-agent.md) "Cost
  discipline").

### Provider-specific in each adapter

- **Wire format.** Gemini speaks `BidiGenerateContent*`; OpenAI/Grok speak
  `session.update` / `input_audio_buffer.*` / `response.*`. Each adapter
  hides its protocol from the daemon.
- **Audio rate.** Gemini accepts 16 kHz PCM directly (the XVF chip's native
  rate). OpenAI/Grok accept only 24 kHz, so `openai_session.py` upsamples
  inside the turn's `send_audio` and the rest of the daemon stays at 16 kHz.
- **Provider preprocessing policy.** OpenAI's input `noise_reduction` is a
  provider-side transform, not a generic smart-speaker default.
  `JASPER_OPENAI_NOISE_REDUCTION` defaults to `auto`, resolved by
  `input_policy.py` from the effective input contract: already-processed
  profiles (`xvf_chip_aec`, `xvf_software_aec3`) omit provider denoising, raw
  direct mics use `far_field`, and explicit values remain operator overrides.
  The resolved policy logs as `event=voice.input_policy`, with a warning on
  suspicious combinations such as explicit `far_field` on an
  already-processed stream.
- **Manual VAD signalling.** Both Gemini and OpenAI run manual VAD with
  different markers — Gemini `activity_start`/`activity_end`, OpenAI
  `input_audio_buffer.commit` then `response.create`. `LiveTurn.end_input()`
  abstracts it; daemon code is identical.
- **Lifecycle.** Gemini: 15-min audio cap with a 2-hour resumption handle
  (`session_resumption_update.new_handle`). OpenAI: 60-min hard cap, no
  resumption. The supervisor primitives are shared; the loops are not (see
  Anti-patterns).
- **Server-side VAD capability.** OpenAI and Grok support mid-session
  switching to `server_vad`; Gemini's `automatic_activity_detection` is fixed
  at connect time. `_begin_turn` checks `connection.supports_server_vad()`
  rather than branching on provider name, and supporting adapters implement
  the public `set_turn_detection()` / `create_response_only()` /
  `mark_server_vad()` / `server_speech_started()` / `wait_for_server_eou()`
  hooks. Production defaults to local Silero
  (`JASPER_SERVER_VAD_ENABLED=0`) because the May 2026 A/B matrix found
  server VAD cut off real utterances —
  [HANDOFF-vad-experiments.md](HANDOFF-vad-experiments.md).

### Reconnect supervisor and idle context reset

Each adapter runs its own supervisor loop; `_supervisor.py` holds only the
shared primitives. Four behaviours a maintainer touching a session module
should know:

- **The resumption handle is dropped on the first failure of any kind**
  ([ADR-0166](adr/0166-a-resumption-handle-is-dropped-on-the-first-failure.md)):
  one turn of context continuity traded for never looping on a
  server-invalidated handle.
- **Tight-retry-loop escalation.** A 5-deep `FailureFingerprint`
  (exception type, close code, reason) ring buffer records the shape of each
  reconnect failure and clears on success. When all five match, a
  fire-and-forget callback plays the proactive `cant_reach_cloud` cue,
  rate-limited to once per hour (`ESCALATION_*` in `_supervisor.py`; wired in
  `daemon_main.py`'s `run()`). The supervisor never gives up — the user just
  finds out audibly instead of silently.
- **Backoff shift saturation.** The exponent saturates because the supervisor
  retries forever and an unbounded `2 ** shift` overflows; the outer clamp
  makes it a numeric safety bound only.
- **Idle context reset is opt-in and off.**
  `JASPER_OPENAI_CONTEXT_RESET_SEC` / `JASPER_GEMINI_CONTEXT_RESET_SEC` /
  `JASPER_GROK_CONTEXT_RESET_SEC` all default to `0`, with the legacy
  `JASPER_LIVE_CONTEXT_RESET_SEC` as a global fallback when set. It shipped
  at 300 s on the theory that stale context bleeds across hours; the terse
  system prompt makes that hypothetical while the costs are real — each reset
  busts the OpenAI prompt cache and blocks the wake event for seconds. Set a
  positive value only as a hedge if stale-context glitches actually appear.

## Provider interruption contract

Verified against provider docs 2026-06-09.

| Provider | Native behavior | JTS adapter obligation |
| --- | --- | --- |
| OpenAI Realtime | VAD can cancel an in-progress response; with WebSocket playback the client must stop playback, measure what played, and send `conversation.item.truncate`. `response.cancel` covers manual paths. | Local TTS flush first, then the playout ledger's item id + `audio_played_ms` drive `conversation.item.truncate`; `response.cancel` for explicit cancellation. |
| Gemini Live | `START_OF_ACTIVITY_INTERRUPTS` is the default; start of user activity cuts off the response, and interrupted turns are reported. | Treat interruption as provider-side generation state only. Still flush local TTS — Gemini cannot see the DAC queue depth. No item-truncation call to synthesize. |
| xAI Grok Voice | Exposes OpenAI-style `server_vad`, speech events, `conversation.item.truncate`, and `response.cancel`. | Reuse the OpenAI shape where event support is confirmed, keeping feature probes for xAI's provider-specific event-name differences. |

The provider-neutral seam is **capability-based, not provider-name-based**,
and lives on `LiveTurn` in
[`jasper/voice/session.py`](../jasper/voice/session.py):
`request_local_interrupt()` (local flush only — it never cancels or truncates
the provider), `cancel_response(reason)`,
`truncate_assistant_audio(provider_item_id, audio_played_ms)`, and the
optional getattr-probed `drop_pending_audio()` for adapters with an internal
playout buffer. Two adapter obligations are easy to get wrong: truncation is
a **no-op plus WARN when played-ms is 0** (an out-of-range `audio_end_ms`
errors server-side and desyncs context, so never truncate on
bytes-received), and adapters must tolerate a missing provider item id —
Gemini has none today. The packs themselves are owned by
[HANDOFF-barge-in.md](HANDOFF-barge-in.md); the whole surface is default-OFF
behind `JASPER_BARGE_IN_<PROVIDER>`.

Which reconciliation a provider needs is a **declarative registry field**:
`ProviderCatalogEntry.interrupt_reconcile` (`needs_client_truncate` for
OpenAI, `server_self_truncates` for Gemini, `inherits` for Grok), with
`resolve_interrupt_reconcile()` following the `inherits` edge so packs always
read a concrete kind. The active kind is surfaced at runtime on
`event=barge.detected` (`reconcile=`) and
`/state.voice.barge_in.barge_in_reconcile`, so a durable barge-in is
distinguishable from a cosmetic one. Pinned by
`tests/test_voice_barge_in_contract.py` and `tests/test_voice_catalog.py`.

The cross-provider ordering invariant — provider cancel/truncate follows the
local TTS flush and the final playout-ledger acknowledgement — is owned by
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md#robust-barge-in-contract).

## Adding a fourth provider

1. New module `jasper/voice/<provider>_session.py` implementing
   `LiveConnection` and a `LiveTurn`. Route every model-issued tool call
   through `jasper.tools.dispatch_tool(registry, name, args)` — it owns the
   per-tool timeout, the `{"error": …}` shapes, scalar wrapping, timing logs,
   and the wake-telemetry observer. Keep only wire-format parts in the
   adapter; do not re-inline the dispatch body.
2. New model entries (per model ID, `as_of` bumped) in
   `jasper/data/model_pricing.json`, plus `pricing_url` + `pricing_buckets`
   on the `ProviderCatalogEntry`. No code in `jasper/usage.py`.
3. New env-var block in `Config` (key, model, voice, provider-specifics) with
   an explicit "required only when active provider" validation.
4. New entry in `jasper/voice/catalog.py`: model status labels, voice choices,
   an `interrupt_reconcile` declaration (plus `interrupt_reconcile_base` when
   it inherits), and `runtime_imports` — the adapter module first, then any
   third-party SDK the adapter imports *lazily*. Both are required, no
   default: `runtime_imports` is what `jasper-doctor` imports to prove the
   provider's code loads in the venv, so an undeclared lazy SDK makes that
   check pass on a box where the provider cannot run.
   `tests/test_voice_provider_runtime_imports.py` parses the real source and
   fails if either half drifts.
5. No reconciler shell allow-list edit — `install.sh` emits
   `voice_provider_ids` from the catalog. Keep the fail-closed parking tests
   green.
6. New branch in `_make_connection(cfg)`.
7. New contract test modeled on `tests/test_openai_session.py`, pinning:
   connect → tool round-trip → reconnect → manual-VAD payload shape →
   text-context injection does not request generation → a tool round advances
   the turn's idle anchor. Add the turn class to
   `tests/test_voice_barge_in_contract.py`'s `TURN_CLASSES`.
8. No provider-list edit in `scripts/switch-voice-provider.sh` — it reads the
   installed runtime catalog. Add a row to this doc's trade-off table.

If the wire format is OpenAI-Realtime-compatible (the Grok pattern), most of
step 1 is subclassing `OpenAIRealtimeConnection` and overriding
`PROVIDER_NAME`, base URL, and event-name normalisations. Otherwise the
Gemini adapter is the better template — full state machine, supervisor loop,
idle context reset, and tool dispatch in one place.

### Idle anchor + tool rounds

The pre-response idle watchdog
(`jasper/voice/turn_playback.py:_idle_watchdog`) reads
`turn.last_activity_at()` and abandons the turn when `idle_for >
JASPER_IDLE_TIMEOUT_SEC` (default 20) and no audio has arrived yet. It is
protocol-agnostic — all adapters share this one timer. Once audio has
started it switches to a response-stall cap:
`JASPER_RESPONSE_STALL_TIMEOUT_SEC` (default 120) since the last output
chunk, so a long active response is unaffected because each chunk refreshes
`turn.last_chunk_at()`.

That makes the turn class's idle anchor a **cross-provider contract: any
server event meaning "the model is still working" must advance the anchor**,
not just audio deltas and the final `response.done`. A tool-call
`response.done` starts a multi-second round trip during which no audio
arrives; forget to reset and the watchdog fires mid-dispatch. Production hit
exactly this on 2026-05-21 — a weather-tool turn ended ~0.6 s after the tool
result was sent, with the orphan-response warning logging 48 dropped audio
tokens.

Wiring today: `OpenAIRealtimeTurn._note_activity()` from the function-calls
branch of `_handle_response_done` and from `_on_response_done` (Grok inherits
it verbatim); `GeminiLiveTurn._note_activity()` on tool_call arrival, inside
the per-tool loop, and again after `send_tool_response` lands. A new provider
either exposes an equivalent and calls it on every tool-round server event,
or documents why its wire format satisfies the anchor naturally.

**The anchor is turn-open, so the user's input phase spends the same budget
as the model's first response** — `last_activity_at()` returns turn-start
until the model speaks, and the model cannot speak until `end_input()`. Why
`HARD_RECORDING_CAP_SEC` therefore cannot fire on a stock box, why
push-to-talk derives its own bound instead, and the two degraded bands a low
`JASPER_IDLE_TIMEOUT_SEC` walks into are
[ADR-0162](adr/0162-the-pre-response-idle-anchor-stays-turn-open.md) — read it
before touching either timer.

**Push-to-talk turns refuse server VAD**, which is an *endpointer*: at
`server_vad_silence_ms` the server declares end-of-utterance and the model
answers, a second writer of a boundary the button already owns. `_begin_turn`
refuses it and logs `event=server_vad.disabled_push_to_talk` (WARN, one-shot
per daemon) rather than going quietly inert, evaluated against the same
three-part condition that would otherwise arm it — flag *and* provider
support *and* music playing — so it can only claim to have blocked something
genuinely on the table.

### End-of-turn timing

End-of-turn — un-duck music, fire the "done listening" chirp, release the
turn — is anchored on `TtsPlayout.expected_drain_at()`, a sample-counted
deadline for when the last queued sample exits the OS audio stack, not when
it leaves the inter-task queue. Both `_play_responses` and `_idle_watchdog`
consult it, so timing is provider-agnostic and the two paths converge. New
adapters get this for free; per-provider chunk pacing (OpenAI burst, Gemini
realtime) needs no adapter change. Design and observability hooks:
[audio-paths.md](audio-paths.md) "End-of-turn drain".

When a provider exposes a stable assistant audio item id, its `LiveTurn`
should yield `AudioOutChunk` values from `audio_out_chunks()` with
`provider_item_id` populated (OpenAI does, from
`response.output_item.added.item.id`; Gemini has no equivalent). The daemon
passes that identity through `OutputdTtsPlayout.write_segment()` so fan-in's
flush acknowledgement can later drive provider-specific truncate or cancel.

## Anti-patterns

Surfaced and rejected in design reviews:

- **Don't auto-fall-back across providers.** A provider failure plays
  `cant_reach_cloud` and stays put (ADR-0159).
- **Don't add cue text per provider.** Cues never name which backend failed.
- **Don't share the supervisor LOOP across providers.** The primitives are
  shared; the loop bodies differ enough — handle drop on Gemini, no handle on
  OpenAI — that abstracting them has consistently produced bigger diffs than
  two parallel loops.
- **Don't make tools fully neutral by default.** Visible-to-everyone is right
  for subsystem-call tools; a tool needing one provider's exclusive
  capability gets tagged explicitly, so the model elsewhere cannot see or
  call it — the safest failure mode.
- **Don't approximate end-of-turn from upstream signals** — network-arrival
  timestamps, queue-dequeue stamps, fixed post-response margins. The
  `TtsPlayout` drain primitive is the only correct anchor; two such
  approximations were retired for clipping the last word on burst-streamed
  responses.

## Related docs

- [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md) — the cue
  subsystem, including the pre-rendered TTS used by all providers
- [HANDOFF-prompting.md](HANDOFF-prompting.md) — the prompt and tool-
  description surfaces these adapters serialize
- [audio-paths.md](audio-paths.md) — how TTS enters fan-in before CamillaDSP,
  and assistant loudness matching

---

Last verified: 2026-08-26 (every kept claim rechecked against `jasper/voice/`,
`jasper/config.py`, `jasper/usage.py`, `jasper/voice_daemon.py`, and
`deploy/`; the supervisor + idle-context-reset section was folded in from the
archived 2026-05 rework brief and re-verified against the code)
