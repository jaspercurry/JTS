# Audio paths and software volume knobs

Two paths to the dongle, processed differently. Knowing which is which
matters when you're testing volume-controlled output and when you're
trying to understand the loudness-tracking compensation in
`jasper-voice`.

## How we got here

A smart speaker plays music AND voice prompts. They want different
processing: music tolerates latency and benefits from EQ / room
correction; voice prompts need to be heard above music and shouldn't
be re-processed by an EQ tuned for music. This is the standard
"two-bus" pattern (HA Voice PE, OVOS, Alexa AVS Dialog/Content,
broadcast PA hardware).

The Linux-on-a-single-Pi version of this pattern has one constraint:
**CamillaDSP supports only one ALSA capture device per process.**
Combining music and TTS into one DSP pipeline would require either
pre-mixing them upstream (which would mean ducking music ducks TTS
too — wrong) or the fragile ALSA `multi` plugin (xrun storms with
bursty writers). So we route TTS around CamillaDSP into the dongle's
dmix instead, and compensate for the bypass in software (see
"TtsVolumeTracker" below).

## The two paths

```
MUSIC chain (gets CamillaDSP processing)
    renderers → pcm.jasper_renderer_in (plug)
              → pcm.jasper_renderer_mix (dmix; multi-writer-safe)
              → hw:Loopback,0,0 → snd-aloop → plughw:Loopback,1,0
              → jasper-camilla (main_volume + filters)
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers

TTS / TEST-TONE chain (BYPASSES CamillaDSP)
    jasper-voice TtsPlayout → pcm.jasper_out (dmix on dongle)
                            → dongle → amp → speakers
```

The renderer-side dmix (`jasper_renderer_mix`, fronted by
`jasper_renderer_in`) was added 2026-05-22 so the three renderers
(librespot, shairport-sync, bluealsa-aplay) can hold the device
simultaneously. Without it, snd-aloop's `hw:Loopback,0,0` is
single-writer; any second renderer trying to open it during a phantom
or genuine session of another renderer returned ALSA -EBUSY, which
crashed librespot in a respawn loop and made the user-reported
"Spotify Connect handover from AirPlay" fail.

Both legs converge at `pcm.jasper_out`, a dmix on the dongle. dmix
sums the two writers' streams sample-wise and sends one stream to the
DAC. CamillaDSP is upstream of dmix only on the music leg.

## Volume knobs and which path each affects

| Knob | Where it lives | Music | TTS / `aplay -D jasper_out` |
|------|----------------|-------|----------------------------|
| CamillaDSP `main_volume` (the ducker) | DSP, websocket port 1234 | yes | no |
| Source slider (iPhone, Spotify Connect, BT phone) | Renderer-side, before Loopback | yes | no |
| Source amplitude (PCM data) | The WAV / sounddevice buffer | yes | yes |
| `JASPER_TTS_GAIN_DB` | TtsPlayout source-side | n/a | yes |
| `TtsVolumeTracker` (auto) | TtsPlayout source-side | n/a | yes — auto-tracks music |
| Apple dongle Headphone | Hardware mixer | (pinned 100%) | (pinned 100%) |
| TPA3255 amp | Physical knob | yes | yes |

Two notes:
- `master_gain` is a CamillaDSP mixer named in `v1.yml` but currently
  configured as identity. The Ducker operates on `main_volume`, not
  `master_gain`. Old comments/docs that called master_gain "the
  ducking knob" are wrong.
- `listening_level` is the canonical user-facing volume in the
  VolumeCoordinator (see [HANDOFF-volume.md](HANDOFF-volume.md)). It
  maps to `main_volume` for IDLE and AirPlay; for Spotify and BT,
  `main_volume` stays pinned at 0 dB and the source slider carries
  `listening_level`.

## Why TTS still tracks user volume changes

Since TTS bypasses CamillaDSP, naively it would always play at fixed
amplitude regardless of how the user set volume. To preserve the
property "however the user adjusted volume — iPhone slider, AirPlay,
Spotify, the dial, the external amp — TTS matches the music level the
user is actually hearing," `TtsVolumeTracker` in
[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) measures
CamillaDSP's `playback_rms` (the actual signal hitting the DAC, after
every upstream attenuator) and scales TTS to sit a configurable
headroom above it. A "loudness anchor" persists across boots so a
quiet bedroom from yesterday is still quiet today until someone
changes it.

This compensation is load-bearing — it's what makes the bypass invisible
to the user. Don't remove it without first removing the bypass.

