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
> The 2026-05-23 design-space + option-costing record (Options A/B/C, the
> alternatives considered, and the standing rejection of AEC-topology
> re-architecture) is preserved verbatim in
> [historical/barge-in-design-space-2026-05.md](historical/barge-in-design-space-2026-05.md).
> Its AEC-topology rejection is **still binding**; its "today/current reality"
> snapshots are superseded by the sections here.

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
  [historical/persistent-live-session-rework-2026-05.md](historical/persistent-live-session-rework-2026-05.md));
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
| 3. Contract alignment | Add `cancel_response`/`truncate_assistant_audio` to `session.py`; tolerate missing item ids | Session-contract test; `scripts/test-merge` green |
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
- **Threshold:** `JASPER_VAD_BARGE_IN_THRESHOLD` (`config.py`, default
  0.5) is wired — `_handle_playback_frame` reads
  `self._cfg.vad_barge_in_threshold` (`jasper/voice_daemon.py`) to gate
  the sustained-speech run that fires the local interrupt.
- **`event=` logs** via `jasper.log_event`: `barge.detected`
  (`leg=`, `silero=`, `sustained_ms=`), `barge.cancel` (`reason=` — the
  OpenAI/Grok pack's `response.cancel`), `barge.truncate`
  (`provider=`, `item_id=`, `audio_end_ms=`), `barge.truncate_skipped`
  (`reason=zero_played_ms` at WARN — the no-op-if-0 guard; `reason=no_item_id`
  at DEBUG), `barge.truncate_failed` (WARN — the truncate wire send errored),
  `barge.server_only` (not yet built, PR 7 — server fired but local
  didn't — false-barge suspect),
  `barge.flush_failed` (WARN),
  `barge.disabled_no_reference` (WARN, one-shot per daemon — the
  no-AEC-reference turn-start guard below), and
  `barge.disabled_push_to_talk` (WARN, its own one-shot latch so it
  cannot swallow the no-reference WARN a later wake turn owes the
  operator). Reuse the existing `event=tts_flush.playout_ack`.
- **Push-to-talk turns refuse barge-in.** A turn whose audio comes from
  an accessory mic (`_manual_endpoint_this_turn`, set in `_begin_turn`
  from `_active_manual_source`) scores frames from *that* stream, but
  `_barge_in_reference_available` was computed from `cfg.mic_device` —
  a different one. The self-interrupt guard has therefore not cleared
  the audio barge-in would run on, so the turn refuses and says so
  rather than going quietly inert.
- **`/state.voice.barge_in`** *(✅ landed PR-2)*: `enabled` (per active
  provider, read **fresh** in jasper-control's aggregator via
  `provider_state.read_barge_in_enabled` — not jasper-voice's
  session_status, since the daemon's `Config` is stale on a toggle),
  plus `last_at` / `count_session` / `last_leg` pulled through from the
  daemon's session_status firing counters.
- **Doctor:** (not yet built, PR 7) one `@doctor_check`
  `check_barge_in_config` — warn if a
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
- [historical/persistent-live-session-rework-2026-05.md](historical/persistent-live-session-rework-2026-05.md) —
  why manual VAD / `automatic_activity_detection.disabled` / `NO_INTERRUPTION`
  are used today (the decision barge-in revisits for Gemini).
- [HANDOFF-vad-experiments.md](HANDOFF-vad-experiments.md) — local-Silero-on-AEC
  is the production VAD default; why server-VAD configs failed.
- [HANDOFF-aec.md](HANDOFF-aec.md) — AEC engines, leg vocabulary, the
  topology-is-fixed rule.
- [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md) — the capture-and-label
  corpus pattern PR 7's threshold derivation extends.
- [historical/barge-in-design-space-2026-05.md](historical/barge-in-design-space-2026-05.md)
  — the frozen 2026-05-23 option costing and the binding topology rejection.

---

Last verified: 2026-06-22 (operational sections — current state + plan —
verified against the tree at/after `main` d2ef9122 which contains #532;
provider mechanics rechecked against OpenAI Realtime, Gemini Live, and
xAI Voice docs. 2026-08-26 triage pass moved the frozen 2026-05-23 appendix to
`docs/historical/` unchanged; the operational sections were not re-verified.)
