# Pending audit follow-ups

This file captures recommendations from the May 2026 architectural-pattern
audit (comparison of our daemon against Mycroft / OVOS / Rhasspy / Home
Assistant Voice / Willow / Pipecat / OpenAI Realtime / Amazon AVS) that
we **did not adopt immediately**, along with the rationale. The five
"Tier 1" changes from that audit landed in code; what's listed here is
everything else we decided to defer, reject, or investigate further.

## Already in place (recorded for the record)

These confirm what our codebase does well — should not regress:

- **Wake-gated streaming via manual VAD on the wire.** We don't merely
  gate locally; we use `automatic_activity_detection.disabled=True` so
  the protocol contract excludes pre-wake audio entirely.
  [`gemini_session.py:_build_config`](../jasper/voice/gemini_session.py).
- **Manual VAD + `activity_start`/`activity_end`.** Empirically required:
  we tried server-side auto VAD with pause-resume and turn 2 silently
  fails. Comments in `_build_config` document this in detail.
- **`interrupted` signal hooked into local TTS flush.** Wired in
  `_dispatch` (queues sentinel, sets event) and `_play_responses`
  (races write vs `wait_for_interrupt`, then `tts.flush()` which uses
  `stream.abort()` to drop buffered samples). Currently dormant
  because `NO_INTERRUPTION` prevents the server from emitting the
  signal — wakes up automatically the moment we drop NO_INTERRUPTION.
- **No nonlinear noise suppression in front of the LLM.** Clean signal
  path — only anti-aliased polyphase decimation in `MicCapture`.
- **Wake-word debounce.** `WakeWordDetector.reset()` after each fire +
  state flip to SESSION + 5 s refractory covers the multi-fire-on-long-
  wake-phrase concern.

## Tier 2 — gated on hardware AEC working

These are the highest-value improvements we **cannot adopt yet** because
they require a working AEC reference signal flowing to the XVF3800. The
current ALSA topology (single dmix on dongle, no fan-out to XVF USB-IN)
makes the chip's onboard hardware AEC inert. The dual-output topology
attempt (`route → multi → 2x[plug → dmix]` with heterogeneous rates)
failed with `snd_pcm_hw_params_any: EINVAL`; resolving that is the
gating change for this whole tier.

### Drop `NO_INTERRUPTION` once AEC is verified

`gemini_session.py` `realtime_input_config` currently has
`activity_handling=ActivityHandling.NO_INTERRUPTION` because without
AEC, TTS bleed reaches the mic and the server's auto VAD interprets it
as user activity, interrupting the model mid-response. With working
AEC, the chip cancels TTS bleed before it reaches Silero or the
server, so `NO_INTERRUPTION` becomes unnecessary and we can return to
the API default (`START_OF_ACTIVITY_INTERRUPTS`). The interrupt
plumbing is already wired and dormant; this is a one-line change once
hardware AEC is verified.

### Run wake-word detection during TTS for "stop" interruption (HA Voice PE pattern)

Currently `_handle_session_frame` does not feed audio to the wake
detector during the SESSION state. Home Assistant Voice PE loads a
dedicated short barge-in keyword model (e.g. "stop") only while TTS
is playing. Without working AEC, openWakeWord during TTS would trigger
on the model's own bleed (especially on phonemes the model itself
produces). Once AEC is working, this is a worthwhile addition — gives
users a hands-free interrupt path without requiring full real-time
barge-in.

### Lower wake refractory from 5 s → 2-3 s

`WAKE_REFRACTORY_SEC = 5.0`. Mature open-source projects cluster around
2-3 s. Our 5 s value is defensive against TTS playback tail bleed —
without AEC, dropping to 3 s would cause more music/TTS-tail false
fires. With working AEC the bleed is cancelled and a shorter refractory
is fine. The comment block in `voice_daemon.py:WAKE_REFRACTORY_SEC`
already calls out this dependency.

### Multi-trigger ducking (wake / listening / TTS playback)

Currently we duck on wake-fire and restore on turn-end. The Sonos /
Echo / HA Voice PE pattern is to duck other media at three separate
triggers with different depths — wake (lighter), listening (heaviest,
need ASR clarity), TTS speaking (medium, dialog needs to sit on top).
For us those three states collapse into one (`_begin_turn` → `_end_turn`).
Becomes meaningful only if we ever decouple them, which only makes
sense when AEC enables a clearer separation between "hearing the
user" and "speaking to the user."

## Tier 3 — investigate before adopting

### Pre-wake Silero gate before openWakeWord