### Ceiling policy is branch-specific (read before changing the formula)

The tracker's first version (May 2026) treated `main_volume + offset_db`
as an **absolute** ceiling on TTS gain in every branch — the mental
model was "main_volume controls max possible TTS loudness, the
tracker can only push DOWN from there." That assumption broke once
source-side sliders (iPhone, Spotify, BT) and external amplifiers
became the dominant carriers of user loudness intent. At
`listening_level` below ~90% on loud-source music (e.g. AirPlay at
70% → `main_volume = -15 dB`), the ceiling actively defeated the
tracker, leaving TTS several dB *quieter* than music instead of the
+6 dB above music the headroom formula intended. PR #294
(2026-05-24) lifted the ceiling off the music-playing branch only:

- **Music actively playing** (`windowed_rms > silence_threshold`):
  no `main_volume + offset_db` ceiling. The measured signal IS the
  answer. Hearing-safety lives in `TtsPlayout.MAX_TTS_GAIN_DB`
  (-6 dB).
- **Silence with a valid anchor**: ceiling APPLIES. Anchor can be
  stale (loud music yesterday, quiet bedroom today at low
  `main_volume`), and `main_volume + offset_db` is the right
  backstop against blasting in that case.
- **No anchor ever recorded** (sentinel < -120, effectively
  first-boot only): target IS the ceiling — `main_volume` is the
  only loudness signal we have.

`JASPER_TTS_GAIN_DB` (the `offset_db`) therefore only affects the
silence branches now and is **deprecated** as of PR #295. Default
`0` = "TTS in silence sits at `main_volume` exactly." Negative values
still work but log a one-shot deprecation warning at startup; positive
values are rejected by config validation. The env var will be removed
once nobody's using it.

This branch-specific policy is the structural invariant the
regression tests in `tests/test_tts_volume_tracker.py` lock in
(`test_music_branch_ignores_master_volume_entirely` in particular).
Re-introducing an unmeasured cap on the music branch will fail it.

### Debugging TTS gain — structured telemetry

