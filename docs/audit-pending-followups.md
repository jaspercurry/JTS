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

## Tier 2 — gated on hardware AEC working (Phase A LANDED, awaiting hardware verification)

These are the highest-value improvements that became possible once the
AEC reference signal flowed to the XVF3800. Phase A landed the dual-
output ALSA topology as a `type plug → type multi → [dongle, snd-aloop
sub1]` fan-out with both legs at 48 kHz, plus a second CamillaDSP
instance (`jasper-aec-bridge.service`) that captures from
`hw:Loopback,1,sub1`, resamples 48 → 16 kHz with AsyncSinc, and writes
to the XVF3800's USB-IN endpoint. The previous failure
(`snd_pcm_hw_params_any: EINVAL`) was misdiagnosed as a rate-mismatch
problem — the actual cause was `multi`'s linked `period_size` constraint
across slaves with different default period sizes. Pinning identical
`period_size 1024` / `buffer_size 4096` on both leaves resolved it.

`jasper-aec-tune` calibrates `AUDIO_MGR_SYS_DELAY` via white-noise
cross-correlation; `jasper-aec-init.service` re-applies the persisted
delay at every boot (firmware 2.0.6's `SAVE_CONFIGURATION` has a brick
hazard per respeaker repo issue #8, so we don't persist on-chip).

**Hardware verification still pending.** The Tier 2 follow-ups below
unlock once `jasper-aec-tune` produces a sane delay value AND
`AEC_AECCONVERGED` reads `1` while music plays AND a recorded mic
sample shows ≥25 dB of music attenuation. Until then, treat them as
"ready to drop, awaiting confirmation."

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

## Phase-2 transport / playback follow-ons

The May 2026 voice-music-control phase landed:

- **Volume:** percent-based `set_volume` / `adjust_volume` / `mute` /
  `unmute` against CamillaDSP's main fader (works regardless of which
  renderer is active).
- **Transport:** source-aware `next_track` / `previous_track` /
  `pause` / `resume` / `get_now_playing`. AirPlay routes via shairport-
  sync's MPRIS interface (`org.mpris.MediaPlayer2.ShairportSync` on
  the system bus → DACP → sender), Spotify Connect routes via spotipy
  against the active device, no-active-source returns a clean
  "nothing is playing" error.

These follow-ons are explicitly in scope for the next phase:

### `play_song` / `play_artist` / `play_album` / `play_playlist`

Spotify Web API search + start_playback, exposed as separate tools so
the LLM doesn't have to choose `kind=` from a free-form query. The
existing `spotify_play(query, kind=…)` plumbing handles the API call
shape; the new tools are thin aliases that pin `kind` and clean up the
system-instruction few-shots.

The interesting bit is **device targeting in the AirPlay-carrying-Spotify
case** (the canonical iPhone use case from the original handoff):

- User has iPhone Spotify casting via AirPlay to the Pi.
- The renderer reports `aplactive=1`. To the Pi, AirPlay is the source.
- User says "play Kanye West."
- Correct behaviour: target the **iPhone's** Spotify Connect device,
  not the Pi's librespot. The iPhone's Spotify app receives the
  start_playback, changes track, AirPlay stream content updates
  seamlessly — no source switch on the Pi side.

`spotify_routing.resolve_target` already handles this — it uses
`_match_track` (title-only fuzzy match between AirPlay metadata and
Spotify currently-playing) to detect "AirPlay is carrying Spotify."
The new tools just need to call into `resolve_target` like
`spotify_play` already does. Lean on the existing logic; do not
reimplement it.

The deferred-non-goal: **starting** a Spotify-via-iPhone-via-AirPlay
session from cold (nothing playing on the iPhone, nothing AirPlay'd,
"play Kanye"). The phone has to be sending AirPlay for that case to
work, and the user has explicitly said this is out of scope.

### Bluetooth transport (AVRCP)

