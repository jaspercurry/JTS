# Audio paths and software volume knobs

One audio path reaches the final output owner. On solo and active-output
speakers, renderer audio and assistant TTS converge in `jasper-fanin` before
CamillaDSP, then `jasper-outputd` owns the final hardware sink. A passive
bonded member is the deliberate exception: its local TTS enters outputd after
CamillaDSP. Knowing that mix-stage boundary matters when testing
volume-controlled output and assistant loudness matching.

## How we got here

A smart speaker plays music AND voice prompts. They need different
mix policy but the same speaker-protection path: music should be
ducked while speech plays, while TTS/cues still need crossover,
correction, gain ceilings, and active-speaker protection.

The Linux-on-a-single-Pi version of this pattern has one constraint:
**CamillaDSP supports only one ALSA capture device per process.** JTS
therefore pre-mixes upstream in `jasper-fanin`: renderer/program lanes
are ducked there, TTS is mixed after the duck, CamillaDSP receives one
stream for crossover/protection, and `jasper-outputd` writes the final
sink. This avoids ALSA `multi` aggregation in the hot path and gives
single Apple, dual Apple, and DAC8x profiles the same TTS semantics.

## The two paths

```
MUSIC chain (gets CamillaDSP processing)
    renderers / correction sweeps → private fan-in lanes
              → hw:Loopback,0,0..4 → snd-aloop → hw:Loopback,1,0..4
              (a lane ARMED for ring ingress replaces its snd-aloop hop with
               a per-renderer SHM slot ring — /dev/shm/jts-ring/lane-<label>.ring,
               written by the renderer's own jts_ring ioplug and read by fan-in.
               Arming is per box and operator-explicit; the fleet default is
               unarmed, and an unarmed box runs the aloop path above.)
              → jasper-fanin → Ring A (/dev/shm/jts-ring/program.ring)
              → jasper-camilla (jts_ring_capture; main_volume + filters)
              → Ring B (/dev/shm/jts-ring/content.ring), or the ACTIVE ring
                (/dev/shm/jts-ring/active-content.ring) on a roleful box
              → jasper-outputd → outputd_dac → amp → speakers

TTS / CUE chain (CROSSED OVER on every output profile)
    jasper-voice TtsPlayout → /run/jasper-fanin/tts.sock
                                   → jasper-fanin, mixed after program duck
                                   → jasper-camilla crossover/protection
                                   → Ring B, or the ACTIVE ring on a roleful box
                                   → jasper-outputd final sink
                                   → selected DAC(s) → amps → drivers
```

That TTS chain is also the active-output topology. On active speakers,
assistant audio must stay in fan-in upstream of CamillaDSP so it rides
the crossover/protection graph; outputd's post-crossover TTS mixer is
not armed on active endpoints.

Passive/dumb bonded multiroom members are the exception: the grouping
reconciler points voice at `/run/jasper-outputd/tts.sock`, and outputd
mixes that speaker's own assistant audio into its local post-round-trip
content lane so replies do not ride the shared sync buffer. Active
endpoints stay on fan-in, with outputd TTS unarmed.

The SHM slot ring is the only transport between fan-in, CamillaDSP, and
outputd. `jasper-outputd` reads Ring B: CamillaDSP writes the post-DSP
stereo program to `jts_ring_playback`, and outputd consumes
`/dev/shm/jts-ring/content.ring` one DAC-sized slot at a time. A roleful
(active-crossover) box has a ring of its own — the ACTIVE ring,
`jts_ring_active_playback` → `/dev/shm/jts-ring/active-content.ring` —
carrying POST-crossover per-driver channels rather than a full-range stereo
program. The role rides the device NAME because it cannot ride the width:
on a 2-way speaker both rings are 2 channels, so outputd admits the ACTIVE
ring only when the hardware reconciler's
`JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT` marker says so. The snd-aloop
loopback route between those three daemons, and the machinery that migrated
a live box between the two, were retired under
[ADR-0100](adr/0100-one-audio-transport.md).