Every user-perceptible TTS gain change emits a single structured log
line (PR #295) carrying the full computation context:

```
event=tts_gain.compute branch=music windowed_rms=-26.0 anchor_dbfs=-25.9
  main_volume_db=-15.0 offset_db=0.0 ceiling_db=-15.0 target_db=-7.0
  final_db=-7.0 max_cap_db=-6.0
```

Fields:
- `branch` — which decision path fired (`music` / `anchor` / `no_anchor`)
- `windowed_rms` — what `playback_rms` reported, post-windowing
- `anchor_dbfs` — last-known music level (frozen during silence)
- `main_volume_db` — CamillaDSP's `main_volume` at the moment
- `offset_db` — the deprecated `JASPER_TTS_GAIN_DB` offset
- `ceiling_db` — `main_volume + offset` (applied to silence branches only)
- `target_db` — what the formula computed before any clamping
- `final_db` — what `TtsPlayout` actually applied after `MAX_TTS_GAIN_DB`
- `max_cap_db` — hearing-safety cap (always `-6 dB`)

Fires only on actual changes to `final_db` (no log spam). The existing
`tts gain set: X dB` short line still fires alongside it for grep-
friendly summaries. Together, the two lines let you reconstruct any
TTS gain choice from logs alone — no need to correlate across separate
`event=duck`, `volume persistence`, and `tts gain set` lines.

## End-of-turn drain — when is the speaker actually silent?

`sounddevice.RawOutputStream.write()` returns when the bytes are
accepted into PortAudio's internal ring, **not** when they reach the
DAC. There are still ~chunk_duration of ring + ~60-85 ms of dmix
tail + DAC flush ahead of the bytes at that point. A naive
end-of-turn timer that fires "shortly after the last write" can land
mid-tail, clipping the last word — observed in production
(PR #311, 2026-05-25) when OpenAI Realtime burst-streamed 10 chunks
in 730 ms ahead of a 4 s playout.

`TtsPlayout` owns the drain semantic. Two methods:

- `expected_drain_at()` — monotonic deadline when the last-queued
  sample's tail will have cleared the OS audio stack. Backed by a
  single `_ring_end_monotonic` float that advances on each `write()`
  (anchors fresh on now() if the speaker was idle; appends during
  back-pressure). Reset by `flush()` since barge-in's `abort()`
  discards the ring. Returns `0.0` when nothing is queued — naturally
  reads as "already drained" against `time.monotonic()`.
- `wait_drained()` — single `asyncio.sleep` to the deadline. No
  polling because the deadline is known up-front.

Both end-of-turn paths consult the same primitive:

- `_play_responses` (the consumer) awaits `tts.wait_drained()` after
  its final write — replaces a fixed `TTS_ALSA_DRAIN_SEC` sleep.
- `_idle_watchdog` (the server-said-done path) polls
  `tts.expected_drain_at()` cooperatively — replaces a fixed
  `POST_RESPONSE_IDLE_TIMEOUT_SEC` margin.

Both anchor on the same math, so they converge on identical timing.
Whichever observes "drained" first triggers `_end_turn` via the
bg-task done check; the loser's task is cancelled cleanly.

The dmix + DAC flush tail itself is configurable:
`JASPER_TTS_DRAIN_TAIL_SEC` (default 0.085 s, wired through
`cfg.tts_drain_tail_sec`). Bump on a Pi if you observe truncation;
lower if end-of-turn feels sluggish.

**Observability.** `_end_turn` logs `drain wait X.XXs` in the
canonical `turn ended:` line whenever audio was actually received.
This number is "time from last server activity (response.done or
last audio.delta) to the daemon recognizing the turn was over."
Healthy range on the current hardware: ~50-150 ms. Drift above ~150
ms or provider-asymmetric values are the signal to investigate.

```sh
ssh pi@jts.local 'sudo journalctl -u jasper-voice | grep "drain wait"'
```

**Prior art surveyed** (PR #311) before picking sample-counting:

- **LiveKit Agents** — sample-counted `_pushed_duration` +
  `wait_for_playout()` future. Closest analog; same pattern we use.
- **OpenAI wavtools** (older `openai-realtime-console`) — tracks
  `scheduledEndTime` against `AudioContext.currentTime`. Same idea
  on a different audio API.
- **Pipecat** — trailing-silence-pad + EndFrame propagation. The
  pattern JTS effectively had before this fix; race-prone on tight
  UX, which is what bit us.
- **Wyoming (HA Assist)** — protocol round-trip (server sends
  `AudioStop`, satellite acks `Played` after `aplay` exits).
  Overkill for an in-process TTS player.
- **PortAudio callback-based completion** — `outputBufferDacTime` in
  the stream callback's `time_info` is the most precise signal but
  requires switching from blocking `write()` to a callback model.
  Major threading refactor; out of proportion to the fix.

## Operational notes

**Test the music chain** (volume-controlled): `aplay -D plughw:Loopback,0,0 file.wav`.
Goes through CamillaDSP, so `main_volume` applies.

**Test the TTS chain**: `aplay -D plug:jasper_out file.wav`. Bypasses
CamillaDSP. Source amplitude is the only software attenuator —
`main_volume` does nothing to this path.

The Apple dongle Headphone is pinned at 100% by `jasper-dac-init`,
watched by `jasper-headphone-monitor`, checked by `jasper-doctor`.
Software never touches it. The amp gain is a physical knob set at
install time.

## AEC bridge implications

The bridge taps `pcm.jasper_capture`, a dsnoop on `hw:Loopback,1,0` —
the music chain reference, BEFORE CamillaDSP processing. So:

- TTS bleed through the mic isn't in the AEC reference; the bridge
  cancels music bleed only. This is **intentional** today — the
  in-session Silero VAD gate at threshold 0.15 handles TTS bleed
  without needing AEC to cancel it. If robust barge-in (cleanly
  interrupting the assistant during loud music) becomes a goal,
  the architecture has to change — see
  [HANDOFF-barge-in.md](HANDOFF-barge-in.md) for the option space
  (ALSA convergence sink vs PipeWire migration vs measure-first).
- A 25 dB ducking step is a transient the AEC's adaptive filter has
  to re-converge through. Acceptable today; if it becomes a problem,
  move the dsnoop tap downstream of CamillaDSP.

## Related

- [HANDOFF-barge-in.md](HANDOFF-barge-in.md) — open architectural
  decision for upgrading barge-in beyond VAD-only filtering.
- [HANDOFF-volume.md](HANDOFF-volume.md) — VolumeCoordinator and
  source-aware dispatch.
- [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md) —
  voice tool transport routing.
- [HANDOFF-aec.md](HANDOFF-aec.md) — why the AEC bridge taps pre-DSP.

---

Last verified: 2026-05-25 (PR #311 added "End-of-turn drain" section; the TtsPlayout drain primitive replaced the old fixed-margin watchdog and `_play_responses` sleep)
