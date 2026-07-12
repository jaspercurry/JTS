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
As of 2026-06-02, the unconfigured state parks cleanly instead of
consuming the service crash budget: `Config.from_env` raises
`VoiceProviderNotConfigured`, `jasper-voice` exits with EX_CONFIG
(`78`), and `jasper-voice.service` declares `SuccessExitStatus=78` +
`RestartPreventExitStatus=78`. Real runtime crashes still use
`Restart=on-failure` and the existing `StartLimitAction=reboot`
resilience path.

The pre-daemon AEC reconciler has one extra boot-safety contract:
[`deploy/install.sh`](../deploy/install.sh) renders
`/var/lib/jasper/voice_provider_ids` from
`jasper.voice.catalog.provider_ids_manifest_text()` after the runtime
venv is installed. [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile)
then accepts `JASPER_VOICE_PROVIDER` only when it is an exact line in
that shell-readable file. If the file is missing or stale, the
reconciler parks `jasper-voice`; it never starts voice on an
unconfigured or unrecognized provider. This file is only the provider
ID allow-list projection — the active provider itself still lives only
in `/var/lib/jasper/voice_provider.env`.

Operator and diagnostic surfaces also consume the catalog rather than
mirroring provider IDs in parallel: `scripts/switch-voice-provider.sh`
reads the installed Pi runtime catalog for provider IDs, key env vars,
and model env vars; `jasper-doctor` derives the active provider key
check from the same catalog and verifies the generated
`voice_provider_ids` file is present and in sync.
As of 2026-07-12, `scripts/switch-gemini-model.sh` follows the same
boundary: its stable `3.1` / `2.5` operator aliases resolve to model IDs
from the installed Gemini catalog entry. It requires `3.1` to be the
unique tested default and `2.5` to be the unique non-default fallback,
and refuses to edit the effective wizard-owned selector file or restart
voice when that catalog contract is missing, ambiguous, or malformed. A
successful switch atomically updates `/var/lib/jasper/voice_provider.env`,
restarts `jasper-voice`, and verifies the selected model in the new daemon's
process environment.