A topology the ring cannot serve does not degrade onto a second path — it
parks loudly: doctor FAIL, `/state.resilience.transport_park`, and one row
per park on the `/system` page naming the shape and its tracked issue. Owner
ruling 2026-08-27: no banner — a browser learns about a park on the system
screen and nowhere else.
`jasper/control/transport_park.py` is the single classifier all three
surfaces read, so they cannot name different reasons for the same box. The
shapes it names are a passive-stereo composite sink (#2982), an explicit
mono full-range layout (#3117), a bonded member whose `dac_content`
round-trip lane outputd refuses against the ring (#3118), and a roleful box
whose ACTIVE endpoint marker has not converged (a recorded remedy rather
than an open issue). Hard-park refusals that prevent guessing — a
full-range program into a protected driver — are separate and survive:
they are hearing safety, not transport arbitration.

Each renderer has its own snd-aloop lane, and room-correction/test
playback has a dedicated `correction_substream` lane. `jasper-fanin` sums
those lanes and writes Ring A for CamillaDSP and **nothing else**.
Production AEC consumes outputd's post-Camilla speaker monitor.

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

- `jasper-mux` owns policy and the source-handoff transaction. Auto mode is
  source-neutral latest-start-wins: every confirmed inactive→active transition,
  including USB frame flow, becomes the winner. Manual mode persistently pins
  the user-selected source; `/sources/` disables sources entirely. mux records
  process-local confirmed-start order so returning to Auto or losing the winner
  selects the newest source still active. Starts first observed in one snapshot
  use deterministic registry order because their historical order is unknowable.
  Native producer events are wake hints only: `jasper/source_events.py`
  translates librespot inotify and AirPlay/Bluetooth D-Bus signals, while
  fan-in sends USB frame-flow edges over mux's UDS. Every hint and the fixed
  1 Hz lost-alert patrol enter the same reconciler, which re-reads source
  state before applying policy; alert arrival order never chooses the winner.
  The two probes that fork a subprocess (AirPlay over busctl, Bluetooth over
  bluealsa-cli) are re-read on the patrol once per `EVENT_BACKED_PROBE_SEC`
  instead of every tick; an alert naming either source still probes it at once.
  Source metadata lives in `jasper/music_sources.py`, including the
  fan-in lane label and whether `listening_level` is carried by
  CamillaDSP or by a push-to-source volume API. Operational lifecycle
  resources live in `jasper/local_sources/registry.py`: the systemd units
  that run, advertise, park while paired as a follower, restore on unpair,
  and refresh after audio graph changes, plus the explicit source-critical
  subset used for cached readiness health. The registry declares resources;
  the source-lifecycle handoff above owns how desired intent is applied.
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

This file owns the physical audio path and required integration points.

Keep the change boring. A new source should look like the existing
AirPlay, Spotify, Bluetooth, or USB sink lanes, not introduce a second
mixer, a second output device, or a new volume model.

1. **Give it one private fan-in lane.** Add exactly one PCM alias in
   `deploy/alsa/asoundrc.jasper`, pinned to 48 kHz stereo S16_LE via
   `plug`. Current allocation (the pair allocation lives canonically in
   `deploy/modprobe.d/snd-aloop.conf`; `asoundrc.jasper`'s header
   cross-references it, and this list mirrors both): `0` Spotify, `1` AirPlay, `2`
   Bluetooth, `3` USB sink, `4` correction/test, `5`–`7` UNALLOCATED (the
   central hops those pairs used to carry are rings). If you need another
   production source lane, stop and redesign the topology rather than
   overloading snd-aloop.
2. **Teach `jasper-fanin` about the lane.** The canonical lane list is
   the compiled-in default arrays in
   `rust/jasper-fanin/src/config.rs` `Config::from_env` (~line 80):
   `input_pcms` and `input_renderers`, kept positionally aligned. Extend
   both there. The `JASPER_FANIN_INPUT_PCMS` / `JASPER_FANIN_INPUT_RENDERERS`
   env vars are an *optional override* that replaces the compiled defaults
   when set — they are **not** wired into
   `deploy/systemd/jasper-fanin.service` by default, so editing the unit
   file alone does nothing unless you also set them. The lists are
   pipe-delimited because ALSA `hw:` names contain commas. A configured
   input is part of the production graph; if it cannot be opened, fan-in
   fails loudly instead of silently dropping the source. Keep the renderer
   label stable: mux uses that label when it asks fan-in to pass one
   selected source lane.
3. **Wire the source daemon to the alias.** Its systemd unit should
   write to the alias, not to a ring PCM or a raw `hw:Loopback,*` name.
   Renderer units should order after
   `jasper-fanin.service` and use the same hardening/resource patterns
   as the existing sources. If the source is optional, default it off
   and make the disabled state cost zero resident RAM.
4. **Expose fail-soft playing state.** Add one probe in
   `jasper/source_state.py`, surface it through
   `RendererClient.active_renderers()`, and preserve the public bool contract
   (`False` plus debug logging on failure). If mux needs the source for
   arbitration, also expose a tri-state observation where `None` means unknown;
   mux holds an active last-known state for a bounded grace rather than turning
   one failed read into a stop/start flap. This state feeds mux, volume and
   transport fallbacks, dashboards, and voice tools, so avoid
   duplicating probes in each caller. Runtime callers that need the
   effective audible source should prefer `RendererClient.selected_source()`
   when mux is available.
   If the renderer has a native event surface, add a wake adapter in
   `jasper/source_events.py`; the adapter marks the source dirty but must never
   choose a winner or command fan-in.
5. **Declare source metadata.** Add one `Source` enum member and one
   `MusicSourceSpec` in `jasper/music_sources.py`: public ID, fan-in
   label, renderer active key, `/sources/` wizard key, display name,
   and `volume_mode`. `VolumeMode.PUSH` means the source's own volume
   API carries `listening_level` and CamillaDSP returns to 0 dB.
   `VolumeMode.CAMILLA_MASTER` means CamillaDSP carries
   `listening_level`.
6. **Declare source lifecycle resources.** Add the source's operational
   resource group in `jasper/local_sources/registry.py`: persistent intent
   unit, runtime units, parked-follower units, advertise units, and
   audio-refresh units. Keep implementation
   subresources explicit here, as USB does with its process-free readiness marker and
   host-visible gadget owner. Then extend the fixed intent allowlist and add
   one concrete applier only if ordinary systemd enable/start/stop is not
   sufficient. Do not create a
   second persistence path or infer intent from process state.
7. **Define preemption.** Add the source-specific stop/pause/silence
   path to `jasper/mux.py`. Prefer a real renderer-owned API: AirPlay
   uses shairport-sync's native `DropSession` after a successful fan-in
   handoff, with MPRIS `Stop` only as a compatibility fallback. Spotify
   uses Web API pause with a restart fallback, and USB uses fan-in's
   lane-level MUTE/UNMUTE command. Cleanup failure must be observable and
   must not undo an already-completed handoff. If the source cannot be
   controlled from the Pi, document the intentional fallback ("may briefly
   mix") and expose an operator escape hatch only when the failure mode
   justifies one.
8. **Wire manual source selection.** The mux/control allow-lists derive
   from `jasper/music_sources.py`; add the landing-page button in
   `deploy/index.html` and keep `/sources/` as the on/off surface.
   `/source/select` only picks the lane the speaker should currently
   pass.
9. **Teach the coordinator source-specific volume I/O.** The handoff
   safety policy comes from `volume_mode`, but push-mode sources still
   need one `_set_<source>` dispatcher. Add inbound observation only if
   the source has a reliable user-facing volume surface.
10. **Decide transport/metadata truthfully.** If voice `pause`, `next`,
   `previous`, or `now playing` can control the source, wire
   `jasper/tools/transport.py`. If not, return a concrete "not
   supported for this source" response.
11. **Add operator surfaces and observability.** Update `/sources/` if
   the source can be enabled/disabled, `/state` if it has useful live
   state, `jasper-doctor` for topology drift and runtime health, and
   `jts-audio.slice` / no-swap checks for any resident audio-path
   daemon.
12. **Protect measurements and tests.** Add the source to the
   correction `measurement_window()` pause list if it can emit during a
   sweep. Add tests for asound wiring, fan-in config, source-state
   fail-soft behavior, mux preemption, source-handoff safety, volume
   dispatch, and any source wizard toggles.
13. **Update docs in one place, then link.** This section covers the
    cross-cutting checklist. The [documentation index](README.md) should link
    the current operational truth; historical design notes should be marked
    historical.

Renderer and TTS legs converge inside `jasper-fanin`, then pass through
CamillaDSP and into `jasper-outputd`, which owns the direct DAC writer
on current main. The legacy `pcm.jasper_out` dmix and its `v1.yml`
CamillaDSP config were retired (issue #2240); rollback to the
pre-outputd topology is git history plus a redeploy of an older build.

## Volume knobs and which path each affects

| Knob | Where it lives | Music | TTS |
|------|----------------|-------|----------------------------|
| CamillaDSP `main_volume` (listening level/source volume) | DSP, websocket port 1234 | yes | yes on pre-DSP fan-in; already upstream of passive outputd TTS |
| fan-in program duck | `jasper-fanin` TTS socket | yes | no |
| Source slider (iPhone, Spotify Connect, BT phone) | Renderer-side, before the fan-in lane | yes | no |
| Source amplitude (PCM data) | The WAV / TTS PCM buffer | yes | yes |
| Assistant loudness matcher (auto) | jasper-fanin + provider profiles | n/a | yes |
| Apple dongle Headphone | Hardware mixer | (pinned 100%) | (pinned 100%) |
| TPA3255 amp | Physical knob | yes | yes |

Two notes:
- `master_gain` is a CamillaDSP mixer named in the base Camilla configs
  but currently configured as identity. The Ducker operates on
  `main_volume`, not `master_gain`. Old comments/docs that called
  master_gain "the ducking knob" are wrong.
- `listening_level` is the canonical user-facing volume in the
  VolumeCoordinator. It maps to `main_volume` for IDLE, AirPlay, and
  USB sink; for Spotify and BT, `main_volume` stays pinned at 0 dB and
  the source slider carries `listening_level`. `listening_level=0` is special on every
  music source: Camilla also asserts `main_mute` and the calibrated
  volume floor (default −50 dB) so content mute means silent content
  rather than "very quiet."

## Assistant Loudness Matching

Since assistant audio normally enters the same DSP path as music, a fixed
provider PCM level would ignore how the user currently listens to music.
Current main keeps that compensation at one owner: the pre-DSP TTS mix boundary
in `jasper-fanin`.

1. The mix owner continuously measures content/music with a bounded
   K-weighted loudness window. In fan-in mode this measurement happens
   before program ducking and before TTS is mixed, so the assistant
   baseline tracks the renderer content level rather than the temporary
   ducked level.
2. At wake turn start, `jasper-voice` embeds `VOLUME_CONTEXT` directly in
   `PREPARE_ASSISTANT`, making the safety snapshot and assistant identity one
   atomic command. The context is absolute: canonical user dB, downstream Camilla
   dB, the quiet-room `tts_envelope(listening_level)` target, mute, and a
   `CLOCK_BOOTTIME` nanosecond stamp captured at snapshot acquisition and
   carried unchanged through publication. Separately published live updates
   retain the standalone `VOLUME_CONTEXT` command. Treating the envelope as a
   speaker target is load-bearing: fan-in subtracts downstream attenuation
   before gain calculation, so Camilla cannot attenuate the target twice.
3. The mix owner snapshots the current content loudness before ducking,
   then ignores content-meter updates while the voice turn or correction
   measurement window is active.
4. For each assistant/cue segment, `TtsPlayout` sends un-gained
   48 kHz stereo PCM plus optional source-loudness profile metadata.
   Sustained writes are paced (`_OUTPUTD_PACE_AHEAD_SEC`, 1.2 s) so at
   most ~1.2 s of audio is queued ahead of realtime: the mix owner's
   TTS lane keeps a bounded pending queue (2 s,
   `DEFAULT_MAX_PENDING_FRAMES` in `rust/jasper-fanin/src/tts.rs`) and
   drops audio commands that arrive while it is full
   (`event=fanin.tts_command_dropped`) rather than blocking the socket
   reader — a blocked reader would stall barge-in FLUSH behind queued
   audio. Without writer-side pacing, faster-than-realtime provider
   bursts (OpenAI Realtime delivers ~11 s of reply audio in ~4 s)
   overflow the budget and the surviving chunks play as garbled
   "fast-forward" audio. A contract test
   (`tests/test_tts_ipc_pacing.py`) pins the watermark against the
   Rust budget.
5. The mix owner chooses one reference, in order: qualified live music,
   held most-recent music, held assistant envelope offset, then first use. A
   music reference is held only after a full 3 s short-term window while the
   current period is audible; silence and brief cues cannot overwrite it.
   After 600 s of sustained content silence by default
   (`JASPER_FANIN_HELD_CONTENT_TTL_SEC`), held music expires so an old song
   cannot control speech hours later. Music gets the default `+1.5 LU`
   assistant offset.

   With no music, there is exactly one level function: the existing gentle
   quiet-room `tts_envelope(level)` (`-54 + 26 * level/100` LUFS). That function
   is the final speaker target: first use is exactly the envelope (offset zero),
   and the ordinary `+1.5 LU` assistant offset applies only to music-relative
   references. Completed assistant speech
   learns only the difference between its achieved speaker loudness and that
   deterministic target, clamps the learned correction to
   `JASPER_FANIN_ASSISTANT_ENVELOPE_OFFSET_LIMIT_LU` (default ±8 LU), persists
   it in the versioned fail-soft record, and later targets
   `envelope(current level) + offset`. It never turns the last response into a
   second volume curve.
6. Hearing safety is peak-aware and enforced at that boundary: the
   requested loudness gain is capped so the profiled source peak stays
   below the configured assistant peak ceiling (default `-3 dBFS`),
   then passed through the malformed-value floor. There is intentionally
   no fixed source-gain ceiling; the positive side is governed by the
   dynamic peak cap plus validated/fallback source-profile metadata. A new
   segment's lower cap applies to every rendered frame immediately, even while
   the continuity ramp is descending from a prior segment.
7. While speech is queued, each accepted `VOLUME_CONTEXT` re-targets gain at
   dequeue with a 100 ms ramp, mute override, and the original peak cap. A
   music-anchored segment uses `canonical delta - downstream delta`: a
   Camilla-master edit cancels at the mixer, while a push-mode edit adjusts TTS
   directly. A no-music segment uses
   `envelope delta - downstream delta`, so knob tracking keeps the gentle
   speech slope instead of switching to the steeper canonical music slope.
   Equal-level observations that repair Camilla also republish, because the
   downstream carrier changed even though the canonical percentage did not.
   For slow Spotify/Bluetooth writes, the coordinator publishes the already
   known push-mode intent before waiting for the source actuator, then publishes
   a fresh converged snapshot afterward. Mute is stricter: fan-in receives
   `muted=true` before the best-effort Camilla backstop or source slider, so a
   wedged local controller cannot delay the immediate speech stop. Ordinary
   Camilla-master edits publish only after their fast local write, avoiding a
   provisional two-ramp transient against stale downstream gain.
8. `SEGMENT_END` commits the calibration only for completed assistant speech.
   Fan-in remembers the last effective gain as audio drains, so it still commits
   when the queue becomes empty just before end arrives. Flush clears that
   candidate, and any muted rendered frame disqualifies the whole segment:
   interrupted/unheard tails, mute, cues, and chirps never train the record.

The passive bonded-member route sets `JASPER_TTS_MIX_STAGE=post_dsp` and uses
the same absolute context with `MixStage::PostDsp`. Outputd structurally treats
`downstream_db` as zero, because its assistant mix is already after CamillaDSP;
reusing fan-in's `- downstream_db` algebra there would double-compensate.
Outputd honors mute and live re-gain, and fails closed to silence when an
atomic turn-start context is missing or rejected.

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
  merged back into the same provider/model/voice profile. The
  measurement is finalized by `end_segment()` — called by the playout
  loop when the provider closes its audio iterator, and again
  (idempotently) by turn teardown, so providers whose iterator only
  closes on release (Gemini) still train the profile. Cues and
  chirps never train the profile.
- Cached cue WAVs and dynamic cue text do not train persisted
  provider profiles. `AudioCueManager` measures the exact 24 kHz mono
  cue PCM at playback and sends a one-shot `source_profile`
  (`provider=jts`, `model=cue-...` / `dynamic-text`) with
  `segment_kind="cue"`. Standalone feedback paths prepare assistant
  loudness context before ducking, so fan-in uses the current content
  baseline or listening-level-derived TTS envelope instead of falling
  back to its built-in quiet-room target.
- `jasper-voice` serializes assistant-owned output before it reaches
  fan-in. One voice turn owns the wake chirp, live assistant TTS, and
  end chirp as a single output episode. Proactive/admin speech (timer,
  research, supervisor, and `/cue/play`) starts only when no turn or
  other assistant episode is active. Dynamic text cache-fills before
  claiming a proactive episode, then checks the episode epoch again
  before writing so stale speech cannot reach the TTS lane after a
  newer turn has claimed output.
- Profiles are advisory. If a profile is missing or malformed, the mix owner
  uses conservative built-in fallback source loudness/peak values and
  still applies the dynamic peak cap and gain floor.

In the dual Apple active-output profile, TTS/cues enter fan-in instead
so they can pass through CamillaDSP crossover/protection. Fan-in accepts
the same outputd-compatible profile metadata, snapshots pre-duck content
loudness, applies the same profile/peak-capped gain decision, and emits
`event=fanin.assistant_loudness`. The resulting TTS/cue samples are then
mixed into the program buffer before CamillaDSP active crossover/protection.

Operator retunes live in `/var/lib/jasper/fanin.env`:

```
JASPER_OUTPUTD_ASSISTANT_OFFSET_LU=1.5
JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS=-3.0
JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_LUFS=-24.0
JASPER_OUTPUTD_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS=-6.0
JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS=-41.0
JASPER_OUTPUTD_CONTENT_SILENCE_LUFS=-60.0
JASPER_FANIN_HELD_CONTENT_TTL_SEC=600
JASPER_FANIN_ASSISTANT_ENVELOPE_OFFSET_LIMIT_LU=8
JASPER_FANIN_ASSISTANT_REFERENCE_PATH=/var/lib/jasper/assistant_volume_reference.json
```

When a cue/chirp/assistant segment arrives without a prepared wake-turn
context and without measurable content, fan-in uses
`JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS` as the final quiet-room
speaker target instead of a fixed fallback gain. This keeps no-context
feedback sounds on the same profile/peak-cap path as live assistant speech.

### Debugging Assistant Gain

Every assistant gain decision emits one structured journal line from the
active mix owner:

```
event=fanin.assistant_loudness kind=assistant provider=openai
  model=gpt-realtime-2 voice=verse reference=held_assistant
  calibrated=true confidence=0.82 baseline_lufs=-29.4 target_lufs=-27.9
  target_speaker_lufs=-39.5 envelope_offset_lu=1.2 source_lufs=-18.2
  source_peak_dbfs=-2.5 requested_gain_db=-9.7 peak_cap_gain_db=-0.5
  final_gain_db=-9.7 reason=target
```

`jasper-fanin` exposes the same decision fields under
`tts.assistant_loudness` in `/run/jasper-fanin/control.sock`, together with the
accepted volume context and stamp, held content/assistant values, the live
`envelope_offset_lu`, and `volume_context_rejected`. `/state.fanin`
embeds that STATUS block verbatim.

`jasper-doctor` warns if that telemetry is missing or malformed, and if
`final_gain_db` disagrees with the decision it came from — the published gain
must equal `max(gain floor, min(requested_gain_db, peak_cap_gain_db))`. It has
no fixed range to check against, because the ceiling is the per-decision peak
cap, not a literal. Use
that surface first when debugging a provider loudness report; it shows
whether the system used a calibrated profile, what content baseline it
matched, which reference won (`live_content`, `held_content`,
`held_assistant`, or `first_use_fallback`), and which clamp path applied (`target`, `peak_cap`,
`fallback_profile`, or `gain_floor`).

## End-of-turn drain — when is the speaker actually silent?

The TTS write call returns when bytes are accepted by the current
transport, **not** when they reach the DAC. On current main, the
transport is a local Unix socket into `jasper-fanin`; the legacy
rollback path used PortAudio. Either way, there is still transport
queue, fan-in/Camilla/outputd tail, and DAC flush ahead of the bytes
at that point. A naive
end-of-turn timer that fires "shortly after the last write" can land
mid-tail, clipping the last word — observed in production
(PR #311, 2026-05-25) when OpenAI Realtime burst-streamed 10 chunks
in 730 ms ahead of a 4 s playout.

`TtsPlayout` owns the end-of-turn drain semantic. The fan-in IPC-backed
implementation extends the same boundary with
`write_segment()`/`end_segment()` metadata for segment identity, but the
voice daemon still waits through the stable methods below:

- `expected_drain_at()` — monotonic deadline when the last-queued
  sample's tail will have cleared the OS audio stack. Backed by a
  single `_ring_end_monotonic` float that advances on each `write()`
  (anchors fresh on now() if the speaker was idle; appends during
  back-pressure). Reset by `flush()` since barge-in's `abort()`
  discards the ring. Returns `0.0` when nothing is queued — naturally
  reads as "already drained" against `time.monotonic()`.
- `wait_drained()` — single `asyncio.sleep` to the deadline. No
  polling because the deadline is known up-front.

For interruption, `TtsPlayout.flush()` uses fan-in's
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
Whichever observes "drained" first completes its background task and lets
WakeLoop schedule `_end_turn`; the session-frame done-task check remains
as a backup, and the loser's task is cancelled cleanly.

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
Goes through CamillaDSP, so `main_volume` applies. On a box whose
`correction` renderer-ingress lane is armed onto its SHM ring
(`jasper-audio-config renderer-lanes` reports it; the fleet default is
unarmed), fan-in reads the ring instead of this alias, so use
`aplay -D correction_ring_lane file.wav` there — the product's own
measurement spawns pick the right one automatically
(`jasper.audio_measurement.correction_lane.correction_play_device`).

**Test the TTS chain**: use `jasper-voice`/cue playback or the canonical
local TTS socket, `/run/jasper-fanin/tts.sock`. (The `jasper_out`
pre-outputd rollback dmix was retired — issue #2240.)

On Apple-dongle installs, the dongle `Headphone` control is pinned at
100% by `jasper-dac-init`, watched by `jasper-headphone-monitor`, and
checked by `jasper-doctor`. Those services are enabled only when
`jasper-audio-hardware-reconcile` recognizes the selected final-output
DAC as the Apple USB-C dongle; DAC8x and unknown-output states disable
the Apple-specific units. The reconciler runs at install/boot and from
udev `controlC*` add/remove/change events, so USB DAC changes converge
without a deploy-only scan. The helper scripts remain runtime-safe for
manual/operator starts. `outputd_dac` still points at the detected
single-device final-output card. The same reconcile pass writes
`/run/jasper-output-hardware/output_hardware.json`, the observed output-hardware state that
`/state` exposes as `audio.output_hardware`; `/sound/output-topology` uses a
ready observed shape to seed an unsaved output-map draft when no saved topology
exists. Two Apple USB-C adapters can therefore be visible as an observed
four-output shape without implying that outputd has switched to a dual-sink
runtime graph.

The old DAC8x final-output alias route has been removed. `outputd_dac`
renders directly to an ordinary recognized final-output card for every
registered single DAC profile — no profile gets a converting `plug` in
front of it (PR-4, format-foundation, deleted the last one). The
InnoMaker HiFi AMP Pro was the first profile-scoped
*format* exception: the kernel DAI (`ma120x0p.c`) advertises only
S24_LE/S32_LE at continuous 44.1-192 kHz rates (a driver-advertisement
limit, not a documented silicon one), so its registry profile declares an
`S32_LE` final edge. The base HiFiBerry DAC8x now declares the same
`S32_LE` edge (wide-output-path PR-7) for a different reason: it is the
intended horn-lane fix (acoustic verdict pending — see plan §6 PR-7) — a
2-channel `aplay --dump-hw-params` open test on jts3 confirmed the S32 edge
opens cleanly (2026-08-07). The profile's *capability* is 8 channels while
jts3 runs a 2-channel active 2-way, so no width above 2 has been paired with
S32_LE on this silicon; that pairing fails closed at the ALSA open rather
than being pre-verified if unsupported. Declaring it lets outputd's
i32 program spine reach the DAC with zero narrowing where an undithered
16-bit requantization used to crackle on decay tails. DAC8x Studio's
registry entry declares `S16_LE`, unchanged: it shares
the base DAC8x's DAC-chip family, but no lab unit exists to
run the same hardware probe, so the registry does not flip it on inference
alone. The two boards do NOT share an overlay or a driver — the Studio has
its own `hifiberry-studio-dac8x` overlay and machine driver (raspberrypi/linux,
2026-01-15) — and the base profile now matches only the one card name its
own driver emits. Studio silicon on the Studio driver stack classifies as
`hifiberry_dac8x_studio`; under the base overlay it stays on the base row
(ADR-0232). Two residuals stay documented: a
Studio board configured with the base overlay is genuinely indistinguishable,
and on rpi-6.18.y and later the Studio family shares one card name, so a
Studio DAC8x parks as `unknown` (#2258). The Apple USB-C dongle
declares the packed 24-bit `S24_3LE` edge (wide-output-path PR-8) for a third
reason: that device advertises exactly `S16_LE` and `S24_3LE` at
48 kHz/2ch and no 32-bit width at all, so the packed edge is simply the widest
wire the silicon has (`aplay -D hw:A -f S24_3LE` open-proof on jts.local,
2026-08-08). That declaration covers a **single** armed dongle; the dual-Apple
composite still declares `S16_LE`, because outputd's paired composite sink has
no packed-24 child write path and refuses that width at open — so a composite
and its child profile legitimately differ, and the reconciler resolves the
emitted format by ARMED-profile id rather than through `child_profile_ids`.
For every declaring profile, outputd requests that format directly
on the raw `hw:` open and writes its i32 program straight through at that
edge (its internal program spine is i32, so an S32 edge converts nothing,
while the packed 24-bit edge narrows once to 24 significant bits and packs
three little-endian bytes per sample through its own write path);
because there is no conversion layer in front of the card, outputd's own
client-edge readback is now the hardware-edge proof — the pinned-slave
`plug` that used to own that guarantee is gone. InnoMaker's profile also
declares a width-2 active-output lane, so `jasper-audio-hardware-reconcile`'s
active-graph gate is consulted for it like any other coherent single DAC;
`OUTPUTD_ACTIVE_MODE` still only reaches the render as `1` once a legal
active graph is the live CamillaDSP config, so an uncommissioned box stays
byte-identically passive. The render script no longer needs its own
per-profile rejection for this — that guarded against a converting plug
remixing channels, and no plug remains; a wrongly-requested channel count
now fails closed at ALSA's `set_channels` on the raw `hw:` open instead.
Active-speaker channel
ownership lives in `/var/lib/jasper/output_topology.json` and the generated
active CamillaDSP graph, not in an ALSA alias. `/sound/output-topology` records
physical DAC lanes, speaker groups, passive/active modes, subwoofers, and
safety evidence without playback authority of its own. The web save route
separately coordinates park, commit, and safe runtime convergence; reset writes
zero speaker groups and remains parked.
`/sound/active-speaker/channel-identity` records operator-confirmed physical
channel identity on that saved topology, but still grants no playback
authority. Product active-driver playback uses the protected active graph via
`/sound/active-speaker/commission-load` and
`/sound/active-speaker/commission-ramp-*`; passive/full-range layouts have no
separate active driver test in the product UI. Generic `aplay` tone playback is
explicit lab mode only and must point at a dedicated non-daemon test PCM, never
at outputd/CamillaDSP product lanes. The topology itself still grants no
playback authority.
`/sound/active-speaker/driver-measurement`,
`/sound/active-speaker/summed-test`, and
`/sound/active-speaker/summed-validation` persist commissioning evidence only;
they do not apply the normal active profile. `/sound/active-speaker/summed-test`
is the audible exception: it temporarily loads the protected all-drivers-live
commissioning graph through the active-speaker runtime lane, plays the bounded
combined speech test on `correction_substream`, accepts live level
changes through `/sound/active-speaker/summed-test/level`, records only an
audible completed result, and rolls back. A play completes two ways: the
operator confirms hearing it, or the request's own `duration_ms` budget elapses
after at least one whole stimulus repeat played cleanly. Stopped-before-audio,
watchdog-expired, artifact-only, or stale summed-test records remain evidence of
an incomplete check, not unlock tokens for the baseline compiler.
Driver evidence is accepted only for the current saved physical target and
matching safe-session floor result, so changing the speaker layout or DAC output
assignment invalidates old evidence for readiness. Summed validation must
reference the latest current audible combined-driver test for that speaker
group; the product flow can use an explicit operator listening check when no
phone-mic reading is present, while artifact-only or stale tests cannot satisfy
the baseline compiler.
`/sound/active-speaker/baseline-profile/save-and-apply` is the product
active-speaker handoff into normal playback: the backend compiles, validates
apply support, applies, and reports one result. The lower
`/sound/active-speaker/baseline-profile/apply` endpoint remains the apply
primitive, but the product UI does not ask the browser to stitch save and apply
together. Apply is enabled only for an outputd-owned active playback lane. Today
that product handoff is
profile-declared for a single Apple USB-C dongle at width 2, the InnoMaker
HiFi AMP Pro at width 2, DAC8x/DAC8x Studio
at width 8, and the dual-Apple USB-C composite at width 4. Protected startup
staging follows the durable-outputd boundary: supported DACs resolve to the
active outputd lane instead of opening `hw:<card>,0` directly, so normal
`jasper-outputd` ownership is not bypassed.
The production automatic measurement flow is intentionally narrower than this
transport capability: it requires a two-way preset and a DAC profile with
`supports_active_crossover_commissioning=True`, currently only the base DAC8x.
Other active-lane devices remain modeled but cannot enter that flow.
Do not infer active-speaker runtime width from physical DAC output count. The
diagnostic route can use the saved single-DAC physical width, but product apply
width is declared by the active DAC profile; an eight-output DAC can still lack
a durable eight-lane handoff until the outputd ALSA lane, CamillaDSP generation,
staging, baseline compilation, and guards are widened as one contract.
When apply is enabled, it still goes through the shared DSP apply
transaction before CamillaDSP runs the generated baseline profile. Software
never touches downstream amp gain. The amp gain is a physical knob set at
install time.
The same topology surface reports the detected output clock domain. Supported
topology hardware IDs include one Apple dongle, HiFiBerry DAC8x/DAC8x Studio,
the InnoMaker HiFi AMP Pro, and the special
`dual_apple_usb_c_dac_4ch` pair. The dual-Apple option is valid
only for exactly two Apple child DACs on the expected same USB controller/bus,
one speaker-local stereo pair per DAC, and exactly four physical outputs.
Stored 900 s common-clock drift evidence is surfaced as validation evidence;
missing evidence warns, failed evidence blocks, and missing/partial live
hardware observation blocks the composite clock report. When the live dual pair
is ready, reconcile promotes `jasper-outputd` to `JASPER_OUTPUTD_SINK=dual_apple`
and pins DAC A/B from the saved topology child identity; if only observed
hardware order is available, that order is used only as first-time bootstrap.
Partial states, USB topology mismatches, or saved-topology identity mismatches
park normal output rather than silently routing four active lanes to the wrong
dongle. Generic USB DAC aggregation through ALSA `multi`/`dmix`/`plug` or
CamillaDSP multi-device output remains unsupported.

## AEC bridge implications

The bridge receives outputd's speaker monitor over localhost UDP, and that
is its only reference source. There is no pre-DSP ALSA reference: the
summed fan-in output reaches CamillaDSP over Ring A, which no diagnostic
tap reads.

- Production AEC consumes outputd's 48 kHz stereo speaker monitor over
  UDP. That reference includes renderer/content, TTS/cues, fan-in
  ducking/gain, CamillaDSP filters/crossover/protection, and outputd sink
  selection. It is the final software/electrical reference; no software
  reference can include DAC, amp, driver, or room acoustics except through
  microphone observation.
- A 25 dB ducking step is a transient the AEC's adaptive filter has
  to re-converge through. The old remedy — move the tap downstream of
  CamillaDSP — is already in place: every AEC reference now comes from
  outputd's post-DSP speaker monitor, so the duck is inside the
  reference rather than divergent from it.
- Chip-AEC addition: production chip-AEC mode and the wake-corpus
  chip-AEC comparison profile also ask outputd to publish the same final
  speaker monitor as an XVF USB-IN reference. The UDP tap stays at
  outputd's 48 kHz graph rate; the XVF USB-IN side output is downsampled
  to the chip's 16 kHz playback contract. The production path is opt-in via
  `JASPER_WAKE_LEG_CHIP_AEC=1` / `JASPER_AEC_CHIP_AEC_ENABLED=1`;
  the recorder owns the same overlay during corpus chip-AEC comparison
  sessions and removes its test env when corpus mode exits.

---

Last verified: 2026-08-26 against `deploy/alsa/asoundrc.jasper`,
`deploy/alsa/conf.d/`, `deploy/modprobe.d/snd-aloop.conf`, and
`jasper/control/transport_park.py`.
