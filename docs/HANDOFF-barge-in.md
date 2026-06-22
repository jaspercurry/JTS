# Robust barge-in — implementation plan & gap analysis

> **Status: operational.** This doc owns the **implementation plan
> and current-code gap analysis** for robust assistant-speech
> barge-in. The *contract* is canonical elsewhere and is **not
> restated here** (single-source-of-truth):
>
> - Output-side invariant + first acceptance test —
>   [HANDOFF-speaker-output-reference.md → Robust Barge-In Contract](HANDOFF-speaker-output-reference.md#robust-barge-in-contract)
> - Provider interruption matrix + capability interface —
>   [HANDOFF-voice-providers.md → Provider Interruption Contract](HANDOFF-voice-providers.md#provider-interruption-contract)
>
> The 2026-05-23 design-space + option-costing record (Options A/B/C,
> and the standing rejection of AEC-topology re-architecture) is
> preserved verbatim as a **[historical appendix](#appendix--historical-2026-05-23-design-space--costing-record)** at the bottom. Its
> AEC-topology rejection is **still binding**; its "today/current
> reality" snapshots are superseded by the sections here.

## What barge-in means here

Barge-in = the household interrupts the assistant **while it is
speaking**, by talking over its TTS, and the speaker stops and
listens — **without a second wake word**. The contract (#532, 2026-06-09)
is **JTS-owned**: JTS stops audible assistant audio *first* (the
latency-critical action), then reconciles each provider's
conversation state to match what the listener actually heard.
Provider-native interruption is an accelerant, never the gate.

The first product acceptance target (defined in the output-reference
contract) is: assistant is speaking → user says **"volume down"** with
no wake word → local TTS stops immediately, the command runs once, and
provider history is truncated/cancelled to match the played-audio
ledger.

## The one-sentence shape

**JTS owns the interrupt: a local VAD on the AEC-cleaned mic flushes
local TTS first, then the active provider's *pack* reconciles the
model's conversation state.** Most logic is shared in the
provider-agnostic core (`jasper/voice_daemon.py`,
`jasper/voice/turn_playback.py`, `jasper/audio_io.py`, the
`LiveTurn`/`LiveConnection` Protocol in `jasper/voice/session.py`); each
provider's barge-in behaviour is a small **pack** behind that one
interface, loaded only for whichever provider the household configured.
**No provider is privileged — there is no default** (the runtime
already refuses to start unconfigured; see `Config.from_env`). The spine
delivers the felt experience identically for whoever is active, and the
pack supplies only that provider's reconciliation delta: one needs a
client truncate, one needs nothing, one inherits-with-a-caveat.

## Current state (verified 2026-06-21 against the tree at/after `main` d2ef9122, which contains #532)

The provider-agnostic *consumer* plumbing exists and is idle: there is
an interrupt event, a fast local-flush primitive, and the Protocol
slots. What is missing is a **trigger**, three concrete **blockers**,
and on-hardware **AEC proof**. Cells: ✅ done / ⚠️ partial / ❌ missing.

> **Update (PR 2 landed, default OFF):** the core spine now exists — the
> trigger (`_handle_playback_frame` → `request_local_interrupt()`), the
> drain-tail race, and the wired `vad_barge_in_threshold` — so the first
> two ❌ Core cells below and blockers #1/#2 are addressed behind the
> per-provider flag. Blockers #3 (production playout ledger) and #4
> (on-hardware AEC proof) still gate the per-provider truncate packs
> (PRs 4–6) and default-on (PR 7); the table below is the pre-PR-2
> snapshot, kept for the still-open cells.
>
> **Update (PR 4 landed, default OFF):** the OpenAI reference pack is
> wired. After the local flush, `turn_playback._flush_for_interrupt` drives
> the active provider's seam — `cancel_response` (→ `response.cancel`,
> guarded to an in-progress response) then `truncate_assistant_audio`
> (→ `conversation.item.truncate{content_index:0, audio_end_ms}` at the
> playout ledger's played-ms). The **no-op-if-0 guard** is in the pack:
> a `max_audio_played_ms == 0` ack (e.g. the ledger of blocker #3 not yet
> live) WARNs and skips rather than truncating on bytes-received. Grok
> inherits it; Gemini's pack (PR 5) stays no-op. `send_audio` was
> **deliberately left half-duplex** — local-VAD flush + cancel/truncate is
> sufficient for context alignment, and forwarding mic during playback
> stays out until it can be corroborated by server-VAD (avoids forwarding
> TTS bleed). So on-device correctness still depends on blocker #3 (real
> played-ms) and default-on on blocker #4 / PR 7.
>
> **Update (integrated review — post-flush replay fixed, default OFF):** the
> review found the local flush cleared only the DAC ring while burst-delivery
> providers (OpenAI/Grok) had already queued the whole response in the adapter,
> so `_play_responses` resumed writing the backlog and the assistant talked over
> the user. Fixed by a new **`drop_pending_audio()`** seam member (getattr-probed;
> drains the adapter's playout queue, preserving the terminal sentinel; Grok
> inherits, Gemini's local path drains too), called from `_flush_for_interrupt`.
> Also: OpenAI truncate clamps `audio_end_ms` to the per-item received-ms
> (multi-segment out-of-range guard); the barge-in flag read is mtime-gated; and
> the provider's `reconcile` kind is surfaced on `event=barge.detected` / `/state`.

| Capability | Core | OpenAI | Gemini | Grok |
|---|---|---|---|---|
| Mic stays live during TTS | ❌ `_handle_session_frame` early-returns once `_input_ended` is set (drops mic during playback) | ❌ `send_audio` also no-ops after `_committed` | ❌ daemon stops forwarding | ❌ inherits OpenAI |
| Local-VAD barge-in gate | ❌ none; `Config.vad_barge_in_threshold` (`JASPER_VAD_BARGE_IN_THRESHOLD`, default 0.5) is **read by no code** | — | — | — |
| Interrupt event → flush | ✅ `turn_playback._play_responses` races `wait_for_interrupt()` → `tts.flush()` → `clear_interrupted()` — *but see blocker #2* | ❌ never sets the event | ⚠️ sets `_interrupt_event` on `server_content.interrupted`, but **config-disabled** (`NO_INTERRUPTION` + automatic VAD disabled in `_build_config`) | ❌ inherits OpenAI |
| Local TTS flush primitive | ✅ PortAudio `TtsPlayout.flush` = `abort()`+`start()` (<50 ms); fan-in `flush_sync` (`FLUSH_SYNC`) returns an ack | ✅ via Core | ✅ via Core (only adapter that can fire it today) | ✅ via Core |
| Played-ms playout ledger | ⚠️ consumer reads `ack["max_audio_played_ms"]` in `audio_io.py`, but the **production fan-in producer returns `max_audio_played_ms=0, events=[]`** (the DAC-clock ledger is not wired to the fan-in path yet — see #532) | needs it for truncate | doesn't need it | needs it (best-effort) |
| Provider cancel/truncate | contract names `cancel_response()` / `truncate_assistant_audio()` — **not yet in `session.py`** (today: `supports_server_vad`, no truncate method) | `_cancel_response()` (`response.cancel`) implemented; `conversation.item.truncate` scaffolded (`_last_assistant_item_id` captured) but **never sent** | n/a — server self-truncates | scaffolded via OpenAI |

**Headline:** there is exactly one place that drops the user's voice
during playback (`_handle_session_frame`'s `_input_ended` early-return),
no detector wired to the existing flush, and the production flush ack
reports `0` ms played. Close those and the flush machinery lights up.

## The blockers (priority order)

1. **Full-duplex spine (core).** Stop dropping mic frames after
   `_input_ended`; run a local-VAD barge-in gate on the AEC-cleaned
   leg during playback and set the interrupt event on a sustained
   speech run. Detection must run **inline in the frame handler**, not
   as a `WakeLoop._bg_task`: `_handle_session_frame` ends the turn the
   moment any `_bg_tasks` entry completes (`if any(t.done() for t in
   self._bg_tasks): await self._end_turn()`), so a fire-once detection
   task would cut the turn short.
2. **Drain-tail interrupt gap (core).** The interrupt race in
   `_play_responses` lives *inside* the `async for` chunk loop, but
   burst-delivery providers (OpenAI sends ~11 s of audio in ~4 s) then
   sit in `await tts.wait_drained()` **with no interrupt race active** —
   the single most common barge-in moment. The race must also cover the
   drain window. (The earlier "turn_playback needs no change" reading
   was wrong.)
3. **Production playout ledger → fan-in ack (#532's named missing
   slice).** Wire the DAC-clock `audio_played_ms` ledger that already
   exists in the outputd core into the fan-in `FLUSH_SYNC` ack so it
   stops returning `0`. **Without this, OpenAI/Grok client truncate is
   impossible to do correctly** — truncating with bytes-received instead
   of samples-rendered desyncs (or errors) the provider context.
4. **AEC clean-enough (hardware, no software substitute).** Barge-in
   detection runs *while the speaker emits TTS*; only echo cancellation
   distinguishes "user spoke" from "we heard ourselves." Feed the
   **AEC-cleaned leg only** (`:9876` chip-AEC primary carrier or
   software AEC3) — **never** the AEC-OFF chip-direct leg (`:9877`),
   which carries full TTS bleed. This is leg/threshold selection, **not**
   AEC-topology change, so it stays inside the standing AEC rule (see
   appendix). MEMORY warns software AEC3 may not be clean enough (the
   whisper-music miss); chip-AEC is the strong candidate. Default OFF
   until measured.

## Provider packs — per-provider capability shims

The barge-in capability follows the repo's **self-contained module +
registry** pattern (config-ownership pattern 2, the transit-provider
shape): the spine is provider-agnostic and the daemon drives it with
**zero per-provider knowledge**; each provider contributes a small
*pack* — its implementation of the capability seam (next section) plus a
declaration of *what kind* of reconciliation it needs. Only the
**active** provider's pack is loaded; the existing `PROVIDERS` registry
in `jasper/voice/catalog.py` is the natural home for the capability
declaration. Authoritative provider matrix + source URLs live in
[HANDOFF-voice-providers.md → Provider Interruption Contract](HANDOFF-voice-providers.md#provider-interruption-contract);
the pack contents (implementation-affecting facts only) are:

- **OpenAI pack (Realtime / WebSocket) — the reference pack, most-used
  here.** The server **never auto-truncates on
  WebSocket** (only WebRTC/SIP do). JTS must: stop playback → measure ms
  *actually rendered* → send `conversation.item.truncate{item_id,
  content_index:0, audio_end_ms}`. `audio_end_ms` is inclusive ms
  relative to the assistant item start and **must be ≤ actual played
  length or the server errors**; truncate deletes the *unheard*
  transcript so context stays aligned. `input_audio_buffer.speech_started`
  is the trigger; `response.cancel` is the manual/PTT path (errors if no
  active response). `output_audio_buffer.*` are WebRTC-only — ignore.
- **Gemini pack (Live).** Default `ActivityHandling` is already
  `START_OF_ACTIVITY_INTERRUPTS`; the server interrupts itself on user
  speech and **self-truncates server-side (keeps only what it *sent*)** —
  there is **no client truncate/cancel call**. The client's only
  obligation is to obey `server_content.interrupted` (stop + flush). Two
  gotchas: an interrupted turn sends **no `generation_complete`** (goes
  `interrupted` → `turn_complete`), and Gemini's history reflects what it
  *sent*, not what JTS *played* (unfixable via API → keep the local
  playback buffer short). The current `NO_INTERRUPTION` + manual-VAD
  choice is deliberate (see
  [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md));
  barge-in must revisit it: either leave server VAD on and just obey
  `interrupted`, or stay manual-VAD and drive the flush purely from the
  local gate (cleaner single authority, no double-VAD).
- **Grok pack (xAI Voice).** Documented OpenAI-compatible **including**
  `conversation.item.truncate` (identical fields), so the inheritance is
  structurally safe. **But** a third-party report says truncate may
  no-op/error on xAI and `response.cancel` alone suffices, and
  `conversation.item.done` is **not emitted** by xAI. Billing is flat
  ~$3/hr per connection-minute (barge-in neither saves nor costs).
  Inherit OpenAI but treat truncate as **best-effort / tolerant-of-error,
  gated OFF** until one paid trial confirms behavior (per AGENTS.md
  "scope to the observed-broken path" — don't enable truncate-on-Grok
  by inheritance).

## The provider-pack capability seam (`session.py`)

This is the single interface every pack implements — the #532 contract's
**capability-based** shape (not provider-name-based), so a new provider
lands as declaration + interface only. The code today is
provider-VAD-named; align it:

- Add `cancel_response(reason)` (explicit local/manual cancel) and
  `truncate_assistant_audio(provider_item_id, audio_played_ms)`.
- Keep `supports_provider_vad()` distinct from barge-in support
  (rename/clarify the existing `supports_server_vad()`).
- Adapters must **tolerate a missing provider item id** (Gemini has
  none; OpenAI emits one and JTS already carries it through the
  outputd-compatible TTS IPC — see `_last_assistant_item_id`).
- The existing `wait_for_interrupt()` / `clear_interrupted()` stay as
  the daemon-facing event the playback path awaits.

## Implementation plan

Spine first (provider-agnostic, default **OFF**, no behaviour change
until flagged on) — it delivers the felt barge-in for *every* provider;
then the capability seam; then per-provider packs (reconciliation
fidelity, loaded for the active provider); then the hardware gate. Each
row is an independently-mergeable PR.

| PR | Scope | Verification (one line) |
|---|---|---|
| 1. Playout ledger → fan-in ack | Wire DAC-clock `audio_played_ms` from the outputd core into the fan-in `FLUSH_SYNC` ack (blocker #3) | `FLUSH_SYNC` ack returns nonzero `max_audio_played_ms` within one output period of the real played duration (the test #532 specifies) |
| 2. Core spine + drain-tail fix **(✅ landed, default OFF)** | `_handle_playback_frame` branch off `_handle_session_frame` (behind flag) runs Silero on the AEC leg → `LiveTurn.request_local_interrupt()` sets the interrupt event; the `_play_responses` interrupt race now also covers `wait_drained`; `vad_barge_in_threshold` is wired (blockers #1, #2). Enable is the per-provider `JASPER_BARGE_IN_<PROVIDER>` flag read fresh via `provider_state.read_barge_in_enabled`; a turn-start guard hard-disables + WARNs (`barge.disabled_no_reference`) on a no-AEC-reference profile. Provider truncate/forward stays for PRs 4–6. | Flag OFF → byte-identical old behavior (pinning test on the `_input_ended` drop); flag ON + synthetic high-Silero frames → event fires & playback flushes (incl. the drain-tail window) |
| 3. Contract alignment | Add `cancel_response`/`truncate_assistant_audio`/`supports_provider_vad` to `session.py`; tolerate missing item ids | Session-contract test; `scripts/test-merge` green |
| 4. OpenAI pack **(✅ landed, default OFF; reference — most-used)** | `cancel_response`→`response.cancel` (guarded to an in-progress response); `truncate_assistant_audio`→`conversation.item.truncate(audio_end_ms = ledger played-ms)`; truncate **no-ops + WARNs if the ledger ack reports `max_audio_played_ms==0`** (never truncate on bytes-received); the spine (`_flush_for_interrupt`) drives cancel-then-truncate after the local flush. `send_audio` **left unchanged** (half-duplex): local-VAD flush + cancel/truncate is sufficient for context alignment, so forward-during-playback is deferred until server-VAD corroboration. | `event=barge.truncate` logged with correct `audio_end_ms`; next turn's context matches the truncated transcript |
| 5. Gemini pack **(✅ landed, default OFF)** | Obey `interrupted` (already coded) + set event from local gate; **resolved the server-VAD-on vs manual-VAD choice → option (a): keep manual VAD + NO_INTERRUPTION even when the flag is on, so the daemon's local gate is the single interruption authority and the connection wire config is barge-in-agnostic**; **no truncate** (server self-truncates → the reconcile seam is a *final* no-op, not deferred wiring). Hardware-free contract pinned in `tests/test_gemini_barge_in.py` | `tests/voice_eval/regression/test_barge_in_gemini.py` added but **SKIPPED** (paid + the single-turn harness can't drive audio overlap); on-device proof — speak over Gemini TTS → quiet < ~400 ms — still owed under blocker #4 / PR-7 |
| 6. Grok pack *(verify)* | No code beyond inheriting the OpenAI pack unless divergence found; truncate gated best-effort; add `tests/voice_eval` Grok scenario | One paid Grok trial confirms inherited cancel/truncate (or a minimal override lands with the observed divergence) |
| 7. AEC threshold + default-on | On-hardware capture of TTS-bleed vs real-barge Silero distributions on both AEC profiles; set threshold from data; doctor check; runtime self-interrupt-loop guard | bleed-P99 < threshold < real-barge-P10 from captured corpus; defaults flip ON only per profile that passes |
| 8. Bonded/multiroom barge-in *(follow-up, deferred)* | Verify flush latency + AEC reference for the member-local TTS lane (`/run/jasper-outputd/tts.sock`) and the outputd reference (`:9891`) when a speaker is grouped | On-device bonded test: barge-in stops member-local TTS without desync; bonded mode stays on the non-barge-in path until then |

PRs 1–2 are the real engineering and unblock all three providers; 3 is the seam and 4–6 are thin per-provider packs (OpenAI the reference; Gemini and Grok mostly declaration + inherit); 7 is the risk (cheap to code, gated on a
hardware measurement that might say "chip-AEC only").

## Config & observability

- **Per-provider enable, default OFF** until blocker #4 passes — so a
  fresh Pi can never ship a self-interrupting speaker. Read via a
  **fresh SSOT reader** (`jasper/voice/provider_state.py`-style), not
  `Config.from_env`: jasper-control (which serves `/state`) is not
  restarted on a provider switch, so a `Config`-cached flag goes stale.
- **Threshold:** reuse `JASPER_VAD_BARGE_IN_THRESHOLD` (already in
  `config.py`, default 0.5). It is currently **dead config** (read by no
  runtime code). Its `.env.example` comment was Gemini-specific and
  described an unbuilt forwarding behaviour; it was rewritten
  provider-neutral on 2026-06-21 with a "not yet read at runtime" note.
  PR 2 finalizes the comment when it wires the gate.
- **`event=` logs** via `jasper.log_event`: `barge.detected`
  (`leg=`, `silero=`, `sustained_ms=`), `barge.cancel` (`reason=` — the
  OpenAI/Grok pack's `response.cancel`), `barge.truncate`
  (`provider=`, `item_id=`, `audio_end_ms=`), `barge.truncate_skipped`
  (`reason=zero_played_ms` at WARN — the no-op-if-0 guard; `reason=no_item_id`
  at DEBUG), `barge.truncate_failed` (WARN — the truncate wire send errored),
  `barge.server_only` (server fired but local didn't — false-barge suspect),
  `barge.flush_failed` (WARN). Reuse the existing `event=tts_flush.playout_ack`.
- **`/state.voice.barge_in`** *(✅ landed PR-2)*: `enabled` (per active
  provider, read **fresh** in jasper-control's aggregator via
  `provider_state.read_barge_in_enabled` — not jasper-voice's
  session_status, since the daemon's `Config` is stale on a toggle),
  plus `last_at` / `count_session` / `last_leg` pulled through from the
  daemon's session_status firing counters.
- **Doctor:** one `@doctor_check` `check_barge_in_config` — warn if a
  provider has barge-in enabled while the active AEC profile is
  `direct_mic` (no reference → guaranteed self-interruption) or the
  feeding leg is AEC-OFF. Plus a **runtime** self-interrupt-loop guard:
  if the gate trips with no reference cancellation available, hard-disable
  for the session and WARN rather than loop.
- **No silent failure path:** barge-in cutting TTS is itself the user
  feedback; a *failed* flush must stay observable (`barge.flush_failed`)
  and fall through to normal turn end.

## Risks & open questions

- **False-barge from TTS bleed (the #1 risk).** Only the AEC-clean leg
  + a data-derived strict threshold contains it; cannot be validated
  without on-device measurement (blocker #4 / PR 7). Software AEC3 may
  not clear the bar — that is the one step no code can de-risk.
- **Two half-duplex guards must change together:** `_handle_session_frame`'s
  `_input_ended` early-return *and* OpenAI `send_audio`'s post-`_committed`
  return. Missing either makes barge-in silently inert.
- **OpenAI `audio_end_ms` correctness** depends entirely on the played-ms
  ledger (PR 1). Bytes-received over-counts (generation outruns playback)
  and the server errors on out-of-range.
- **Grok truncate** is documented-but-unverified; gate it best-effort
  and confirm in one paid trial. Watch for `conversation.item.done`
  absence stranding any state machine.
- **Gemini played-vs-sent gap** is unfixable via API; mitigate by keeping
  the local buffer short.
- **Multiroom / bonded mode** routes TTS through a member-local outputd
  socket with a different flush/reference path (`:9891`); barge-in
  latency and AEC reference there are untested — out of scope for the
  first ship, tracked as PR 8.
- **Latency budget is detection-dominated** (arming run ~160–240 ms),
  not flush-dominated (<50 ms PortAudio / one IPC RTT). The arming-run
  length is the tuning lever, traded against false-barge risk on hardware.
- **Voice-eval cost discipline applies** to PRs 4–6 (~$0.075–$0.60/scenario):
  announce cost, never loop-until-passing.

## Cross-references

- [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md) —
  **canonical** output-side barge-in contract, playout ledger, fan-in flush.
- [HANDOFF-voice-providers.md](HANDOFF-voice-providers.md) — **canonical**
  provider interruption matrix + capability interface + source URLs.
- [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md) —
  why manual VAD / `automatic_activity_detection.disabled` / `NO_INTERRUPTION`
  are used today (the decision barge-in revisits for Gemini).
- [HANDOFF-vad-experiments.md](HANDOFF-vad-experiments.md) — local-Silero-on-AEC
  is the production VAD default; why server-VAD configs failed.
- [HANDOFF-aec.md](HANDOFF-aec.md) — AEC engines, leg vocabulary, the
  topology-is-fixed rule.
- [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md) — the capture-and-label
  corpus pattern PR 7's threshold derivation extends.

---

## Appendix — historical: 2026-05-23 design-space + costing record

> **Historical appendix (2026-05-23).** Everything below is the original
> research snapshot, costing the option space for robust barge-in under
> the earlier measure-first policy. Preserved for primary-source
> archaeology and option costing. **The AEC-topology rejection it
> records is still binding** (PipeWire `module-echo-cancel`, snd-aloop→PipeWire
> fanout, dual-USB-sink hardware-AEC retry, custom XVF firmware remain
> rejected; targeted single-knob OS-layer fixes localized by measurement
> are still acceptable). Its **"today / current reality" snapshots are
> superseded** by the operational sections above — read it for the
> *reasoning*, not for current state.

> ### ⚠️ Original 2026-05-23 warning
>
> [`AGENTS.md`](../AGENTS.md) and
> [`CONTRIBUTING.md`](../CONTRIBUTING.md) (both updated 2026-05-23)
> establish a standing rule: **for the AEC subsystem,
> architectural changes are not reviewable; engine swaps and
> tuning are.** The named-rejected paths include "PipeWire
> `module-echo-cancel`," "replacing snd-aloop with PipeWire
> fanout," "dual-USB-sink hardware-AEC retry," and "custom XVF
> firmware." Targeted single-knob OS-layer fixes (a specific
> ALSA setting, a kernel module parameter) ARE acceptable when
> measurement has localized the root cause to that layer.
>
> Options A and B below are **explicitly the kind of speculative
> re-architecture that policy rejects.** This doc records the
> reasoning so a future contributor (or future Claude session)
> doesn't re-derive the costing from scratch and propose what's
> already been declined. It is **not** a menu of live options.
>
> The operative recommendation is **Option C** — measure
> first, then if measurement justifies it, address with
> engine-internal tuning inside `jasper/cli/aec_bridge.py` or
> the `jasper_aec3` binding. That path stays inside the policy.
>
> On 2026-05-27 the product direction changed: robust barge-in during
> assistant speech is now a known requirement, and the deliberate
> architecture direction is the JTS-native output owner described in
> [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md).
> Keep the costing below as context, not as the current recommendation.

The trigger: a future feature request — make barge-in work
reliably under loud music. The current implementation works for
"quiet room, raised voice" but has known limits (see
[Today's barge-in: what works](#todays-barge-in-what-works-and-what-doesnt)).
The naive fix ("put TTS in the AEC reference") collides with
the audio architecture in non-obvious ways. This doc explains
why, costs the architectural options for the historical record,
and recommends the measurement path that stays inside policy.

---

## Today's barge-in: what works and what doesn't

JTS supports barge-in today via **local Silero VAD gating**, not
AEC. Mechanism:

- During a turn, [`WakeLoop._handle_session_frame`](../jasper/voice_daemon.py)
  runs every mic frame through Silero VAD ([`SpeechVAD`](../jasper/voice_daemon.py)).
- A frame is forwarded to the realtime LLM **only if** the VAD
  score exceeds `END_OF_UTTERANCE_SPEECH_THRESHOLD = 0.15`.
- TTS bleed through the mic typically scores below that
  threshold (the comment block at
  [`voice_daemon.py:315-327`](../jasper/voice_daemon.py) records
  the calibration: TTS bottoms out ~0.13, real soft speech ~0.19,
  music vocals ~0.13 — 0.15 sits between them).
- When the gate fires, [`tts.flush()`](../jasper/audio_io.py)
  aborts the ALSA output buffer for sub-50 ms cutoff.

This works because the discrimination window between "TTS-tail
probability" and "real speech probability" is large enough at
typical levels. The whole defense is that 0.06 gap.

### Where this approach is fragile

1. **Loud music under TTS reduces the gap.** When music is
   playing, both TTS bleed AND music vocals contribute to the
   mic. The VAD score floor rises. Real user speech needs to
   stand out further from the noise. Anecdotally, raised-voice
   barge-in still works; conversational-voice barge-in becomes
   unreliable.
2. **TTS amplitude isn't fixed.** `jasper-outputd` boosts assistant
   gain when content is loud, based on measured content loudness and
   provider source-loudness profiles. Louder TTS → more bleed in
   the mic → bleed score creeps toward 0.15.
3. **Server-side auto VAD is structurally off.** All three
   voice providers (Gemini Live, OpenAI Realtime, Grok) run with
   `automatic_activity_detection.disabled = true` via the
   manual-VAD path (see
   [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)).
   The server can't tell its own TTS from user speech, so
   re-enabling auto-VAD with the current mic substrate would
   bring back the self-interrupt loop the manual-VAD path was
   built to escape.
4. **No telemetry on barge-in success/fail rates today.** We
   don't have data on how often this actually breaks in real
   use. (See [Option C](#option-c--measure-first-vad-only-may-be-enough)
   below.)

### What "robust" barge-in would mean

Confidence that the user can interrupt the assistant mid-utterance
with a normal speaking voice, including while music is playing
underneath TTS. As a user-experience bar: comparable to ChatGPT
Voice / Alexa / Google Assistant.

That bar requires the mic signal — at the point where wake / VAD
/ server-side activity detection look at it — to be **clean of
the speaker's own output**. Which means AEC has to cancel both
music AND TTS, not just music.

---

## What the canonical AEC architecture says

Before any architectural choice, the canonical reference
architecture for AEC with multiple sound sources is worth
stating, because it constrains the rest of the discussion.

The professional installed-audio guidance and academic literature
on AEC converges on three properties:

1. **The reference signal must match exactly what was played
   through the speaker.** Anything that happens between the
   reference tap and the speaker (mixing, post-EQ, ducking,
   resampling) is invisible to the AEC and degrades cancellation.
2. **A single pre-mixed reference is the standard.** Multiple
   sources are mixed *before* the reference tap; the AEC sees
   one combined far-end signal.
3. **Delay between reference and capture must be tightly
   bounded.** AEC engines estimate this delay continuously but
   converge slowly and stay fragile if the underlying alignment
   wanders.

Citations from the research pass for this doc:

- [Switchboard Audio's AEC3 explainer](https://switchboard.audio/hub/how-webrtc-aec3-works/):
  *"if the reference signal doesn't match what was actually
  played through the speaker (e.g., because of post-processing
  or mixing after the reference tap point), AEC performance
  will suffer."*
- [Symetrix — Tips & Tricks for Successful AEC](https://www.symetrix.co/knowledge/tips-tricks-for-successful-aec/):
  *"The AEC reference point and local speaker outputs need to
  be tapped after all processing, and right before the outputs.
  …The AEC reference should receive a mix of all the far-end
  and program audio that will be played through the
  loudspeakers."*
- [Bose Professional AEC guide](https://pro.bose.com/en_us/support/article/aec_a_complete_guide.html)
  (lookup at write time returned 404; cached title and excerpt
  confirm same guidance — re-check if the canonical URL moves).
- [Biamp Tesira — Per-channel AEC referencing](https://support.biamp.com/Tesira/Programming/Per-channel_AEC_referencing):
  same pattern — one reference per AEC, drawn from the mixed
  output.

For the specific engine JTS uses (WebRTC AEC3 via
`libwebrtc-audio-processing-1`):

- AEC3 exposes `ProcessReverseStream()` for the far-end (single
  reference) and `ProcessStream()` for the near-end mic. There
  is no native multi-reference input.
- AEC3's delay estimator cross-correlates the single reference
  with the capture; if you feed it a software sum of two
  references with mismatched path delays, the estimator
  converges on a compromise that's wrong for both.
- See [issue 42221406 on the WebRTC tracker](https://issues.webrtc.org/issues/42221406)
  for community discussion of delay-estimator edge cases.

The Linux-world consensus matches: [PipeWire's
`module-echo-cancel`](https://docs.pipewire.org/page_module_echo_cancel.html)
creates a *sink* abstraction; every audio source writes to that
sink; the sink's content IS the reference. Same pattern as the
pro-audio guidance, packaged as a Linux module.

---

## Today's reality on JTS

The current topology, for reference. Full details in
[audio-paths.md](audio-paths.md).

```
MUSIC chain (gets CamillaDSP + main_volume ducking)
    renderers → private fan-in lanes:
                  librespot_substream   → hw:Loopback,0,0
                  shairport_substream   → hw:Loopback,0,1
                  bluealsa_substream    → hw:Loopback,0,2
                  usbsink_substream     → hw:Loopback,0,3
              → jasper-fanin sums lanes to hw:Loopback,0,7
              → snd-aloop capture side hw:Loopback,1,7
              → CamillaDSP
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers

TTS chain (bypasses CamillaDSP)
    jasper-voice TtsPlayout → pcm.jasper_out (dmix on dongle)
                            → dongle → amp → speakers
```

The renderer-side dmix (`pcm.jasper_renderer_mix`, fronted by
`pcm.jasper_renderer_in`) was added in PR #214 (2026-05-22) to
let the three renderers hold the loopback simultaneously —
resolving the Spotify Connect handover bug. It was retired on
2026-05-26 after AirPlay validation showed the fan-in topology was
both cleaner and more reliable. **That retired dmix remains prior art
for the convergence sink pattern Option A below proposes — same idiom,
different location in the chain.**

The two chains now converge at `jasper-fanin` before CamillaDSP, and the
AEC bridge consumes outputd's UDP speaker monitor as its normal reference.
That closes the old "TTS invisible to AEC" gap and also moves the
reference downstream of CamillaDSP filters/crossover and outputd sink
selection. It is the final software/electrical speaker reference; DAC,
amp, driver, cabinet, and room behavior are still only observable through
the microphone.

This means the canonical final-output reference is now published by the
final output owner, so barge-in no longer needs a permanent TTS-only or
content-only half-architecture. Remaining work is now playout accounting,
flush/truncation coordination, and empirical tuning rather than inventing
another reference tap.

---

## The trap: "just put TTS in the AEC reference"

The obvious-looking fix is to tap TTS separately and add it to
the reference. Either:

- (A) Route TTS through the snd-aloop / CamillaDSP chain so it
  appears in the existing reference.
- (B) Add a second ALSA dsnoop on the TTS path; sum two
  references in the AEC bridge before feeding AEC3.
- (C) Use the ALSA `multi` plugin to fork TTS into both the
  dongle and the loopback.
- (D) Have `TtsPlayout` tee samples over UDP to the AEC bridge
  alongside the existing ALSA write.

**All four are wrong for the same underlying reason: they create
a reference that doesn't exactly match what was played.**

Each fails in a slightly different way:

- **(A)** breaks the assistant loudness matcher's load-bearing
  assumption ("assistant audio bypasses CamillaDSP" — see
  [audio-paths.md](audio-paths.md) "Assistant Loudness Matching");
  subjects TTS to music ducking; requires
  either a second CamillaDSP instance or invasive CamillaDSP
  topology surgery (CamillaDSP supports only one capture per
  process).
- **(B)** re-introduces a second snd-aloop cable, which is
  exactly the failure class
  [HANDOFF-resilience.md](HANDOFF-resilience.md) PR #93
  eliminated. The `loopback_cable` kernel state still wedges on
  SIGKILL.
- **(C)** explicitly rejected in
  [audio-paths.md:18-24](audio-paths.md) — the ALSA `multi`
  plugin xrun-storms with bursty writers (TTS is bursty).
- **(D)** the most tempting; turns out to be the most fragile.
  Music ref path: ALSA capture from snd-aloop, low-latency,
  jitter-free. TTS ref path: UDP from `TtsPlayout`, variable
  jitter. **Different end-to-end path delays.** AEC3 estimates
  one delay; with mismatched path delays for the two summed
  sources, the estimator converges on a compromise that's
  wrong for both. Calibration drift over firmware/kernel/dongle
  changes turns this into a slow-rotting fragility.

Patents exist for hybrid / combined-reference AEC schemes (e.g.
[US9653060B1 — Hybrid reference signal for acoustic echo
cancellation](https://patents.google.com/patent/US9653060B1/en),
[US11477327 — Post-mixing acoustic echo
cancellation](https://patents.justia.com/patent/11477327))
precisely because doing this correctly is non-obvious signal
processing. JTS should not be inventing patentable DSP to enable
barge-in.

**Rule for future contributors:** if you find yourself proposing
to give AEC3 two references and sum them, stop. Read this section.
The single pre-mixed reference is the canonical answer; pick one
of the options below instead.

---

## Option A — Stay ALSA-only; add a software convergence sink

> **Policy status: rejected as speculative re-architecture.** Per the
> standing rule in [AGENTS.md](../AGENTS.md), changes that
> restructure the snd-aloop / dmix topology around AEC are not
> reviewable today. The costing below is preserved as decision-record
> only.

Restructure so music + TTS converge at a software mix point
*before* the dongle. The minimal shape:

```
renderers → existing music chain → CamillaDSP ──┐
                                                ├──> dmix(jasper_premix)
TTS ────────────────────────────────────────────┘        │
                                            snd-aloop sub2 (new)
                                                         │
                                          ┌──────────────┴──────────────┐
                                          ▼                             ▼
                            jasper-output-bridge          AEC bridge (ref input)
                                          │
                                   dongle dmix → DAC
```

Both CamillaDSP and `TtsPlayout` write to a new dmix
(`jasper_premix`), which sits on snd-aloop substream 2. A new
small always-on daemon (`jasper-output-bridge`) reads sub2
capture and writes to the dongle. The AEC bridge reads the same
sub2 capture as its reference. Single pre-mixed signal, perfect
time alignment (sample-locked dmix), canonical AEC architecture.

### What you'd build

Prior art is already in-tree: PR #214 added `pcm.jasper_renderer_mix`
as a multi-writer dmix in front of the loopback for renderer
convergence. This option uses the **same idiom one layer further
down the chain** — converging music + TTS *after* CamillaDSP.

1. New `pcm.jasper_premix` dmix definition in
   [`deploy/alsa/asoundrc.jasper`](../deploy/alsa/asoundrc.jasper)
   wrapping `hw:Loopback,0,2`. ipc_key 7780 (unique vs the existing
   7777=jasper_out, 7778=jasper_capture, 7779=jasper_renderer_mix).
2. Update [`deploy/camilladsp/v1.yml`](../deploy/camilladsp/v1.yml)
   so CamillaDSP's playback target is `jasper_premix` instead of
   `jasper_out`.
3. Update [`jasper/config.py`](../jasper/config.py)
   `tts_device` default to `jasper_premix`.
4. New daemon `jasper-output-bridge` (small Python or Rust):
   reads from `pcm.jasper_premix_capture` (dsnoop on
   `hw:Loopback,1,2`), writes to `pcm.jasper_out` (existing dmix
   on dongle). Mirrors the structure of `jasper-aec-bridge`,
   including `sd_notify` Tier 1+2 hardening from
   [`jasper/watchdog.py`](../jasper/watchdog.py).
5. Update [`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py)
   `REF_DEVICE` to point at the new dsnoop on sub2.
6. Update [`audio-paths.md`](audio-paths.md) and
   [`HANDOFF-aec.md`](HANDOFF-aec.md) topology diagrams.

### Costs

- **One new always-on daemon.** ~30 MB Pss, comparable to
  jasper-aec-bridge. Needs Tier 1+2 watchdog.
- **~20-60 ms additional music latency.** One extra dmix hop
  (~10-20 ms) + the snd-aloop cable (~10-20 ms) + the output
  bridge's playback buffer (~10-20 ms). Depending on
  `period_size` tuning. Most music sources tolerate this;
  Bluetooth A2DP with video sync is the most sensitive.
- **Expanded snd-aloop kernel-state surface.** A second cable
  (sub2) joins the existing music chain cable (sub0) as wedge
  risk. If `jasper-output-bridge` is SIGKILL'd, sub2 wedges and
  audio stops until `rmmod snd_aloop && modprobe` (with all
  consumers stopped). Tier 4 in
  [HANDOFF-resilience.md](HANDOFF-resilience.md) becomes more
  likely to need wiring.
- **One more thing to think about during install / deploy /
  reconcile.** A new unit, new asoundrc clause, new failure
  mode in `jasper-doctor`.

### What it preserves

- Assistant loudness matching continues to work — TTS still bypasses
  CamillaDSP, so the "TTS doesn't get ducked" property survives, and
  outputd remains the final mix point where content loudness and
  provider source profiles can be compared.
- Pure ALSA stack — no new audio server, no PipeWire migration.
- Stays inside the architectural framework `HANDOFF-resilience.md`
  established (sd_notify watchdog, fault-isolated daemons,
  UDP-localhost for mic transport).

### When this is the right answer

- You want barge-in robustness *and* you want to keep ALSA as the
  audio substrate.
- You're willing to take on one more daemon + one more snd-aloop
  cable in exchange for canonical AEC architecture.
- The latency cost (~20-60 ms on music) is acceptable.

---

## Option B — Migrate to PipeWire

> **Policy status: rejected by name.** [AGENTS.md](../AGENTS.md)
> "Architecture is fixed; swap the engine, not the topology"
> explicitly names "PipeWire `module-echo-cancel`" and "replacing
> snd-aloop with PipeWire fanout" as paths not to propose.
> [CONTRIBUTING.md](../CONTRIBUTING.md) "Working on a sensitive
> subsystem" repeats the constraint for external contributors.
> The costing below is preserved as decision-record only — if
> future evidence ever warrants reopening the conversation, the
> trade-off table is here.

PipeWire's `module-echo-cancel` does Option A out of the box,
plus several second-order wins JTS would otherwise build
incrementally.

### What you'd build

1. Install `pipewire`, `wireplumber`, `pipewire-pulse`, and
   `libspa-aec-webrtc` packages on the Pi.
2. Stop using `/root/.asoundrc` for routing; declare a PipeWire
   graph (in `~/.config/pipewire/` or
   `/etc/pipewire/pipewire.conf.d/`) with:
   - A virtual sink (`jasper-premix`) that all renderers and TTS
     write to.
   - `module-echo-cancel` consuming `jasper-premix` as the
     reference and the XVF chip's mic as the capture, exposing
     an echo-cancelled source for `jasper-voice`.
   - A loopback from `jasper-premix` to the dongle's hardware
     PCM.
3. Reconfigure renderers (`shairport-sync.conf`, `librespot.service`,
   `bluez-alsa-aplay.service`) to write to the PipeWire sink
   (typically via `pipewire-pulse` which presents a Pulse
   server API, or directly as PipeWire clients).
4. Update [`deploy/camilladsp/v1.yml`](../deploy/camilladsp/v1.yml)
   — CamillaDSP supports PipeWire as both a capture and a
   playback backend.
5. Retire `jasper-aec-bridge` — the
   `libspa-aec-webrtc` library inside PipeWire handles this.
6. Retire `pcm.jasper_capture`, `pcm.jasper_ref`,
   `pcm.jasper_out`, the snd-aloop module entirely.
7. Update or rewrite: [`audio-paths.md`](audio-paths.md),
   [`HANDOFF-aec.md`](HANDOFF-aec.md),
   [`HANDOFF-resilience.md`](HANDOFF-resilience.md),
   [`HANDOFF-airplay.md`](HANDOFF-airplay.md),
   `BRINGUP.md`, `install.sh`, and the various wizards that
   touch audio.
8. Retest *everything* on the new substrate. Every renderer,
   every voice provider, ducking, volume, AEC, AirPlay sync,
   Bluetooth A2DP latency, wake event capture.

### Second-order wins this gets you

These are real and worth weighing:

- **Solves the multi-mic arbitration problem cleanly.**
  PipeWire's `module-combine-stream` is the canonical primitive
  for the planned multi-satellite-mic feature documented in
  [satellites.md](satellites.md). Today's plan is to roll a
  custom arbitration daemon; PipeWire reduces that to graph
  config.
- **Better resampler defaults.** PipeWire's SPA resampler is
  generally on par with libsamplerate-best and benefits from
  more recent tuning than the ALSA defaults. The 12 dB 4-8 kHz
  loss in shairport's plug-resampler that motivated PR #75 is
  exactly the kind of foot-gun PipeWire would have caught
  earlier.
- **Bluetooth A2DP comes from PipeWire-native code path.**
  Retires `bluez-alsa-aplay` and the per-DAC asoundrc
  contortions for it.
- **Foundation for future audio integrations.** Snapcast,
  multi-room, network audio — PipeWire is where the Linux audio
  ecosystem is going, and these get easier on it.
- **Declarative routing graph.** The current asoundrc + multiple
  systemd units + ad-hoc snd-aloop topology becomes one config
  file describing the graph.

### Costs

- **~50-80 MB additional Pss** for `pipewire` + `wireplumber` +
  `pipewire-pulse`. (Verify on actual Pi 5 hardware before
  committing — published numbers vary by distro.) JTS today
  sits around 770 MB / 2 GB; PipeWire would land around 820-850
  MB. On the 1 GB Pi build, this matters more.
- **Multi-week project.** Replacing the audio substrate
  touches install, deploy, BRINGUP, doctor, every renderer, the
  AEC bridge, CamillaDSP integration, every wizard that touches
  audio, every test. Honest estimate: 2-4 weeks of focused work,
  longer if regressions surface.
- **Different mental model.** ALSA + asoundrc is "config files
  that processes evaluate locally." PipeWire is "a graph
  evaluated by a daemon that owns the audio path." Foreign
  idiom for current JTS, which is otherwise composed of small
  process-per-purpose daemons.
- **One more long-running daemon (and its session manager) in
  the resilience ladder.** PipeWire and wireplumber both need
  to be tracked. Their failure modes are different from
  ALSA's. The community has shaken out most of the early bugs
  but you'd be inheriting a new dependency surface.
- **Migration risk.** Any of the audio properties JTS spent
  effort tuning — the precise resampler choice, the per-renderer
  format negotiation, the AirPlay sync behaviour, the CamillaDSP
  format lock — needs to be re-validated. Some of these have
  PRs documenting subtle bugs already fixed; some of those
  bug-classes have PipeWire-side equivalents to discover.
- **Re-derives the resilience story.** The Tier 1-5 ladder in
  [HANDOFF-resilience.md](HANDOFF-resilience.md) was built
  against ALSA failure modes. PipeWire has its own failure
  modes (graph deadlocks, wireplumber restarts, RTKit
  priorities). Some Tier 1-2 work would re-apply; some would
  need fresh design.

### Comparison snapshot

| Property | Stay ALSA (Option A) | Migrate PipeWire (Option B) |
|---|---|---|
| Canonical AEC architecture | Yes (with new daemon + new cable) | Yes (out of box) |
| Multi-mic arbitration foundation | Custom code | `module-combine-stream` |
| Resource cost | +~30 MB Pss | +~50-80 MB Pss |
| Implementation effort | Days–1 week | 2-4 weeks + retest |
| Migration risk | Localized (one new daemon) | Replaces audio stack |
| Match to current JTS idiom | High | Low |
| New dependency surface | None | PipeWire + Wireplumber + SPA |
| Long-term ecosystem fit | Diverges from Linux mainstream | Aligns with where Linux audio is going |

### When this is the right answer

- Barge-in robustness has been measured to actually matter (see
  Option C).
- You're also seeing the multi-renderer contention bug in real
  use, the multi-mic-satellite arbitration is on the near
  roadmap, and the ALSA-only patchwork is starting to feel like
  it's accumulating workarounds rather than solving root causes.
- You have 2-4 weeks of focused time and willingness to retest
  the full audio chain.

### Important nuance on the existing exclusion

[HANDOFF-resilience.md](HANDOFF-resilience.md) lists "PipeWire
migration — out of scope per project policy" in its "what we
explicitly did NOT do" section. The reasoning quoted:

> The resilience win comes from removing snd-aloop from the
> bridge↔voice path entirely, not from replacing the userspace
> audio stack.

That exclusion was scoped to a specific resilience question (do
we need PipeWire to fix the snd-aloop bridge wedge?) and the
answer was correctly "no, UDP is enough." The exclusion was not
a general "PipeWire is forever inappropriate for JTS." Barge-in
is a different motivation. Re-evaluating the trade-off is a
legitimate move; it doesn't contradict prior decisions.

---

## Option C — Measure first; VAD-only may be enough

The recommended *immediate* next step. Before committing to
either A or B, gather evidence on whether the current VAD-only
barge-in is actually insufficient under real use.

### What you'd build

Less than the first version of this doc implied — much of the
measurement substrate already exists or is in active development
under [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md). The
barge-in story extends that program rather than building parallel
infrastructure. Concrete additions:

1. Per turn during TTS playback, log every frame where Silero VAD
   score crosses an "attempted barge-in" threshold (e.g. > 0.10,
   below the 0.15 gate, to catch near-misses too). Live in
   [`jasper/voice_daemon.py`](../jasper/voice_daemon.py) alongside
   the existing in-session VAD plumbing at line ~2365.
2. For each such moment, record: TTS RMS at the moment, music
   RMS at the moment, whether the gate (≥0.15) actually fired,
   time from gate fire to `tts.flush()` completion, and whether
   the user re-spoke within ~5 s (proxy for "first attempt
   failed, user tried again").
3. Capture short audio clips of the mic and the AEC reference
   around each attempt — extend the existing wake-events capture
   ring buffers in [`jasper/wake_events.py`](../jasper/wake_events.py)
   with a "barge-in" event type. Reuses the SQLite schema and the
   500 MB rolling audio retention from
   [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md).
4. Run for two to four weeks of normal household use.
5. Use the existing `bash scripts/fetch-wake-events.sh` flow plus
   a small query script to summarise. The capture / scoring
   tooling being developed under PR #206 (mic-quality-v2) is the
   natural place to add barge-in-specific scoring queries —
   coordinate so the indexes generalize across both workstreams
   rather than diverge.

### What the data tells you

Three buckets the data is likely to fall into:

- **VAD-only is already adequate.** Most barge-in attempts get
  a confident score (~0.3+), gate fires promptly, no
  re-attempts. You've saved yourself a multi-week project.
- **VAD-only is adequate *except* in specific conditions.**
  Failure cluster is tightly correlated with one or two
  conditions (very loud music, particular TTS phonemes,
  specific user voices). May be fixable with targeted tuning
  (per-music-level threshold, different VAD model, longer
  refractory) — cheaper than full architectural change.
- **VAD-only is genuinely insufficient.** Failure rate is high
  enough across normal conditions that architectural rework is
  justified. Now you have real evidence to choose A vs B with,
  rather than speculation.

### Why this is the recommended first move

- Costs roughly a day of instrumentation work.
- Defers a 2-4 week decision until you have data.
- Matches the JTS pattern from
  [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md):
  measure before re-architecting. The shipped wake-event
  capture system exists exactly for this kind of "is the thing
  we think is broken actually broken, and how badly" question.
- The instrumentation itself is durable. Even after a barge-in
  architectural change, having the success-rate metric makes
  the change's improvement (or regression) measurable.

---

## Other alternatives considered (and mostly rejected)

For completeness, things that came up in the research pass and
didn't survive scrutiny.

### Hardware AEC, revisited

> **Policy status for barge-in: still rejected by name.**
> [AGENTS.md](../AGENTS.md) names "dual-USB-sink hardware-AEC retry"
> and "custom XVF firmware" as paths not to propose. The wake-detection
> chip-AEC carve-out in [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md)
> does not reopen hardware AEC as the barge-in architecture.

The XVF3800's on-chip AEC was disabled deliberately
([HANDOFF-aec.md](HANDOFF-aec.md): the chip's AEC assumed the
chip drove the speaker via its own codec, which JTS doesn't —
audio routes through the Apple dongle). A topology change that
returned the speaker drive to the chip's codec would re-enable
chip AEC and solve barge-in cleanly. But the dongle was chosen
for DAC quality; the chip's AIC3104 is meaningfully worse. Hard
to imagine this trade landing as positive.

The convergence question (does chip AEC adapt in the current dongle
topology when fed a USB-IN reference signal?) has its own
user-authorized wake-detection carve-out. The 2026-05-29 result was
positive for fixed `150°`/`210°` ASR beams, and that path is now an
opt-in wake leg with outputd's direct final-output reference fanout
and a bridge `:9876` repoint. This is deliberately narrower than
barge-in: it scores "Jarvis" on chip beams; it does not solve
assistant-speech cancellation, playout accounting, or conversational
interruption. See [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md)
and [HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md).
§2.4. That promotion does **not** re-open the barge-in trade here.)
**The barge-in carve-out is scoped to the convergence test only**: it
does not re-open the codec-swap dismissal above, nor PipeWire
`module-echo-cancel`, dual-USB-sink, or custom firmware. Agents
proposing those remain bound by the policy.

### Different AEC engine

SpeexDSP's AEC is the main alternative. It's also single-
reference (see [Speex EchoState docs](https://www.speex.org/docs/api/speex-api-reference/group__SpeexEchoState.html))
— it would face the same multi-source problem. AEC3 is the
better engine on most measures; switching engines doesn't help.

### Train a custom VAD

A purpose-built VAD that's better at distinguishing JTS-TTS
specifically from user speech. Real but expensive — would need
a labelled dataset of "TTS vs user during TTS" pairs, model
training, on-device inference budget. Buys some improvement over
the off-the-shelf Silero model without changing the AEC
architecture, but the ceiling is still "we can't see the TTS
signal cleanly." Not a substitute for the architectural fix
when conditions push the VAD's discrimination window past its
limit.

### Push-to-talk barge-in via the dial

The rotary dial has a "hold to talk" Gemini session. A
variation: while TTS is playing, holding the dial drops TTS
volume to zero AND opens an interrupt path. Reliable, but
trades the no-physical-interaction property of voice barge-in
for a button press. Worth offering as a complementary capability,
not a substitute.

### Mic mute during TTS

Don't stream mic frames at all during TTS. Trivially eliminates
self-wake and the discrimination problem. Also trivially
eliminates barge-in. Anti-feature.

### JACK instead of PipeWire

JACK is a pro-audio routing daemon with native multi-writer
sinks. Pre-dates PipeWire by years. Active community, but it's
oriented toward low-latency studio use, not embedded smart-home
audio. PipeWire absorbed most of JACK's capabilities and added
sensible defaults for consumer use cases. JACK is the worse fit
for JTS for the same reasons PipeWire is the canonical Linux
audio answer in 2026.

### Single-purpose UDP-based mixer daemon

A custom small daemon that owns the dongle, accepts music
(snd-aloop or UDP) and TTS (UDP), mixes them, writes to the
dongle, and forwards the mix to the AEC bridge over UDP. This
is "Option A done with UDP instead of a second snd-aloop
cable" — keeps the canonical single-reference architecture and
avoids the kernel-state wedge risk. But CamillaDSP doesn't
speak UDP; you'd need an intermediate process to ferry
CamillaDSP's ALSA output into the UDP mixer, which itself
re-introduces some of the failure surface. Possibly worth
exploring if Option A's snd-aloop sub2 cable proves to be the
specific thing that wedges, but not the obvious starting point.

---

## Open questions

The decision can't be made cold. Before either Option A or
Option B lands, these need answers:

1. **What's the actual barge-in success rate today?** Required.
   Drives whether this work is needed at all. → Option C.
2. **Is the failure mode concentrated in specific conditions?**
   If yes, may admit a tuning fix rather than architectural
   change. → Option C.
3. **What's PipeWire's real Pss on a Pi 5 running JTS?** The
   50-80 MB number is a literature estimate; measure on actual
   hardware (boot a test image with PipeWire installed, run
   nothing else, take `smem`). Drives the "fits on 1 GB?"
   question.
4. **Does CamillaDSP's PipeWire integration work cleanly in
   2026?** CamillaDSP gained PipeWire support a few years ago;
   verify it's stable and that the `main_volume` ducking still
   works the way `Ducker` expects.
5. **What's the actual latency budget for music?** AirPlay 2 is
   sync-tolerant; Bluetooth A2DP-with-video is not. Measure
   AirPlay sync drift and BT lip-sync delta with a 60 ms
   playback latency increase before committing to Option A.
6. **What's the migration path for the wake-event corpus and
   the regression test scenarios?** A PipeWire migration
   shouldn't invalidate the labelled wake-event corpus or the
   voice-eval regression suite. Sanity-check that AEC reference
   format / mic capture format / sampling rate stay compatible.
7. **Does the household actually use barge-in?** If the user
   never tries to interrupt TTS in real life (because TTS turns
   are short, because they wait), the whole question is
   academic. Option C surfaces this.

---

## Recommendation

The production fan-in topology resolved the renderer contention and
retired the renderer-side dmix from PR #214. What remains is the
barge-in question itself. The path that stays inside the standing
policy is:

1. **Build Option C instrumentation.** ~1 day. Extends the
   mic-quality-v2 measurement substrate ([HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md))
   rather than building parallel infrastructure. Run for 2-4
   weeks. Get real data on how often VAD-only barge-in actually
   fails under normal household use.
2. **Based on the data**, choose:
   - **If VAD-only is adequate:** declare barge-in done, archive
     this doc with a "resolved 2026-XX: VAD-only met the bar"
     note. No architectural change needed.
   - **If VAD-only is inadequate but the failure cluster is
     narrow:** try targeted VAD tuning, engine-internal AEC3
     knob changes ([AGENTS.md](../AGENTS.md) "AEC bridge —
     reconciler toggle"), or single-knob OS-layer fixes that
     measurement has localized to a specific layer. All in policy.
   - **If VAD-only is genuinely insufficient and engine-internal
     tuning hits a ceiling:** the policy question reopens. At that
     point, the Option A / Option B costing in this doc is the
     starting record for whether the trade has changed enough to
     reconsider. **Reopening the policy is the user's call, not
     an agent's.** Surface the data and the trade; don't propose
     the architecture change.

The decision is not urgent and shouldn't be made on speculation.
The data-collection step is the highest-leverage move and the
only one currently in-policy.

---

## References

External sources surveyed for this doc:

- [Switchboard Audio — How WebRTC AEC3 Works](https://switchboard.audio/hub/how-webrtc-aec3-works/)
- [PipeWire — module-echo-cancel documentation](https://docs.pipewire.org/page_module_echo_cancel.html)
- [PipeWire — module-loopback documentation](https://docs.pipewire.org/page_module_loopback.html)
- [Symetrix — Tips & Tricks for Successful AEC](https://www.symetrix.co/knowledge/tips-tricks-for-successful-aec/)
- [Bose Professional — AEC: A Complete Guide to Reference](https://pro.bose.com/en_us/support/article/aec_a_complete_guide.html) (URL was returning 404 at write time — search for current canonical URL if revisiting)
- [Biamp — Per-channel AEC referencing](https://support.biamp.com/Tesira/Programming/Per-channel_AEC_referencing)
- [XMOS — Choosing an Acoustic Echo Canceller for voice-enabled smart home products](https://www.xmos.com/developer/blog/huw/post/choosing-acoustic-echo-canceller-voice-enabled-smart-home-products)
- [voice-engine/ec — Echo Canceller for Linux on Pi (uses SpeexDSP)](https://github.com/voice-engine/ec)
- [WebRTC AEC3 capture signal delay tracker issue](https://issues.webrtc.org/issues/42221406)
- [Speex EchoState API reference](https://www.speex.org/docs/api/speex-api-reference/group__SpeexEchoState.html)
- [Hybrid reference signal for AEC — US9653060B1](https://patents.google.com/patent/US9653060B1/en)
- [Post-mixing acoustic echo cancellation — US11477327](https://patents.justia.com/patent/11477327)
- [Multichannel acoustic echo cancellation — US9967661B1](https://patents.google.com/patent/US9967661B1/en)
- [Home Assistant Voice Preview Edition](https://www.home-assistant.io/voice-pe/)
- [Arch Linux Forums — snd-aloop documentation thread](https://bbs.archlinux.org/viewtopic.php?id=276688)

Internal cross-references (for the next reader):

- [audio-paths.md](audio-paths.md) — current routing topology,
  how TTS enters fan-in before CamillaDSP, and how fan-in matches
  assistant loudness.
- [HANDOFF-aec.md](HANDOFF-aec.md) — AEC engine choice, the
  chip-AEC-disabled investigation, current software AEC tuning.
- [HANDOFF-resilience.md](HANDOFF-resilience.md) — the resilience
  ladder, the snd-aloop wedge story, the PipeWire-exclusion
  framing (and its specific scope).
- [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)
  — why manual VAD is used today, the
  `automatic_activity_detection.disabled` decision, the
  NO_INTERRUPTION history.
- [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md) — the
  capture-and-label pattern Option C would extend.
- [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) — active
  workstream building the measurement infrastructure Option C
  would extend rather than duplicate.
- [satellites.md](satellites.md) — multi-mic arbitration
  design that Option B would simplify.

---

Last verified: 2026-06-22 (operational sections — current state + plan —
verified against the tree at/after `main` d2ef9122 which contains #532;
provider mechanics rechecked against OpenAI Realtime, Gemini Live, and
xAI Voice docs. The appendix below is the frozen 2026-05-23 historical
record and is intentionally not re-verified.)
