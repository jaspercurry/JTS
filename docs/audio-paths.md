# Audio paths and software volume knobs

Two paths to the final output owner, processed differently. Knowing
which is which matters when you're testing volume-controlled output and
when you're trying to understand the loudness-tracking compensation in
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
bursty writers). So we route TTS around CamillaDSP into
`jasper-outputd`, the final output owner, and compensate for the DSP
bypass in software (see "TtsVolumeTracker" below).

## The two paths

```
MUSIC chain (gets CamillaDSP processing)
    renderers / correction sweeps → private fan-in lanes
              → hw:Loopback,0,0..4 → snd-aloop → hw:Loopback,1,0..4
              → jasper-fanin → hw:Loopback,0,7
              → snd-aloop → pcm.jasper_capture (dsnoop on hw:Loopback,1,7)
              → jasper-camilla (main_volume + filters)
              → outputd_content_playback
              → snd-aloop → outputd_content_capture
              → jasper-outputd → outputd_dac → amp → speakers

TTS / TEST-TONE chain (BYPASSES CamillaDSP)
    jasper-voice OutputdTtsPlayout → /run/jasper-outputd/tts.sock
                                   → jasper-outputd → outputd_dac
                                   → amp → speakers
```

Each renderer has its own snd-aloop lane, and room-correction/test
playback has a dedicated `correction_substream` lane. `jasper-fanin`
sums those lanes into substream 7, which CamillaDSP and the AEC bridge
share via `pcm.jasper_capture` / `pcm.jasper_ref`. This replaced the
short-lived renderer-side dmix (`jasper_renderer_mix`) after AirPlay
testing showed dmix's per-write timing could drop WiFi-bursty RTP
packets.

## Manual source selection

The landing page's Source selector chooses which enabled renderer lane
the speaker passes; it does not turn renderers on or off. The on/off
surface remains `/sources/`.

Control path:

```
deploy/index.html
  → jasper-control /source/state + /source/select
  → jasper-mux UDS /run/jasper-mux/control.sock
  → jasper-fanin UDS /run/jasper-fanin/control.sock
  → selected input gate in the fan-in audio loop
```

Ownership is deliberately split:

- `jasper-mux` owns policy and the source-handoff transaction. Auto
  mode is latest-source-wins; manual mode is the user-selected source.
  Source metadata lives in `jasper/music_sources.py`, including the
  fan-in lane label and whether `listening_level` is carried by
  CamillaDSP or by a push-to-source volume API.
- Before mux exposes a new lane, it asks
  `VolumeCoordinator.prepare_source_handoff(...)` to make the target
  volume carrier safe. Only then does it send `SELECT <label>` to
  fan-in. After the gate moves,
  `VolumeCoordinator.finalize_source_handoff(...)` converges the
  steady-state carrier. This is the guard against loud source-switch
  transients such as Spotify (Camilla 0 dB) → AirPlay
  (Camilla-as-master). Mux logs one `event=source.handoff_start`
  and one terminal `event=source.handoff` per transition, both with a
  stable `id` also exposed in `/source/state.last_handoff`, so a
  source switch can be correlated across journal, dashboard, and
  control API without phase-by-phase log spam.
- `jasper-fanin` owns only the cheap audio gate: `AUTO` sums active
  lanes; `SELECT <label>` passes one renderer lane; `NONE` passes no
  renderer lane. The correction/test lane is always mixed so
  diagnostics and room correction still work. Fan-in starts in `NONE`,
  and mux keeps it there whenever no source has a guarded winner.
- `jasper-control` is the HTTP proxy for the web UI. It also merges
  `/sources/` availability into `/source/state` so unavailable/off
  renderers can be disabled in the landing-page selector.

## Adding a new music source

This is the canonical checklist for adding another source that should
play through the speakers as **music/content**. It is not for TTS,
system cues, wake sounds, or other assistant-owned audio; those stay on
the TTS/test-tone path unless the design explicitly wants CamillaDSP
processing.

For the planned provider/source capability boundary, read
[`HANDOFF-source-capabilities.md`](HANDOFF-source-capabilities.md)
alongside this checklist. This file owns the physical audio path and
required integration points; the source-capabilities doc owns the
future extraction plan for volume, transport, metadata, and health
adapters.

Keep the change boring. A new source should look like the existing
AirPlay, Spotify, Bluetooth, or USB sink lanes, not introduce a second
mixer, a second output device, or a new volume model.