Display/aggregation surfaces that are not `jasper-voice` (e.g.
`jasper-control`'s `/state` and the `/system/` dashboard) read the
active provider through
[`jasper/voice/provider_state.py`](../jasper/voice/provider_state.py)
(`read_active_provider*`), which re-reads the file fresh — never
`os.environ`, which is frozen at daemon start and only refreshed when
`jasper-voice` restarts on a switch. Returns `""` (unconfigured) for an
unset/invalid value, never a guessed default. As of 2026-06-11,
diagnostics that need to explain why no usable provider was read use
`read_active_provider_state()`, which distinguishes configured, unset,
missing, unreadable, and invalid states so a permission-denied probe is
not reported as first-time setup.

The abstraction lives in [`jasper/voice/session.py`](../jasper/voice/session.py)
as the `LiveConnection` and `LiveTurn` Protocols. WakeLoop in
[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) and the daemon
composition in [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py)
speak only to those interfaces; the per-provider adapters are:

- [`jasper/voice/gemini_session.py`](../jasper/voice/gemini_session.py) — `GeminiLiveConnection`
- [`jasper/voice/openai_session.py`](../jasper/voice/openai_session.py) — `OpenAIRealtimeConnection`
- [`jasper/voice/grok_session.py`](../jasper/voice/grok_session.py) — `GrokRealtimeConnection` (subclass of the OpenAI adapter)

Conversation-history capture is an optional turn capability, not a provider
branch in WakeLoop. Providers that natively receive text transcripts expose
`ConversationTranscriptTurn.user_transcript()` / `assistant_transcript()` on
their turn objects; WakeLoop probes those methods at teardown and writes through
the daemon-held `ConversationStore` only when the opt-in capture gate is
enabled. Providers without native transcript support can either omit the
capability and still satisfy `LiveTurn`, or expose
`ConversationMetadataTurn.conversation_metadata()` for bounded, privacy-safe
metadata such as "transcripts unavailable" and tool names; the `/chat/` page
renders the missing side honestly.

Daemon-initiated confirmation windows use the provider-neutral
`LiveTurn.send_text_context()` hook. It adds a text-only routing instruction to
an already-acquired turn without asking the provider to generate yet, so the
normal user-audio VAD path still decides whether to commit input. The research
ready yes/no window is the first caller.

The single switch point is `_make_connection(cfg)` in
[`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py). Provider session
preprocessing is resolved through
[`jasper/voice/input_policy.py`](../jasper/voice/input_policy.py), which
turns the applied mic/AEC runtime config into an input-audio contract
before OpenAI/Grok wire-format fields are chosen.

## Model catalog policy

The `/voice/` wizard reads its provider, model, voice, and
provider-specific knob metadata from
[`jasper/voice/catalog.py`](../jasper/voice/catalog.py). That file is
the curated catalog: each visible model is labelled as `tested`,
`fallback`, or `experimental` so operators can distinguish "this is
the default we run" from "this exists as an escape hatch." Runtime
`Config` also reads the provider model, voice, and extra-control
defaults from the same catalog helpers; env overrides still win.

The catalog is **not** a runtime allow-list. The provider adapters pass
whatever `JASPER_<PROVIDER>_MODEL` string is configured through to the
SDK, and the wizard preserves unknown configured values as custom
experimental rows. This gives JTS the two properties we want:

- No silent latest: we do not automatically switch a speaker to a new
  upstream model just because a provider released one.
- No permanent lock-in: an operator can still type or script a newly
  released model into the env file, and the next wizard save will not
  erase it.

The `/voice/` wizard also has a manual **Refresh available models**
button per provider. It is deliberately not part of normal page render:
network calls happen only when an operator clicks refresh and the
provider has a configured API key. Discovery code lives in
[`jasper/voice/model_discovery.py`](../jasper/voice/model_discovery.py)
and writes `/var/lib/jasper/voice_model_discovery.json` at mode 0600.
The next page render reads that local cache and appends
provider-discovered model IDs that are not in the curated catalog as
`experimental; discovered` dropdown options.

The same explicit-action rule applies to assistant loudness seeding:
the wizard's **Save and Test** button writes
`/var/lib/jasper/voice_provider.env`, then makes one bounded provider
TTS request for `"This is me talking normally."`, measures it silently,
stores the provider/model/voice loudness profile, and restarts
`jasper-voice`. Plain **Save and restart voice** never calls a provider
TTS API. Daemon-start seeding is off by default and only runs when an
operator opts into `JASPER_ASSISTANT_LOUDNESS_AUTO_SEED=1`.

Important invariants:

- No page-load provider calls. The wizard stays fast and usable when
  the Pi is offline or the provider is down.
- No implicit paid calibration calls. Model refresh and voice-level
  testing are operator-triggered buttons, and Save-and-Test is capped at
  one provider synthesis attempt.
- No auto-promotion. Catalog entries stay first; discovered models are
  hints, not proof they are production-good on this speaker.
- No surprise migration. Refresh never changes
  `JASPER_<PROVIDER>_MODEL`; only an explicit Save with the selected
  model updates the runtime env file.
- Failed refreshes keep the last successful model list and record the
  sanitized error in the cache for the UI. Error strings intentionally
  avoid leaking API-key-bearing URLs.

## Why three, not one

Each backend has a real strength and at least one real cost:

| Provider | Strengths | Costs |
|---|---|---|
| **Gemini Live** (gemini-3.1-flash-live-preview / gemini-2.5-flash-native-audio) | Cheapest by ~5×; mature 24-language voice catalogue; session resumption (2 h handle); the existing Jasper deployment runs on it | Sequential tool calls only on 3.1; occasional silent-session-2 failures requiring a fall-back to `2.5-flash-native-audio-preview-12-2025`; 15-min audio cap on a single session |
| **OpenAI Realtime** (gpt-realtime-2, GA 2026-05-07) | Reasoning levels (minimal/low/medium/high/xhigh); 128K context; multi-tool-at-once; image input; MCP; SIP; arguably tightest tool/instruction following | $32/$64/$0.40 per 1M tokens — about 5× Gemini per minute; 60-min hard session cap with NO resumption; PCM-input only at 24 kHz (we upsample 16 kHz mic) |
| **xAI Grok** (grok-voice-think-fast-1.0) | Sub-second TTFA; flat $3/hour realtime billing (cheapest at sustained active chat); first-class web/x/file/MCP search built-ins; OpenAI-protocol-compatible so it rides the same adapter | Cost is active realtime duration, not tokens, so it's metered separately via `BillableActivityMeter` (token rows price to $0); idle warm WebSocket time is intentionally not counted because xAI's dashboard does not bill it like active conversation time; voice catalogue is disjoint from OpenAI's (eve / ara / rex / sal / leo); fewer guarantees on event-shape stability — xAI documents one rename today (`response.text.delta` → `response.output_text.delta`) and we normalise it in `grok_session.py` |

Anthropic is **not** on the list. As of 2026-05-09 there is no public
real-time speech-to-speech API from Anthropic — only push-to-talk Voice
Mode in the consumer apps and dictation in Claude Code.

## Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │     jasper/voice_daemon.py + voice/*.py        │
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
  shared slugs (`spend_cap_reached`, `cant_connect`,
  `cant_reach_cloud`, `research_failed`) cover provider-independent
  failure modes. Cue text
  is provider-agnostic by design — no "Google" or "Gemini" or
  "OpenAI" mentions ever bake into the audio.
- **Spend-cap pricing** ([`jasper/usage.py`](../jasper/usage.py)):
  `pricing_for_model(model_id, overrides=...)` returns a `Pricing`
  snapshot **keyed by exact model ID** (there is no provider-level price);
  `UsageStore` accepts it on construction and applies it at session-close
  time. Default rates ship dated in `jasper/data/model_pricing.json`; the
  `/voice` page edits per-model overrides (see
  [HANDOFF-pricing-editor.md](HANDOFF-pricing-editor.md)). Switching
  models/providers mid-day naturally aggregates — older sessions retain
  whichever pricing was active when they closed. Four things worth knowing:
  - **Unknown models are unpriced, not guessed.** A model in neither the
    bundled defaults nor the override resolves to an all-zero `Pricing`
    labelled `unpriced:<id>`; `jasper-voice` logs `event=pricing.unpriced`
    and cost reads $0 until a rate is set. We never invent a number.
  - **Per-turn usage is normalised.** OpenAI reports per-response token
    deltas (summed within a turn); Gemini reports a counter cumulative
    for the WebSocket's lifetime, so `GeminiLiveTurn` subtracts the
    baseline captured at turn start. Each per-turn usage row therefore
    holds that turn's tokens and `SUM()` across rows doesn't multi-count.
  - **Time-billed providers (Grok) are metered by active turn time.**
    Grok publishes a flat $/hour realtime rate, so its token rows price
    to $0; `BillableActivityMeter` records billable activity intervals
    when a voice turn is active and the spend queries fold that cost in.
    Pre-fix idle-socket rows are tagged as legacy during schema self-heal
    and ignored. The `/voice` spend-cap status card and cap therefore see
    estimated Grok cost without charging idle warm WebSocket time. The xAI
    dashboard remains the billing source of truth; JTS's local value is a
    conservative circuit-breaker estimate.
  - **Stored cost is a true estimate; the cap pads at read time.**
    `SpendCap` multiplies the rolling spend by
    `JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER` (default 1.25) so the
    breaker stays conservative without inflating the displayed number.
    `/voice` shows the rolling 24h true estimate, the padded comparison,
    and the remaining headroom.
  - **"Household spend" sums per-surface ledgers.** The cap and the
    `/voice` card read `usage.py`'s `household_usage_reader`, which sums the
    voice ledger (`usage.db`) and the P6 tuning-surface ledger
    (`usage-tuning.db`, written solely by root `jasper-correction-web`). So a
    voice session refuses once the tuning assistant's paid calls have exhausted
    the shared daily cap. Definition + fail direction:
    [HANDOFF-calibration-agent.md](HANDOFF-calibration-agent.md) "Cost discipline".
  - **The cap is editable on `/voice`.** The form writes
    `JASPER_DAILY_SPEND_CAP_USD` and
    `JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER` into the wizard-owned
    `/var/lib/jasper/voice_provider.env`, which is sourced after
    `/etc/jasper/jasper.env`; a saved value there overrides the template
    default without giving `jasper-web` write access to `/etc`.
  - **Rate data remains separate from the cap.**
    Bundled rates (`jasper/data/model_pricing.json`, dated) are defaults;
    an optional `JASPER_PRICING_FILE` (`/var/lib/jasper/pricing.json`)
    overlays them per model ID without a code change
    (`load_pricing_overrides`).

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
- **Provider preprocessing policy**. OpenAI's input `noise_reduction`
  is not a generic "smart speaker" default; it is a provider-side audio
  transform on the stream OpenAI receives. `JASPER_OPENAI_NOISE_REDUCTION`
  defaults to `auto`, resolved by `jasper/voice/input_policy.py` from
  the effective input contract: already-processed profiles such as
  `xvf_chip_aec` and `xvf_software_aec3` omit provider denoising,
  raw direct mics use `far_field`, and explicit `off` / `near_field` /
  `far_field` values remain operator overrides. Chip-AEC classification follows
  the reconciler-applied base chip flag plus the primary/session UDP stream;
  the optional 150/210 beam device vars do not need to be present. `jasper-voice` logs the
  resolved policy as `event=voice.input_policy` and warns on suspicious
  combinations such as explicit `far_field` on an already-processed
  input stream.
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
- **Server-side VAD capability**. OpenAI and Grok support mid-session
  switching from manual VAD to `server_vad` via `session.update`, but
  this is now an opt-in experiment, not the default production path.
  Gemini does not — its `automatic_activity_detection` is fixed at
  connect time (changeable only on session resume with a ~500-1000 ms
  reconnect). The daemon's `_begin_turn` checks
  `connection.supports_server_vad()` (a `LiveConnection` protocol
  method) rather than branching on provider name. When
  `JASPER_SERVER_VAD_ENABLED=1`, music is playing, and the provider
  supports it, the daemon switches to `server_vad` with
  `create_response: false` + `interrupt_response: false`, receives
  `speech_started` / `speech_stopped` / `committed` events, and fires
  `response.create` from the `_server_vad_response_trigger` background
  task. Provider adapters that support this surface implement the public
  `LiveConnection.set_turn_detection()` / `create_response_only()` hooks
  and the public `LiveTurn.mark_server_vad()` /
  `server_speech_started()` / `wait_for_server_eou()` hooks; the older
  OpenAI private helper names are compatibility wrappers, not the daemon
  contract. Manual VAD is restored on turn release. The switching is also
  gated on the voice daemon's cheap content-activity observer. Production defaults
  to local Silero (`JASPER_SERVER_VAD_ENABLED=0`) because the May 2026
  A/B matrix found server VAD cut off real utterances and was prone to
  wake-word interference; see `HANDOFF-vad-experiments.md`. Config:
  `JASPER_SERVER_VAD_ENABLED`,
  `JASPER_SERVER_VAD_THRESHOLD`, `JASPER_SERVER_VAD_SILENCE_MS`,
  `JASPER_SERVER_VAD_PREFIX_MS`. Shadow VAD telemetry (a second Silero
  instance on the raw stream, scoring every session frame as pure
  observability) records to the `wake_events` DB alongside the active
  endpointer's decision, for weekly corpus review.

## Provider Interruption Contract

Verified against provider docs on 2026-06-09:

| Provider | Native behavior | JTS adapter obligation |
| --- | --- | --- |
| OpenAI Realtime | VAD can detect user speech and cancel an in-progress response. WebRTC/SIP can automatically truncate unplayed output because the server owns the playback buffer. With WebSocket playback, the client must stop playback, measure what played, and send `conversation.item.truncate`; push-to-talk/manual paths also use `response.cancel` when needed. | Keep local TTS flush first. Use the final playout ledger's provider item id and `audio_played_ms` to send `conversation.item.truncate`; send `response.cancel` for explicit/manual cancellation paths. |
| Gemini Live | `START_OF_ACTIVITY_INTERRUPTS` is the default `ActivityHandling`; start of user activity cuts off the model response. Gemini also reports interrupted server-content turns. | Treat Gemini interruption as provider-side generation state only. Still flush local TTS playback, because Gemini does not know JTS's DAC queue depth or final playout ledger. There is no OpenAI-style item truncation call to synthesize. |
| xAI Grok Voice | xAI's voice API exposes OpenAI-style `server_vad`, `input_audio_buffer.speech_started/stopped`, `conversation.item.truncate`, and `response.cancel`; docs state VAD-mode interruptions are automatic and `response.cancel` is for manual cancel outside VAD. | Reuse the OpenAI adapter shape where event support is confirmed, but keep feature probes/provider overrides because xAI documents OpenAI-compatible shapes with provider-specific event-name differences. |

Sources:

- OpenAI Realtime conversations — interruption/truncation and
  push-to-talk WebSocket guidance:
  <https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation>
- Gemini Live WebSockets API reference — `ActivityHandling`,
  `START_OF_ACTIVITY_INTERRUPTS`, and interrupted server-content turns:
  <https://ai.google.dev/api/live#activityhandling>
- xAI Voice API reference — Realtime client/server events, VAD speech
  events, `conversation.item.truncate`, and `response.cancel`:
  <https://docs.x.ai/developers/rest-api-reference/inference/voice>

The provider-neutral interface is capability-based, not
provider-name-based:

- `request_local_interrupt()` — **landed (PR-2)** on `LiveTurn`
  (`jasper/voice/session.py`; implemented by the Gemini and OpenAI/Grok
  adapters). It is the local-flush trigger only: it sets the turn's
  interrupt event so `_play_responses` flushes audible TTS, and
  deliberately does **not** cancel/truncate the provider. The daemon
  drives it from in-session Silero VAD behind the default-OFF
  `JASPER_BARGE_IN_<PROVIDER>` flag. The provider-cancel seam below
  (`cancel_response` / `truncate_assistant_audio`) is now **wired for
  OpenAI (PR-4)**: after the local flush, `_flush_for_interrupt` drives
  `cancel_response` then `truncate_assistant_audio` with the flush ack's
  played-ms. **Grok inherits that pack** (its paid verification is PR-6).
  **Gemini's reconcile is finalised as a no-op (PR-5)**:
  `server_self_truncates` has no client truncate/cancel call to make, and
  JTS keeps Gemini on manual VAD + `NO_INTERRUPTION` even with barge-in
  enabled, so the daemon's local gate is the sole interruption authority
  (option (a) in [HANDOFF-barge-in.md](HANDOFF-barge-in.md) "Gemini pack";
  pinned by `tests/test_gemini_barge_in.py`).
- `cancel_response(reason)` for explicit local interruption/manual
  cancellation.
- `truncate_assistant_audio(provider_item_id, audio_played_ms)` for
  providers that need conversation history aligned to WebSocket
  playout.
- `drop_pending_audio()` (returns the count dropped) for providers with an
  internal playout buffer: after the local TTS flush, the spine drains the
  adapter's queued-but-unwritten assistant audio so a burst-delivery provider
  (OpenAI/Grok stream the whole response up front) does not replay the backlog
  over the user — the local flush alone clears only the DAC ring. Optional
  (getattr-probed); an adapter that streams without a buffer omits it.
  Preserves any terminal end-of-audio sentinel so the consumer still ends.
- `supports_provider_vad()` remains separate from barge-in support:
  provider VAD can help detect or commit turns, but local TTS flush is
  still required to stop audible audio immediately.
- Adapters must tolerate missing provider item ids. Gemini currently
  has no OpenAI-style item id for audio truncation; OpenAI emits one and
  JTS already carries it through the outputd-compatible TTS IPC used by
  fan-in.
- The active provider's reconciliation kind is surfaced at runtime — on
  `event=barge.detected` (`reconcile=`) and
  `/state.voice.barge_in.barge_in_reconcile` — so a durable barge-in
  (`needs_client_truncate`: OpenAI/Grok cancel+truncate) is distinguishable
  from a cosmetic one (`server_self_truncates`: Gemini no-ops the reconcile and
  a real-time server may resume). This makes the registry's
  `interrupt_reconcile` declaration load-bearing, not test-only metadata.

This seam landed in code as of PR-3 (added **behaviour-neutral**):
`LiveTurn.cancel_response()` / `LiveTurn.truncate_assistant_audio()` and
`LiveConnection.supports_provider_vad()` live in
[`jasper/voice/session.py`](../jasper/voice/session.py). **PR-4 wired the
OpenAI pack** (the reference): `cancel_response` → `response.cancel`
(guarded to an in-progress response, so it can't trip the server's
`response_cancel_not_active`), and `truncate_assistant_audio` →
`conversation.item.truncate{content_index:0, audio_end_ms}` using the
playout ledger's played-ms — a **no-op + WARN when that played-ms is 0**, so
it never truncates on bytes-received (an out-of-range `audio_end_ms` errors
server-side and desyncs context). Grok inherits it; Gemini keeps the no-op
default (it self-truncates server-side). It stays default-OFF behind the
per-provider flag, so no household behaviour changed.
Which reconciliation a provider needs is a declarative registry field, not
a provider-name branch: `ProviderCatalogEntry.interrupt_reconcile` in
[`jasper/voice/catalog.py`](../jasper/voice/catalog.py)
(`needs_client_truncate` for OpenAI, `server_self_truncates` for Gemini,
`inherits` → OpenAI for Grok); `resolve_interrupt_reconcile()` follows the
`inherits` edge so packs always read a concrete kind. The seam is pinned by
`tests/test_voice_barge_in_contract.py` and the registry declaration by
`tests/test_voice_catalog.py`.

The cross-provider invariant is owned by
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md#robust-barge-in-contract):
provider cancel/truncate follows the local TTS flush and final
playout-ledger acknowledgement.

## Adding a fourth provider

When a new real-time backend lands (a self-hostable Ultravox-class
model, Mistral Voxtral, future Anthropic, etc.), the integration
should be:

1. New module `jasper/voice/<provider>_session.py` with a class
   implementing `LiveConnection` (and a corresponding `LiveTurn`).
   Route every model-issued tool call through
   `jasper.tools.dispatch_tool(registry, name, args)` — it owns the
   per-tool timeout (`tool.timeout`), the `{"error": …}` failure
   shapes, scalar wrapping, and the provider-uniform timing logs.
   The adapter keeps only its wire-format parts: parsing the call's
   args and packaging the returned payload. Do not re-inline the
   dispatch body; all three existing adapters route through it
   (Grok via the OpenAI subclass).
2. New model entries (per model ID, with `as_of` bumped) in
   `jasper/data/model_pricing.json`, plus `pricing_url` + `pricing_buckets`
   on the provider's `ProviderCatalogEntry` (so the `/voice` editor and
   research prompt show the right fields/page). (No code in
   `jasper/usage.py` — pricing is data now.)
3. New env-var block in `Config` (api key, model, voice, anything
   provider-specific) with a sane default and an explicit
   "required only when active provider" validation.
4. New provider entry in `jasper/voice/catalog.py`, including model
   status labels (`tested` / `fallback` / `experimental`), voice
   choices for the `/voice/` wizard, and an `interrupt_reconcile`
   barge-in declaration (`needs_client_truncate` /
   `server_self_truncates` / `inherits` + `interrupt_reconcile_base`).
5. No reconciler shell allow-list edit: `install.sh` emits
   `/var/lib/jasper/voice_provider_ids` from the catalog, and
   `jasper-aec-reconcile` reads that generated file. Keep the
   fail-closed parking tests green so an unset, invalid, or missing-
   manifest provider never starts voice.
6. New branch in `_make_connection(cfg)` in `jasper/voice/daemon_main.py`.
7. New contract test in `tests/test_<provider>_session.py` modeled on
   `tests/test_openai_session.py`. Pin: connect → tool round-trip →
   reconnect → manual-VAD payload shape → text-context injection does
   not request generation → tool round advances the turn's idle anchor
   (see "Idle anchor + tool rounds" below). Also add the adapter's
   turn/connection classes to `tests/test_voice_barge_in_contract.py`'s
   parametrized lists so the barge-in seam (cancel / truncate /
   `supports_provider_vad`) is covered.
8. No provider-list edit in `scripts/switch-voice-provider.sh`: it
   reads provider IDs, key env vars, and model env vars from the
   installed runtime catalog on the Pi.
9. New row in this doc's tradeoff table.

If the wire format is OpenAI-Realtime-compatible (Grok pattern), most
of step 1 is "subclass `OpenAIRealtimeConnection` and override
`PROVIDER_NAME` / base URL / event-name normalisations". Otherwise, the
Gemini adapter is the better template — it shows the full state
machine, supervisor loop, idle context-reset, and tool dispatch in one
place.

### Idle anchor + tool rounds

The daemon's pre-response idle watchdog
(`jasper/voice/turn_playback.py:_idle_watchdog`) reads `turn.last_activity_at()`
and abandons the turn if `idle_for > JASPER_IDLE_TIMEOUT_SEC` *and* no
audio has been received yet. The watchdog is protocol-agnostic — all
adapters share this one timer.

Once audio has started, the same watchdog switches to a response-stall
cap: if the server has not signalled turn-complete and no new output
chunk arrives for `JASPER_RESPONSE_STALL_TIMEOUT_SEC` (default 120 s),
the daemon abandons the turn instead of holding the speaker in session
forever. Active long responses are unaffected because each chunk refreshes
`turn.last_chunk_at()`.

That makes the turn class's idle anchor a cross-provider contract:
**any event from the server that means "model is still working" must
advance the anchor**, not just audio deltas and the final
`response.done`. In particular, a tool-call response.done (or
equivalent) starts a multi-second round trip (client → tool → response
2) during which no audio arrives. Forget to reset and the watchdog
fires mid-dispatch at small `JASPER_IDLE_TIMEOUT_SEC` (production runs
20 s since PR #187 raised it from a 10 s override — see `jasper/config.py`
and `.env.example`). Production hit this on 2026-05-21: a weather-tool turn ended
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

### End-of-turn timing

End-of-turn (the moment the daemon un-ducks music, fires the
"done listening" chirp, and releases the turn) is anchored on
`TtsPlayout.expected_drain_at()` — a sample-counted deadline that
tracks when the last queued audio sample actually exits the OS
audio stack, not when it leaves the inter-task queue. Both
`jasper/voice/turn_playback.py`'s `_play_responses` (consumer) and
`_idle_watchdog` (server-said-done
path) consult this primitive, so timing is provider-agnostic and
the two paths converge.

New adapters get this for free — drain math lives below the
provider abstraction. Per-provider chunk pacing (OpenAI burst,
Gemini real-time) doesn't require any adapter changes. The full
design + prior-art survey + observability hooks live in
[audio-paths.md](audio-paths.md) under "End-of-turn drain".

When a provider exposes a stable assistant audio item id, its
`LiveTurn` should yield `AudioOutChunk` values from
`audio_out_chunks()` with `provider_item_id` populated. OpenAI does
this from `response.output_item.added.item.id`; Gemini currently has
no equivalent and leaves the field empty. The voice daemon passes this
identity through `OutputdTtsPlayout.write_segment()` so fan-in's
flush acknowledgement can later drive provider-specific truncate or
cancel calls.

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
- **Don't approximate end-of-turn from upstream signals** —
  network-arrival timestamps, queue-dequeue stamps, fixed
  post-response margins. The TtsPlayout drain primitive
  (`expected_drain_at` / `wait_drained`) is the only correct anchor;
  it accounts for the OS audio pipeline depth that upstream signals
  can't see. PR #311 retired two such approximations
  (`POST_RESPONSE_IDLE_TIMEOUT_SEC=0.5`, `TTS_ALSA_DRAIN_SEC=0.3`)
  that were clipping the last word on burst-streamed responses.

## Related docs

- [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md) — Gemini-side reconnect supervisor + idle context reset
- [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md) — the cue subsystem, including the pre-rendered TTS used by all providers
- [audio-paths.md](audio-paths.md) — how TTS enters fan-in before CamillaDSP and how assistant loudness matching works

Last verified: 2026-07-12 (Gemini switcher catalog/effective-owner contract;
prior 2026-07-06 pass verified that the cap + `/voice` card sum the voice ledger
and the P6 tuning ledger via
`usage.py`'s `household_usage_reader`, verified against `jasper/usage.py`,
`jasper/voice/daemon_main.py`, and `jasper/web/voice_setup.py`; canonical
definition lives in HANDOFF-calibration-agent.md. Prior pass 2026-06-30:
chip-AEC input-policy classification rechecked
against `jasper/voice/input_policy.py` and `tests/test_voice_input_policy.py`;
base chip-AEC no longer depends on optional 150/210 wake-beam device vars.
Prior pass 2026-06-24: time-billed Grok accounting re-verified against xAI's pricing/cost-tracking/Voice WebSocket docs plus `jasper/usage.py`, `jasper/voice/openai_session.py`, `jasper/voice/grok_session.py`, and `tests/test_grok_session.py`; barge-in interruption contract re-verified against `jasper/voice/session.py`, `jasper/voice/turn_playback.py`, and the adapters — added the `drop_pending_audio()` seam member and the `reconcile`/`barge_in_reconcile` observability after the integrated-review remediation; unconfigured-provider parking verified against `jasper/config.py`, `jasper/voice/daemon_main.py`, `jasper/voice_daemon.py`, `deploy/bin/jasper-aec-reconcile`, and `deploy/systemd/jasper-voice.service`; spend/usage accounting still matches current `jasper/usage.py`; `/voice` spend-cap status/settings verified by `tests/test_voice_setup.py`; OpenAI noise-reduction auto policy verified by `tests/test_voice_input_policy.py` and `tests/test_openai_session.py`; audio-path cross-reference updated for fan-in TTS; provider interruption docs rechecked for OpenAI Realtime, Gemini Live, and xAI Grok Voice; server-VAD public hook contract and response-stall cap rechecked against `jasper/voice/session.py`, `jasper/voice/openai_session.py`, `jasper/voice/turn_playback.py`, `jasper/voice_daemon.py`, and `tests/test_voice_daemon_defects.py`;
barge-in capability seam: PR-3 landed it behaviour-neutral
(`LiveTurn.cancel_response`/`truncate_assistant_audio` +
`LiveConnection.supports_provider_vad` + catalog `interrupt_reconcile`);
PR-4 wired the OpenAI/Grok pack to real `response.cancel` +
`conversation.item.truncate` with the played-ms no-op-if-0 guard, driven
by `turn_playback._flush_for_interrupt` — pinned by
`tests/test_openai_session.py`, `tests/test_turn_playback_barge_in.py`, and
`tests/test_voice_barge_in_contract.py`, registry by
`tests/test_voice_catalog.py`)
