# Audio paths and software volume knobs

Two paths to the final output owner, processed differently. Knowing
which is which matters when you're testing volume-controlled output and
when you're trying to understand assistant loudness matching in
`jasper-outputd`.

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
bypass at that same final mix boundary (see "Assistant loudness
matching" below).

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

`jasper-outputd` normally reads the content capture lane directly. For
lab validation, `JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match` inserts an
outputd-owned bounded ring plus ppm-clamped rate matcher at this final
content/DAC clock boundary while leaving the DAC write loop as timing
owner. The default bridge target is 4096 frames (~85 ms at 48 kHz);
AirPlay latency rendering accounts for that target only when the bridge
is explicitly enabled.

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
writer on current main. CamillaDSP is upstream of outputd
only on the music leg. The legacy `pcm.jasper_out` dmix remains in
`/etc/asound.conf` as the pre-outputd rollback path, not as the active
convergence point here.

## Volume knobs and which path each affects

| Knob | Where it lives | Music | TTS / outputd |
|------|----------------|-------|----------------------------|
| CamillaDSP `main_volume` (the ducker) | DSP, websocket port 1234 | yes | no |
| Source slider (iPhone, Spotify Connect, BT phone) | Renderer-side, before Loopback | yes | no |
| Source amplitude (PCM data) | The WAV / TTS PCM buffer | yes | yes |
| Assistant loudness matcher (auto) | jasper-outputd + provider profiles | n/a | yes — auto-tracks content |
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

## Assistant Loudness Matching

Since assistant audio bypasses CamillaDSP, a fixed provider PCM level
would ignore how the user currently listens to music. Current main keeps
that compensation inside `jasper-outputd`, the final output owner:

1. `jasper-outputd` continuously measures content/music with a bounded
   K-weighted loudness window.
2. At wake turn start, `jasper-voice` sends `PREPARE_ASSISTANT` with
   the active provider/model/voice and a conservative silence target
   derived from `listening_level`.
3. `jasper-outputd` snapshots the current content loudness before
   ducking, then ignores content-meter updates while the voice turn or
   correction measurement window is active.
4. For each assistant/cue segment, `OutputdTtsPlayout` sends un-gained
   48 kHz stereo PCM plus optional source-loudness profile metadata.
5. `jasper-outputd` chooses final gain at the mix boundary:
   `target_lufs = content_baseline_lufs + assistant_offset_lu`.
   The default offset is `+1.5 LU`.
6. Hearing safety is peak-aware and enforced in outputd: the requested
   loudness gain is capped so the profiled source peak stays below the
   configured assistant peak ceiling (default `-3 dBFS`), then clamped
   through the global TTS gain floor/ceiling.

Python owns only provider source profiles:

- The persisted profile store is
  `/var/lib/jasper/assistant_loudness_profiles.json`, overridable with
  `JASPER_ASSISTANT_LOUDNESS_PROFILE_PATH`.
- The `/voice/` wizard's **Save and Test** button synthesizes
  `"This is me talking normally."` with the active provider's TTS API,
  measures it silently, and stores the profile before restarting
  `jasper-voice`. The handler caps this explicit test at one provider
  attempt. Daemon-start seeding remains opt-in
  (`JASPER_ASSISTANT_LOUDNESS_AUTO_SEED=1`) so ordinary restarts do not
  spend provider calls implicitly.
- Live assistant PCM is measured passively after real replies and
  merged back into the same provider/model/voice profile. Cues and
  chirps never train the profile.
- Profiles are advisory. If a profile is missing or malformed, outputd
  uses conservative built-in fallback source loudness/peak values and
  still clamps the final gain.

Operator retunes live in `/var/lib/jasper/outputd.env`:

```
JASPER_OUTPUTD_ASSISTANT_OFFSET_LU=1.5
JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS=-3.0
JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_LUFS=-24.0
JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS=-6.0
JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS=-41.0
JASPER_OUTPUTD_CONTENT_SILENCE_LUFS=-60.0
```

When a cue/assistant segment arrives without a prepared wake-turn
context and without measurable content, outputd uses
`JASPER_OUTPUTD_ASSISTANT_DEFAULT_SILENCE_TARGET_LUFS` as the baseline
instead of a fixed fallback gain. This keeps no-context cues on the same
profile/peak-cap path as live assistant speech.

### Debugging Assistant Gain

Every outputd assistant gain decision emits one structured journal line:

```
event=outputd.assistant_loudness kind=assistant provider=openai
  model=gpt-realtime-2 voice=verse calibrated=true confidence=0.82
  baseline_lufs=-29.4 target_lufs=-27.9 source_lufs=-18.2
  source_peak_dbfs=-2.5 requested_gain_db=-9.7 peak_cap_gain_db=-0.5
  final_gain_db=-9.7 reason=target
```

`jasper-outputd` also exposes the latest content and assistant decision
through `/run/jasper-outputd/control.sock` (`STATUS\n`) under
`assistant_loudness`. `jasper-doctor` warns if this telemetry is
missing or malformed. Use those two surfaces first when debugging a
provider loudness report; they show whether the system used a calibrated
profile, what content baseline it matched, and which clamp won.

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
playback exercises only the pre-outputd rollback dmix and bypasses both
CamillaDSP and outputd. `main_volume` does nothing to the TTS path.

On Apple-dongle installs, the dongle `Headphone` control is pinned at
100% by `jasper-dac-init`, watched by `jasper-headphone-monitor`, and
checked by `jasper-doctor`. Those services are enabled only when
`jasper-audio-hardware-reconcile` recognizes the selected final-output
DAC as the Apple USB-C dongle; DAC8x and unknown-output states disable
the Apple-specific units. The reconciler runs at install/boot and from
udev `controlC*` add/remove/change events, so USB DAC changes converge
without a deploy-only scan. The helper scripts remain runtime-safe for
manual/operator starts. `outputd_dac` still points at the detected
final-output card. For explicit DAC8x lab wiring, operators may set
`JASPER_OUTPUT_DAC_ROUTE=mono:N` or `stereo:L,R` in
`/etc/jasper/jasper.env`; the route is applied only for recognized
DAC8x hardware and uses 1-indexed physical output numbers. It takes
effect when deploy, boot/udev reconcile, or a manual
`jasper-audio-hardware-reconcile` run re-renders `/etc/asound.conf`.
This is a small final-output alias route for single-amp/commissioning
cases, not an active-speaker crossover map. Software never touches
downstream amp gain. The amp gain is a physical knob set at install
time.

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
- Chip-AEC exception: production chip-AEC mode and the wake-corpus
  chip-AEC comparison profile can ask outputd to publish its final
  speaker buffer as both an XVF USB-IN reference and an `outputd_udp`
  reference tap for `jasper-aec-bridge`. The UDP tap stays at outputd's
  48 kHz graph rate; the XVF USB-IN side output is downsampled to the
  chip's 16 kHz playback contract. The production path is opt-in via
  `JASPER_WAKE_LEG_CHIP_AEC=1` / `JASPER_AEC_CHIP_AEC_ENABLED=1`;
  the recorder owns the same overlay during corpus chip-AEC comparison
  sessions and removes its test env when corpus mode exits. Default
  software AEC still uses the `pcm.jasper_capture` reference above.

## Related

- [HANDOFF-barge-in.md](HANDOFF-barge-in.md) — open architectural
  decision for upgrading barge-in beyond VAD-only filtering.
- [HANDOFF-volume.md](HANDOFF-volume.md) — VolumeCoordinator and
  source-aware dispatch.
- [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md) —
  voice tool transport routing.
- [HANDOFF-aec.md](HANDOFF-aec.md) — why the AEC bridge taps pre-DSP.

---

Last verified: 2026-06-02 (DAC8x output route knob added; assistant loudness matching, STATUS telemetry, and outputd topology rechecked)