Claim: cheaper than openWakeWord (CPU win) and reduces music false
fires (Silero scores music low; openWakeWord can be fooled by
speech-shaped vocals). Concerns: (a) Silero can mis-score quiet wake
words, hurting recall; (b) the CPU savings are negligible on Pi 5
where openWakeWord already runs well under budget; (c) building the
gate in `_handle_wake_frame` adds complexity for marginal gain.

To investigate properly: build an offline test bench (3 hours of
ambient music + 50 known wake-word samples), compare false-fire rate
and recall with/without the gate. If recall drops noticeably, reject;
otherwise the music false-fire reduction may be worth the work.

### Drop streaming chunk size from 80 ms → 20-40 ms

`MicCapture.OUTPUT_FRAME_SAMPLES = 1280` (80 ms) — the openWakeWord-
recommended frame size. Google's Live API recommends 20-40 ms for
"minimum latency", but in practice the first-chunk latency for our
turns is dominated by model warmup (3-5 s typical), not chunk
arrival. Decoupling mic-frame size from wake-frame size adds code
complexity. To investigate: cut `OUTPUT_FRAME_SAMPLES` to 320 (20 ms)
on a branch and watch the existing `first audio chunk in Xms` log
line for ≥ 100 ms improvement. If no visible improvement, reject.

## Tier 4 — rejected with rationale

### Adopt a 7-state machine (`IDLE → DETECT → SPEECH_BEGIN → RECORDING → THINKING → SPEAKING → IDLE`)

Reject. The 7-state model is meaningful when the local pipeline owns
ASR/NLU/TTS (Mycroft, OVOS, HA Voice). For us THINKING and SPEAKING
happen on Google's side and arrive interleaved (audio chunks while
turn_complete hasn't fired). Splitting them client-side adds
bookkeeping without driving any new behavior. The flags we already
track (`_user_speech_seen`, `_input_ended`, `_server_turn_complete`,
`_active_turn`) cover what state-machine purists would model as
states. Adopted the one part worth keeping: log every state
transition (Tier 1).

### `recording_timeout_with_silence: 3 s` (we use 5 s)

Reject. We have `NO_SPEECH_ABORT_SEC = 5.0`; the 3 s value would
abort cleanly on false-fires faster but risks aborting on slow
speakers. The cost-benefit favors keeping 5 s — if we have a music
false-fire, a 5 s vs 3 s window is barely perceptible.

### Speech-begin minimum duration (200 ms before flipping `_user_speech_seen`)

Reject. `END_OF_UTTERANCE_SPEECH_THRESHOLD` is deliberately low
(0.10) so soft speech registers fast. Adding a 200 ms minimum
re-introduces the "soft speaker doesn't get heard" failure mode the
loose threshold was tuned to avoid. The 0.5 s grace period
(`END_OF_UTTERANCE_GRACE_SEC`) already filters wake-word-tail false
positives at the front of each turn, which is the underlying concern.

### Three-trigger ducking on wake/listening/TTS-speaking

Reject. For us wake fire IS entry to listening, and TTS is
contiguous within the same turn. They collapse into one duck/restore
cycle. Re-ducking on TTS would be a no-op. The three-trigger model
is for systems where wake/listen/think/speak can be separated by
hundreds of ms each.

### Software AEC (WebRTC APM) as a stop-gap before hardware AEC

Reject as bridge work. We already have the right hardware on-device
(XVF3800 with on-chip AEC); writing the WebRTC APM integration would
be throwaway code whose only purpose is to bridge the gap until the
hardware AEC topology is fixed. Energy is better spent on the dual-
output ALSA topology (which is the actual blocker).

### `remove_silence: true` semantics for the active utterance

Reject for the active phase. The Whisper-hallucination problem this
recommendation prevents doesn't exist for Gemini Live + manual VAD —
we don't send trailing silence to the model (we only send up until
the silence detector fires `activity_end`; subsequent silent frames
are gated out). The one place this still matters is pre-roll (Tier 1) —
only replay frames where Silero scored above threshold within the
500 ms window. Worth adding if pre-roll proves to push too much room
tone into the model.

## Open conflicts with the report

These are recommendations from the report we have **empirical
evidence to deviate from**:

- **Auto VAD vs manual VAD on Gemini Live.** Report frames manual VAD
  as one of two options. Our empirical experience: with auto VAD on a
  persistent connection where we pause audio between turns, turn 2
  silently fails (server stays in turn-1's listening state, drops
  turn-2 audio). Manual VAD with explicit `activity_start`/
  `activity_end` markers is non-negotiable for our stack. Documented
  in `gemini_session.py:_build_config`'s comment block.

- **5 s wake refractory vs the report's 2-3 s.** Their value assumes
  AEC. Without working AEC, lower refractory means more music/TTS-tail
  false fires. The hardware-AEC follow-up (Tier 2) unlocks dropping
  this.