When `btactive=1`, transport routes to bluez's MediaPlayer1 interface
via DBus. Object path is dynamic — something like
`/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/player0`. Discovery: enumerate
under `/org/bluez/hci0` for objects implementing
`org.bluez.MediaPlayer1`. Methods are the standard MPRIS-shaped set
(`Play`, `Pause`, `Next`, `Previous`). Easy follow-on once the
A2DP path is exercised; see `bluez/doc/mediaapi.txt` for the contract.

### `get_now_playing` is now source-aware

It reads MPRIS `Metadata` when AirPlay is the source and
`sp.current_playback` for Spotify Connect, with empty-but-tagged
returns when no source is active. Follow-on: also expose
`is_playing` / playback position so "how much of this song is left?"
can be answered without re-querying.

## Highest-leverage gating change — RESOLVED in Phase A

The dual-output ALSA topology now works (`deploy/alsa/asoundrc.jasper`).
What landed: combined approaches 1 + 2 + 4 from the original list.
Match rates at 48 kHz (approach 1) at the `multi` boundary so
period_size negotiation succeeds. Use `snd-aloop` sub1 (approach 2)
as the intermediate timing domain for the AEC leg. Use a second
CamillaDSP instance (approach 4) as the rate-conversion bridge that
slaves the loopback's virtual clock to the XVF's USB clock via
`enable_rate_adjust: true` + AsyncSinc.

The XVF3800 USB firmware does NOT accept 48 kHz on its UAC2
playback endpoint (verified via `cat /proc/asound/Array/stream0` —
locked at 16 kHz S16_LE 2ch FL+FR, single Altset, SYNC iso, no
feedback EP). So approach 1 alone wasn't sufficient — needed the
bridge.

The remaining work that unlocks Tier 2 is hardware verification of
AEC convergence + measurable music attenuation. See "Tier 2 — gated
on hardware AEC working" above for the post-verification action items.

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

(Historical note: this section used to discuss `JASPER_LIVE_CONTEXT_RESET_SEC`
as a UX knob trading rapid-fire reset against follow-up coherence.
That env var was removed 2026-05-09 — OpenAI's `truncation: "auto"`
and Gemini's session-resumption handle handle context management
natively, and the wake-loop's audio buffer makes any natural
reconnect lossless. The follow-up window itself doesn't depend on
context reset.)

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

### Earcons for tool-call confirmation (replace verbal "Done.")

Today, after a volume / transport tool call we have Gemini say
"Done." The verbal ack works but adds 3–5 s of model-response
latency before the duck restores, and the spoken word feels
heavy for what is essentially a button-press confirmation.

Replace it with a local earcon — a short pre-synthesised tone
played by the daemon the moment the tool returns:

- **Ascending sweep** (e.g. 600→900 Hz, 200 ms) on success.
- **Descending sweep** (900→600 Hz, 200 ms) on failure / no-op
  (e.g. transport command when nothing is playing).

Implementation sketch: synthesise the two waveforms once at
daemon startup (`numpy.sin` over the right sample-count, fade
in/out 5 ms to avoid clicks), keep them in memory as `bytes`,
play through the existing `TtsPlayout` instance the moment the
tool's `fn done` log line fires. Suppress Gemini's verbal reply
for tool-acknowledged commands by removing "reply with 'Done.'"
from the system instruction and instead instructing it to stay
silent after volume / transport tools — the earcon IS the
acknowledgment.

Caveat: when we tried the "(silent)" pattern in the system
instruction, Gemini sometimes produced output_tokens > 0 with
zero audio chunks AND no `turn_complete`, which left the duck
held until the 10 s idle timeout. Two ways to avoid that:

1. Have Gemini still emit a tiny audio token (e.g. a single
   "mm." that we discard locally before playback) so the model
   still produces audio + turn_complete.
2. Detect the no-audio-after-tool-call pattern and force-close
   the turn after a short timeout (e.g. 1.5 s) once the tool
   has returned and `turn_complete` hasn't fired — bypassing
   the 10 s pre-response idle window.

Option 2 is the cleaner architectural fix and pairs well with
the un-duck-on-`turn_complete` UX item above. ~30–40 lines of
code total: the synth, the trigger, the timeout adjustment, and
the system-instruction tweak.