1. **Give it one private fan-in lane.** Add exactly one PCM alias in
   `deploy/alsa/asoundrc.jasper`, pinned to 48 kHz stereo S16_LE via
   `plug`. Current allocation: `0` Spotify, `1` AirPlay, `2`
   Bluetooth, `3` USB sink, `4` correction/test, `5` debug/monitor
   reserve, `6` outputd post-DSP content, `7` fan-in summed output. Do
   not put a source on substream `6` or `7`. If you need another
   production source lane, stop and redesign the topology rather than
   overloading snd-aloop.
2. **Teach `jasper-fanin` about the lane.** Extend
   `JASPER_FANIN_INPUT_PCMS` and `JASPER_FANIN_INPUT_RENDERERS` in
   `deploy/systemd/jasper-fanin.service`. The lists are pipe-delimited
   because ALSA `hw:` names contain commas. A configured input is part
   of the production graph; if it cannot be opened, fan-in should fail
   loudly instead of silently dropping the source. Keep the renderer
   label stable: mux uses that label when it asks fan-in to pass one
   selected source lane.
3. **Wire the source daemon to the alias.** Its systemd unit should
   write to the alias, not to `jasper_capture`, `jasper_out`,
   `outputd_content_*`, or raw `hw:Loopback,*` names. Renderer units
   should order after
   `jasper-fanin.service` and use the same hardening/resource patterns
   as the existing sources. If the source is optional, default it off
   and make the disabled state cost zero resident RAM.
4. **Expose fail-soft playing state.** Add one probe in
   `jasper/source_state.py`, surface it through
   `RendererClient.active_renderers()`, and keep transport failures as
   `False` plus debug logging. This state feeds mux, volume, transport,
   dashboards, and voice tools, so avoid duplicating probes in each
   caller.
5. **Declare the source once.** Add one `Source` enum member and one
   `MusicSourceSpec` in `jasper/music_sources.py`: public ID, fan-in
   label, renderer active key, `/sources/` wizard key, display name,
   and `volume_mode`. `VolumeMode.PUSH` means the source's own volume
   API carries `listening_level` and CamillaDSP returns to 0 dB.
   `VolumeMode.CAMILLA_MASTER` means CamillaDSP carries
   `listening_level`.
6. **Define preemption.** Add the source-specific stop/pause/silence
   path to `jasper/mux.py`. Prefer a real renderer-owned API: AirPlay
   uses shairport-sync MPRIS `Stop` when it loses the lane, Spotify uses
   Web API pause with a restart fallback, and USB sink uses its local
   silence endpoint. If the source cannot be controlled from the Pi,
   document the intentional fallback ("may briefly mix") and expose an
   operator escape hatch only when the failure mode justifies one.
7. **Wire manual source selection.** The mux/control allow-lists derive
   from `jasper/music_sources.py`; add the landing-page button in
   `deploy/index.html` and keep `/sources/` as the on/off surface.
   `/source/select` only picks the lane the speaker should currently
   pass.
8. **Teach the coordinator source-specific volume I/O.** The handoff
   safety policy comes from `volume_mode`, but push-mode sources still
   need one `_set_<source>` dispatcher. Add inbound observation only if
   the source has a reliable user-facing volume surface.
9. **Decide transport/metadata truthfully.** If voice `pause`, `next`,
   `previous`, or `now playing` can control the source, wire
   `jasper/tools/transport.py` and document the backend in
   `HANDOFF-voice-music-control.md`. If not, return a concrete "not
   supported for this source" response.
10. **Add operator surfaces and observability.** Update `/sources/` if
   the source can be enabled/disabled, `/state` if it has useful live
   state, `jasper-doctor` for topology drift and runtime health, and
   `jts-audio.slice` / no-swap checks for any resident audio-path
   daemon.
11. **Protect measurements and tests.** Add the source to the
   correction `measurement_window()` pause list if it can emit during a
   sweep. Add tests for asound wiring, fan-in config, source-state
   fail-soft behavior, mux preemption, source-handoff safety, volume
   dispatch, and any source wizard toggles.
12. **Update docs in one place, then link.** This section covers the
    cross-cutting checklist. Source-specific quirks belong in a
    focused HANDOFF only when they are non-obvious, as USB sink does in
    `HANDOFF-usbsink.md`. README's documentation map should link the
    current operational truth; historical design notes should be marked
    historical.

Both legs converge inside `jasper-outputd`, which owns the direct DAC
writer on the outputd cutover branch. CamillaDSP is upstream of outputd
only on the music leg. The legacy `pcm.jasper_out` dmix remains in
`/etc/asound.conf` as the main-branch rollback path, not as the active
convergence point here.

## Volume knobs and which path each affects