- **`NO_INTERRUPTION` vs the report's "wire `interrupted` to playback
  flush".** We have the plumbing wired correctly but it's dormant
  because `NO_INTERRUPTION` prevents server-side emission. Not a bug —
  the chosen Stage 1 trade-off in the absence of working AEC. Wakes
  up automatically the moment we drop NO_INTERRUPTION (Tier 2).

## Highest-leverage gating change

Almost everything in Tier 2 unlocks at once when **the dual-output
ALSA topology works** — i.e. when the XVF3800 chip receives a
reference signal of what the speakers are emitting, so its on-chip
AEC can cancel the echo. Until then we're effectively running the
report's "Stage 1" (mute-mic-during-TTS via `NO_INTERRUPTION`).

The previous attempt at the topology used `route → multi → 2x[plug →
dmix]` with heterogeneous inner rates (dongle 48 kHz, XVF 16 kHz) and
failed with `snd_pcm_hw_params_any: EINVAL` — `multi` couldn't
negotiate a common period_size across the rate mismatch. Possible
next approaches to try:

1. Match rates: run both legs at 48 kHz (let the XVF chip's USB-IN
   accept 48 kHz with internal downsampling, if the firmware does).
2. Use `snd-aloop` as an intermediate ALSA loopback so the XVF leg
   gets a separate, fully-independent timing domain.
3. Use a small userspace `tee` daemon (e.g. `alsa_in`/`alsa_out`)
   instead of ALSA's `multi` plugin.
4. Run a second instance of CamillaDSP that captures the dongle
   signal and writes to the XVF USB-IN, providing the reference
   signal that way.

Any of these would unlock real barge-in, drop refractory to 2-3 s,
allow `NO_INTERRUPTION` removal, and enable the HA Voice PE-style
mid-TTS "stop" wake-word path. Highest single architectural unlock
remaining in the system.

## Future UX work (post-AEC)

These are not architectural items so much as post-AEC UX improvements
we want once the underlying audio/listening behavior is solid. Capturing
here so they don't get lost.

### Conversational follow-up window

Currently every interaction requires a fresh "Hey Jarvis" wake word.
Once hardware AEC is working and `NO_INTERRUPTION` can be removed,
we want the natural back-and-forth pattern that polished voice
assistants ship with:

- After Gemini's response finishes, keep the mic open for ~15-20 s
  (no wake required) listening for a follow-up.
- If user speaks within the window: continue the same conversation
  context (acquire a new turn within the existing connection).
- If silence: timeout, restore music to full, re-arm wake word.

This is sometimes called "continued conversation" (HA / OVOS) or
just "follow-up listening." Two pieces required to make it feel
right:

1. **AEC must work** — otherwise the daemon hears its own TTS and
   the follow-up window self-triggers on the model's last sentence.
2. **A clear audio cue** — a soft tone or volume signature on the
   transition from "Gemini speaking" → "listening for follow-up" →
   "back to music." Without it, users won't know the window is
   open.

The current `JASPER_LIVE_CONTEXT_RESET_SEC=60` value (lowered from
300) is a UX compromise for the no-AEC era: rapid-fire single-shot
queries reset cleanly between groups but quick-enough follow-ups
still share context. When the follow-up window lands, this knob
becomes less critical — context resets when the follow-up window
times out (re-armed wake), not on a clock.

### Un-duck on `turn_complete`, not at turn end

Right now music stays ducked through the entire turn — including
the `POST_RESPONSE_IDLE_TIMEOUT_SEC = 1.5 s` tail that lets the
last TTS chunks finish playing. To the user that 1.5 s of "ducked
silence" after Gemini finishes feels like the assistant is still
listening (it isn't — `activity_end` was sent at user-silence
detection). UX gap: "still ducked = still listening" is broken.

Fix: move `await self._ducker.restore()` to fire the moment we
observe `server_turn_complete` (with a small 200-300 ms tail so
the last TTS chunks aren't fighting music return), not at full
turn-end. Music returns ~1.5 s sooner, gives a clear audio cue
that the interaction is over.

Pairs naturally with the follow-up window above: un-duck at
turn_complete → music returns → user knows interaction is done →
wake again to start fresh. Without follow-up: clean cycle. With
follow-up: temporary duck-down on next user-speech detection.

### Post-`activity_end` audio cue

Even before the follow-up window lands: a soft tone (50-100 ms,
low-volume) when `activity_end` fires would give immediate
feedback that "we heard you, you can stop talking now." Today
the audio cue is the duck-restore at end-of-turn, which lags by
the entire model-response duration. Quicker feedback closes the
mental loop for the user.