| Knob | Where it lives | Music | TTS / outputd |
|------|----------------|-------|----------------------------|
| CamillaDSP `main_volume` (the ducker) | DSP, websocket port 1234 | yes | no |
| Source slider (iPhone, Spotify Connect, BT phone) | Renderer-side, before Loopback | yes | no |
| Source amplitude (PCM data) | The WAV / TTS PCM buffer | yes | yes |
| `JASPER_TTS_GAIN_DB` | OutputdTtsPlayout gain metadata | n/a | yes |
| `TtsVolumeTracker` (auto) | OutputdTtsPlayout gain metadata | n/a | yes — auto-tracks music |
| Apple dongle Headphone | Hardware mixer | (pinned 100%) | (pinned 100%) |
| TPA3255 amp | Physical knob | yes | yes |

Two notes:
- `master_gain` is a CamillaDSP mixer named in the base Camilla configs
  but currently configured as identity. The Ducker operates on
  `main_volume`, not `master_gain`. Old comments/docs that called
  master_gain "the ducking knob" are wrong.
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
CamillaDSP's `playback_rms` (post-DSP content level, after every
upstream attenuator) and scales TTS to sit a configurable
headroom above it. A "loudness anchor" persists across boots so a
quiet bedroom from yesterday is still quiet today until someone
changes it.

This compensation is load-bearing — it's what makes the CamillaDSP
bypass invisible to the user. Don't remove it without first removing
the bypass.

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
- `final_db` — what the TTS path sends to outputd after `MAX_TTS_GAIN_DB`
- `max_cap_db` — hearing-safety cap (always `-6 dB`)

Fires only on actual changes to `final_db` (no log spam). The existing
`tts gain set: X dB` short line still fires alongside it for grep-
friendly summaries. Together, the two lines let you reconstruct any
TTS gain choice from logs alone — no need to correlate across separate
`event=duck`, `volume persistence`, and `tts gain set` lines.

## End-of-turn drain — when is the speaker actually silent?

The TTS write call returns when bytes are accepted by the current
transport, **not** when they reach the DAC. On current main, the
transport is a local Unix socket into `jasper-outputd`; the legacy
rollback path used PortAudio. Either way, there is still transport
queue, outputd/DAC or OS-audio tail, and DAC flush ahead of the bytes
at that point. A naive
end-of-turn timer that fires "shortly after the last write" can land
mid-tail, clipping the last word — observed in production
(PR #311, 2026-05-25) when OpenAI Realtime burst-streamed 10 chunks
in 730 ms ahead of a 4 s playout.

`TtsPlayout` owns the end-of-turn drain semantic. Outputd extends the
same boundary with `write_segment()`/`end_segment()` metadata for the
playout ledger, but the voice daemon still waits through the stable
methods below:

- `expected_drain_at()` — monotonic deadline when the last-queued
  sample's tail will have cleared the OS audio stack. Backed by a
  single `_ring_end_monotonic` float that advances on each `write()`
  (anchors fresh on now() if the speaker was idle; appends during
  back-pressure). Reset by `flush()` since barge-in's `abort()`
  discards the ring. Returns `0.0` when nothing is queued — naturally
  reads as "already drained" against `time.monotonic()`.
- `wait_drained()` — single `asyncio.sleep` to the deadline. No
  polling because the deadline is known up-front.

For interruption, `OutputdTtsPlayout.flush()` uses outputd's
`FLUSH_SYNC` command and returns the daemon's compact playout
acknowledgement (`audio_played_ms`, flushed frames, provider item id).
That acknowledgement is for provider truncation/cancel logic; normal
end-of-turn still uses `wait_drained()`.

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

**Test the music chain** (volume-controlled): `aplay -D correction_substream file.wav`.
Goes through CamillaDSP, so `main_volume` applies.

**Test the TTS chain**: use `jasper-voice`/cue playback or a small
outputd client, not `aplay -D plug:jasper_out`. Direct `jasper_out`
playback exercises only the main-branch rollback dmix and bypasses both
CamillaDSP and outputd. `main_volume` does nothing to the TTS path.

The Apple dongle Headphone is pinned at 100% by `jasper-dac-init`,
watched by `jasper-headphone-monitor`, checked by `jasper-doctor`.
Software never touches it. The amp gain is a physical knob set at
install time.

## AEC bridge implications

The bridge taps `pcm.jasper_capture`, a dsnoop on the summed fan-in
output `hw:Loopback,1,7` — the music chain reference, BEFORE
CamillaDSP processing. So:

- TTS bleed through the mic is not yet in the AEC reference; the bridge
  cancels music bleed only. This is **intentional** for the current
  AEC bridge, even though outputd now owns the final output loop. Robust
  barge-in should move the reference consumer to outputd's eventual
  speaker reference fanout — see
  [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md).
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

Last verified: 2026-05-28 (source handoff guard, future-source checklist, source-capabilities plan link, outputd cutover topology, and TTS drain/flush boundary rechecked)
