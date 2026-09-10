# Audio paths and software volume knobs

One audio path reaches the final output owner. On solo and active-output
speakers, renderer audio and assistant TTS converge in `jasper-fanin` before
CamillaDSP, then `jasper-outputd` owns the final hardware sink. A passive
bonded member is the deliberate exception: its local TTS enters outputd after
CamillaDSP. That mix-stage boundary is what makes volume-controlled output and
assistant loudness behave differently on the two shapes, so it is the first
thing to establish when testing either.

Why the pre-mix sits in fan-in at all — CamillaDSP takes one ALSA capture
device per process — is [ADR-0282](adr/0282-the-pre-mix-lives-in-fan-in.md).

## The physical path

```
MUSIC / CONTENT chain (gets CamillaDSP processing)
    shairport-sync, librespot, bluealsa-aplay, correction/test playback
        → private snd-aloop lanes: hw:Loopback,0,N → hw:Loopback,1,N ─┐
    USB audio (UAC2 gadget), where the reconciler has armed           │
    JASPER_FANIN_USB_DIRECT                                           │
        → jasper-fanin DIRECT-captures hw:UAC2Gadget: no aloop hop,   │
          no bridge process (ADR-0107) ───────────────────────────────┤
                                                                      ▼
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

Renderer ingress has two shapes and no third: an snd-aloop lane, or fan-in
opening a capture device itself
([ADR-0281](adr/0281-renderer-ingress-is-aloop-lanes-plus-usb-direct-capture.md)).
Only the `usbsink` lane takes the second, and only where it is armed.

**The USB leg specifically.** `usbsink` is the one lane with no aloop
substream: the default `input_pcms` list is one entry shorter than the renderer
list (`hw:Loopback,1,3` is absent and the surviving pairs do not renumber), and
`JASPER_FANIN_USB_DIRECT` decides whether the lane has a transport at all.
Armed (the literal `enabled`), fan-in opens `hw:UAC2Gadget` as an S32_LE
capture and feeds the lane resampler, which is the whole USB data plane
(ADR-0107). Unarmed, the lane is `LaneSource::Disabled`: it opens nothing and
renders silence, keeps its roster label so mux can still address it, and
publishes `source: "disabled"` in `STATUS`. USB audio is then *unavailable*
rather than degraded — there is no aloop fallback for USB. Arming is not an
operator toggle: the
reconciler is the single writer of that key and arms it on any box that has
the resolved USB gadget capability, USB Audio Input on in the household, a role
that permits local sources, and the coordinator-derived
`jasper-usbsink.service` enablement. Off such a box it writes the explicit
`disabled` rather than unsetting, so a stale `enabled` in
`/etc/jasper/jasper.env` cannot win. The decision lives in
`jasper/fanin/coupling_auto.py`; the env I/O and daemon transitions in
`jasper/fanin/coupling_reconcile.py`. `jasper-usbsink.service` itself runs no
process — it is the readiness marker the coordinator drives.

The lane labels, and which label carries which source, are owned in two places
and mirrored nowhere: the compiled-in `input_pcms` / `input_renderers` default
arrays in `Config::from_env` (`rust/jasper-fanin/src/config.rs`, positionally
aligned), and `MUSIC_SOURCE_SPECS` in `jasper/music_sources.py`. The ALSA
substream-pair allocation behind the aloop lanes is owned by
`deploy/modprobe.d/snd-aloop.conf`. Room-correction and test playback have
their own `correction` lane, always mixed; fan-in sums the lanes, writes Ring A
and does **nothing else**.

The TTS chain above is also the active-output topology: assistant audio stays
in fan-in upstream of CamillaDSP so it rides the crossover/protection graph,
and outputd's post-crossover TTS mixer is not armed on active endpoints.
Passive/dumb bonded multiroom members are the exception — the grouping
reconciler points voice at `/run/jasper-outputd/tts.sock` and outputd mixes
that speaker's own assistant audio into its local post-round-trip content lane,
so replies do not ride the shared sync buffer.

The SHM slot ring is the only transport between fan-in, CamillaDSP and outputd.
The program wire is `S32_LE` and nothing else: fan-in publishes it
unconditionally, and a `JASPER_FANIN_RING_WIRE_FORMAT` naming any other format
— `S16_LE` above all — is refused as a config-class fault (exit 78, the unit
parks) rather than served, because the Python side still renders the ioplug
conf.d from that key and a narrower declaration would shear against the ring
header. There
is no coupling to declare either — the Python selector vocabulary is gone, and
fan-in still refuses any `JASPER_FANIN_CAMILLA_COUPLING` token but
unset/empty/`shm_ring` as a config-class fault (exit 78, the unit parks) until
that accept-set is removed too. The reconciler's sweep unsets a stale value in
the `fanin.env` it owns, but not in `/etc/jasper/jasper.env`, which fan-in also
loads — so a hand-set copy there still parks the daemon; `grep -R
JASPER_FANIN_CAMILLA_COUPLING /etc/jasper/ /var/lib/jasper/` is the check.
CamillaDSP
writes the post-DSP stereo program to `jts_ring_playback` and outputd consumes
Ring B one DAC-sized slot at a time. A roleful (active-crossover) box has a
ring of its own, carrying POST-crossover per-driver channels rather than a
full-range stereo program. That role rides the device NAME because it cannot
ride the width — on a 2-way speaker both rings are 2 channels — so outputd
admits the ACTIVE ring only when the hardware reconciler's
`JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT` marker says so. The snd-aloop route
between those three daemons, and the machinery that migrated a live box between
the two, were retired under [ADR-0100](adr/0100-one-audio-transport.md).

A topology the ring cannot serve does not degrade onto a second path — it
parks loudly: doctor FAIL, `/system/snapshot.transport_park`, and one row per
park on the `/system` page, which is the only browser-facing park presentation
([ADR-0187](adr/0187-park-presentation-is-the-system-screen-only.md)).
`jasper/control/transport_eligibility.py` is the single classifier all three
surfaces read, so they cannot name different reasons for one box. Hard-park refusals
that prevent guessing — a full-range program into a protected driver — are
separate and survive: hearing safety, not transport arbitration.

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
  including USB frame flow, becomes the winner, and losing the winner falls
  back to the newest source still active. Manual mode persistently pins the
  user-selected source; `/sources/` disables sources entirely. Native producer
  events are wake hints only — `jasper/source_events.py` translates librespot
  inotify and AirPlay/Bluetooth D-Bus signals, fan-in sends USB frame-flow
  edges over mux's UDS — and every hint plus the fixed 1 Hz lost-alert patrol
  enters the same reconciler, which re-reads source state before applying
  policy. Alert arrival order never chooses the winner. Source metadata
  (fan-in lane label, volume carrier) lives in `jasper/music_sources.py`;
  operational lifecycle resources — the units that run, advertise, park while
  paired as a follower, restore on unpair and refresh after audio-graph
  changes — live in `jasper/local_sources/registry.py`, which declares
  resources while the handoff below owns how intent is applied.
- Mux's `STATUS` already answers "what is audible now" in one field,
  `active_source`, which folds the test lease, the manual pin and a still-playing
  winner into one name. Readers consume that field and do not rebuild the answer
  from the pin and the raw winner: `RendererClient.selected_source()` returns it
  verbatim. Mid-handoff the honest answer would be `idle` — the losing source has
  stopped and the winner is not committed — so while the transition lock is held
  mux keeps answering its **last committed** name instead. That is what lets a
  reader treat a plain `idle` as *true* idle: the volume coordinator maps it to
  `Source.IDLE` and takes the attenuating Camilla-master carrier rather than
  resolving a carrier against a lane mux is about to leave. Two names `/state`
  will not report are `idle` itself and the fan-in test lane's label (a
  measurement holding the lease); both fall through to the raw probes. Every reader is fail-soft: an
  unreachable mux, an unparseable reply or a missing field is `None`, never an
  error. `active_renderers()` stays the raw per-renderer view.
- One source can be silenced through mux from outside: `PREEMPT airplay`.
  Deliberately AirPlay-only — its escalation is bounded by two 2 s `busctl`
  calls a socket client can wait out, while Spotify's tier-2 `try-restart` is an
  8 s worst case no client can, and nothing calls the other lanes. Any other
  name is refused with an `error` payload rather than silently ignored, and
  callers pass a 6 s timeout to cover the bounded escalation.
- Before mux exposes a new lane it asks
  `VolumeCoordinator.prepare_source_handoff(...)` to make the target volume
  carrier safe, sends `SELECT <label>` only then, and converges the
  steady-state carrier afterwards with `finalize_source_handoff(...)`. This is
  the guard against loud source-switch transients such as Spotify (Camilla
  0 dB) → AirPlay (Camilla-as-master). Mux logs one
  `event=source.handoff_start` and one terminal `event=source.handoff` per
  transition, both with a stable `id` also exposed in
  `/source/state.last_handoff`; a converge that fails after the gate has
  already moved logs `event=source.handoff_finalize_failed` with that same
  `id`, and the terminal record carries `result=finalize_failed`. So a switch
  is correlatable across journal, dashboard and control API without
  phase-by-phase log spam.
- `jasper-fanin` owns the cheap audio gate and nothing above it. Its control
  socket takes `STATUS`, `SELECT <label>` (pass one renderer lane), `NONE`
  (pass none), `MUTE`/`UNMUTE <label>` (mux's USB preemption) and
  `TAP_ARM`/`TAP_DISARM` (the diagnostic impulse tap). None of them carry policy — fan-in never chooses a
  source. The correction/test lane is always mixed so diagnostics and room
  correction still work. Fan-in starts in `NONE`, and mux keeps it there
  whenever no source has a guarded winner.
- `jasper-control` is the HTTP proxy for the web UI, and merges `/sources/`
  availability into `/source/state` so unavailable renderers can be disabled in
  the landing-page selector.

Those three sockets share one client and one vocabulary. Every sender goes
through `jasper.platform.uds.daemon_command`, whose single timeout covers
connect, send, response and close; every command line is built by a helper in
`jasper.platform.wire`. For the verbs `wire.py` owns — the fan-in select and
mute verbs, the mux verbs and the TTS verbs — neither a caller nor a test
spells one as a literal, so a rename is one edit. The diagnostic tap is the
exception: `jasper/route_latency/tap_client.py` builds `TAP_ARM`/`TAP_DISARM`
as literals over its own `AF_UNIX` socket and uses neither helper.

## Adding a new music source

The canonical checklist for another source that should play as
**music/content**. Not for TTS, cues, wake sounds or other assistant-owned
audio, which stay on the TTS/test-tone path. Keep the change boring: a new
source looks like the existing AirPlay, Spotify, Bluetooth or USB lanes, and
introduces no second mixer, second output device or new volume model.

1. **Give it one private fan-in lane.** Either an snd-aloop lane — one PCM
   alias in `deploy/alsa/asoundrc.jasper`, pinned to 48 kHz stereo `S32_LE` via
   `plug`, over a substream pair allocated in `deploy/modprobe.d/snd-aloop.conf`
   — or, when fan-in can capture the source from a card of its own, a direct
   capture like the UAC2 gadget's: no alias, no aloop pair, fan-in opens the
   device. If the aloop pairs are exhausted, redesign the topology rather than
   overloading snd-aloop.
2. **Teach `jasper-fanin` about the lane.** Extend the compiled-in `input_pcms`
   and `input_renderers` default arrays in `Config::from_env`, keeping them
   positionally aligned. The `JASPER_FANIN_INPUT_PCMS` /
   `JASPER_FANIN_INPUT_RENDERERS` env vars only *override* those defaults and
   are not set by `deploy/systemd/jasper-fanin.service`, so editing the unit
   alone does nothing. The lists are pipe-delimited because ALSA `hw:` names
   contain commas. A configured input is part of the production graph: if it
   cannot be opened, fan-in fails loudly. Keep the label stable — mux uses it
   to ask fan-in for one selected lane.
3. **Wire the source daemon to the alias**, never to a ring PCM or a raw
   `hw:Loopback,*` name. Order the unit after `jasper-fanin.service` and reuse
   the existing sources' hardening/resource patterns. An optional source
   defaults off and costs zero resident RAM while disabled.
4. **Expose fail-soft playing state.** Add one probe in
   `jasper/source_state.py` and surface it through
   `RendererClient.active_renderers()`, preserving the public bool contract
   (`False` plus debug logging on failure). If mux needs it for arbitration,
   also expose a tri-state observation where `None` means unknown, so one
   failed read is a bounded grace rather than a stop/start flap. This state
   feeds mux, volume, dashboards and voice tools — do not duplicate the probe
   per caller. If the renderer has a native event surface, add a wake adapter
   in `jasper/source_events.py`; it marks the source dirty and must never
   choose a winner or command fan-in.
5. **Declare source metadata.** One `Source` enum member and one
   `MusicSourceSpec` in `jasper/music_sources.py`: public ID, fan-in label,
   renderer active key, `/sources/` wizard key, display name, `volume_mode`.
   `VolumeMode.PUSH` means the source's own API carries `listening_level` and
   CamillaDSP returns to 0 dB; `VolumeMode.CAMILLA_MASTER` means CamillaDSP
   carries it.
6. **Declare source lifecycle resources** in `jasper/local_sources/registry.py`:
   persistent intent unit, runtime units, parked-follower units, advertise
   units, audio-refresh units, and any implementation subresource (as USB does
   with its readiness marker and gadget owner). Extend the fixed intent
   allowlist, and add a concrete applier only where plain systemd
   enable/start/stop is not enough. Never add a second persistence path or
   infer intent from process state.
7. **Define preemption** in `jasper/mux.py`, preferring a renderer-owned API:
   AirPlay uses shairport-sync's native `DropSession` after a successful fan-in
   handoff, Spotify uses Web API pause, USB uses fan-in's lane-level
   `MUTE`/`UNMUTE`. Cleanup failure must be observable and must not undo an
   already-completed handoff. Do not add a per-source escape-hatch env var to
   turn the preemption off — a preemption that does not work is a bug to fix. A
   source that genuinely cannot be controlled from the Pi documents that it may
   briefly mix. A caller outside mux that needs a source silenced asks mux over
   the control socket rather than reaching for the renderer itself — but only
   AirPlay is exposed that way (see below), so do not assume a new source earns
   a `PREEMPT` verb.
8. **Wire manual source selection.** The mux/control allow-lists derive from
   `jasper/music_sources.py`; add the landing-page button in
   `deploy/index.html` and keep `/sources/` as the on/off surface.
9. **Teach the coordinator source-specific volume I/O.** Handoff safety policy
   comes from `volume_mode`, but a push-mode source still needs one
   `_set_<source>` dispatcher. Add inbound observation only if the source has a
   reliable user-facing volume surface.
10. **Decide transport/metadata truthfully.** If voice `pause`, `next`,
    `previous` or `now playing` can control the source, wire
    `jasper/tools/transport.py`; if not, return a concrete "not supported for
    this source" response.
11. **Add operator surfaces and observability**: `/sources/` if it can be
    enabled/disabled, `/state` if it has useful live state, `jasper-doctor` for
    topology drift and runtime health, and `jts-audio.slice` / no-swap checks
    for any resident audio-path daemon.
12. **Protect measurements and tests.** Add the source to the correction
    `measurement_window()` pause list if it can emit during a sweep, and add
    tests for asound wiring, fan-in config, source-state fail-soft behavior,
    mux preemption, source-handoff safety, volume dispatch and wizard toggles.
13. **Update docs in one place, then link.** This section is the cross-cutting
    checklist; the [documentation index](README.md) links current operational
    truth.

## Volume knobs and which path each affects

| Knob | Where it lives | Music | TTS |
|------|----------------|-------|----------------------------|
| CamillaDSP `main_volume` (listening level/source volume) | DSP, websocket port 1234 | yes | yes on pre-DSP fan-in; already upstream of passive outputd TTS |
| fan-in program duck | `jasper-fanin` TTS socket | yes | no |
| Source slider (iPhone, Spotify Connect, BT phone, host USB) | Renderer-side, before the fan-in lane | yes | no |
| Source amplitude (PCM data) | The WAV / TTS PCM buffer | yes | yes |
| Assistant loudness matcher (auto) | jasper-fanin + provider profiles | n/a | yes |
| CamillaGUI (expert door) | Third-party GUI on loopback `127.0.0.1:5005`, reached over an ssh tunnel | yes | yes |
| Apple dongle Headphone | Hardware mixer | (pinned 100%) | (pinned 100%) |
| TPA3255 amp | Physical knob | yes | yes |

Notes:

- `master_gain` is the CamillaDSP mixer the graph changes WIDTH at — identity
  on a flat stereo passive box, but carrying the mono fold, the active split
  and the composite program-dest map on the shapes that need them
  (`jasper/sound/camilla_yaml.py`). Ducking never touches it; Camilla-side
  ducking operates on `main_volume`. Old comments and docs that called
  `master_gain` "the ducking knob" are wrong.
- The voice loop owns the duck/restore lifecycle and sends it through
  `TtsPlayout` down the same socket as the speech it is ducking for
  (`event=voice.duck`, `event=voice.duck_failed` — the voice loop's own
  prefix, because the voice loop is what ducked); fan-in owns where the
  attenuation happens, logs its own
  transitions as `event=fanin.program_duck`, and takes the depth from
  `JASPER_FANIN_TTS_PROGRAM_DUCK_DB` (cues: `JASPER_FANIN_TTS_CUE_DUCK_DB`).
- CamillaGUI is an operator escape hatch, not part of the product path: it can
  live-apply a config that raises `devices.volume_limit` past the 0 dB hearing
  ceiling. [SECURITY.md](../SECURITY.md) owns that boundary and the remedy.
- `listening_level` is the canonical user-facing volume in the
  VolumeCoordinator. It maps to `main_volume` for IDLE, AirPlay, and
  USB; for Spotify and BT, `main_volume` stays pinned at 0 dB and
  the source slider carries `listening_level`. `listening_level=0` is special on
  every music source: Camilla also asserts `main_mute` and the calibrated
  volume floor (default −50 dB) so content mute means silent content
  rather than "very quiet."

## Assistant loudness matching

Assistant audio enters the same DSP path as music, so a fixed provider PCM
level would ignore how the user currently listens. One owner compensates: the
pre-DSP TTS mix boundary in `jasper-fanin`. What follows is the cross-process
contract; the reference/held-content algorithm itself lives in
`rust/jasper-fanin/src/` (`loudness.rs`, `tts.rs`) and is not restated here.

**What Python sends fan-in over `/run/jasper-fanin/tts.sock`:**

- `PREPARE_ASSISTANT` at wake-turn start, with `VOLUME_CONTEXT` embedded so the
  safety snapshot and the assistant identity are one atomic command. The
  context is absolute: canonical user dB, downstream Camilla dB, the quiet-room
  `tts_envelope(listening_level)` target, mute, and a `CLOCK_BOOTTIME`
  nanosecond stamp taken at snapshot acquisition and carried unchanged.
- A standalone `VOLUME_CONTEXT` for every later live volume or mute change.
  Mute is stricter: fan-in receives `muted=true` *before* the best-effort
  Camilla or source-slider write, so a wedged local controller cannot delay a
  speech stop.
- Un-gained 48 kHz stereo PCM per segment plus optional source-loudness profile
  metadata, paced to at most `_OUTPUTD_PACE_AHEAD_SEC` (1.2 s) ahead of
  realtime. That pacing is load-bearing: providers deliver faster than realtime
  (OpenAI Realtime, ~11 s of reply audio in ~4 s), and without it the surviving
  chunks play as garbled fast-forward audio.
- `SEGMENT_END`, which commits calibration for completed assistant speech only.

**What fan-in guarantees back:**

- The envelope target is a *speaker* target: fan-in subtracts downstream
  attenuation before computing gain, so Camilla cannot attenuate it twice.
  Reusing that `- downstream_db` algebra in outputd's post-DSP mix would
  double-compensate, which is why the passive bonded route
  (`JASPER_TTS_MIX_STAGE=post_dsp`, `MixStage::PostDsp`) treats `downstream_db`
  as zero. Outputd honors mute and live re-gain, and fails closed to silence
  when an atomic turn-start context is missing or rejected.
- Its TTS lane keeps a bounded pending queue (2 s, `DEFAULT_MAX_PENDING_FRAMES`
  in `rust/jasper-fanin/src/tts.rs`) and drops audio commands arriving while it
  is full (`event=fanin.tts_command_dropped`) rather than blocking the socket
  reader, which would stall a barge-in `FLUSH_SYNC` behind queued audio.
  `tests/test_tts_ipc_pacing.py` pins the writer watermark to that budget.
- Hearing safety is peak-aware here: requested gain is capped so the profiled
  source peak stays under the assistant peak ceiling (default `-3 dBFS`), then
  floored. There is deliberately no fixed source-gain ceiling — the positive
  side is the dynamic peak cap — and a new segment's lower cap applies to every
  rendered frame immediately, even mid-ramp from a prior segment.
- Any muted rendered frame disqualifies a whole segment and `FLUSH_SYNC` clears
  the candidate: interrupted tails, cues and chirps never train the record.

**What Python owns alone** — provider source profiles, persisted in
`/var/lib/jasper/assistant_loudness_profiles.json`
(`JASPER_ASSISTANT_LOUDNESS_PROFILE_PATH` overrides). `/assistant/voice/`'s
**Save and Test** synthesizes one phrase, measures it silently and stores the
profile in one provider attempt; daemon-start seeding stays opt-in
(`JASPER_ASSISTANT_LOUDNESS_AUTO_SEED=1`). Live assistant PCM is measured
passively after real replies and merged into the same provider/model/voice
profile, finalized by `end_segment()` from the playout loop and again
idempotently by turn teardown, so a provider whose iterator only closes on
release (Gemini) still trains. Cue PCM sends a one-shot `source_profile` with
`segment_kind="cue"` and never trains a persisted profile. Profiles are
advisory: missing or malformed, fan-in falls back to conservative built-in
source loudness/peak values and still applies the peak cap and gain floor.

Operator retunes live in `/var/lib/jasper/fanin.env`:

```
JASPER_FANIN_ASSISTANT_OFFSET_LU=1.5
JASPER_FANIN_ASSISTANT_MAX_PEAK_DBFS=-3.0
JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_LUFS=-24.0
JASPER_FANIN_ASSISTANT_FALLBACK_SOURCE_PEAK_DBFS=-6.0
JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS=-41.0
JASPER_FANIN_ASSISTANT_ENVELOPE_OFFSET_LIMIT_LU=8
JASPER_FANIN_ASSISTANT_REFERENCE_PATH=/var/lib/jasper/assistant_volume_reference.json
JASPER_FANIN_CONTENT_SILENCE_LUFS=-60.0
JASPER_FANIN_HELD_CONTENT_TTL_SEC=600
```

A cue, chirp or assistant segment arriving with no prepared wake-turn context
and no measurable content uses `JASPER_FANIN_ASSISTANT_DEFAULT_TTS_ENVELOPE_LUFS`
as its final quiet-room speaker target, keeping no-context feedback sounds on
the same profile and peak-cap path as live speech.

### Debugging assistant gain

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

`jasper-doctor` warns if that telemetry is missing or malformed, or if
`final_gain_db` disagrees with the decision it came from — the published gain
must equal `max(gain floor, min(requested_gain_db, peak_cap_gain_db))`. It
checks no fixed range, because the ceiling is the per-decision peak cap, not a
literal. Read that surface first when debugging a loudness report: it names
whether a calibrated profile was used, which reference won (`live_content`,
`held_content`, `held_assistant`, `first_use_fallback`) and which clamp path
applied (`target`, `peak_cap`, `fallback_profile`, `gain_floor`).

## End-of-turn drain

TTS writes record bytes accepted by the output transport. They do not prove
DAC output or what a listener heard. `TtsPlayout` (`jasper/tts_playout.py`)
estimates a drain deadline from accepted sample duration plus
`Config.tts_drain_tail_sec`
(`JASPER_TTS_DRAIN_TAIL_SEC`). `expected_drain_at()` returns that deadline;
`wait_drained()` waits for it. `play_responses()` and `idle_watchdog()` in
[`jasper/voice/turn_playback.py`](../jasper/voice/turn_playback.py) share that
one anchor to end a turn: the consumer awaits `wait_drained()` after its final
write while the watchdog polls `expected_drain_at()` cooperatively, so
whichever observes "drained" first completes the turn. The watchdog's separate
no-response and stalled-response reaper is `Config.idle_timeout_sec`
(`JASPER_IDLE_TIMEOUT_SEC`), measured from the turn's last activity.

On interruption, `flush()` sends `FLUSH_SYNC` and resets the drain clock
only after a valid acknowledgement. A missing or invalid acknowledgement
leaves the clock intact. Provider truncation uses the acknowledgement's
per-item ledger: assistant `drained_frames` are summed by `provider_item_id`
and converted at 48 kHz. Completed items can be absent; a turn-wide maximum
is not a substitute for an item's boundary.

Fan-in's ledger counts mix commits; outputd estimates drain. Both are
software evidence. Acoustic completion needs a microphone measurement.
The voice daemon's `drain wait` log measures its turn-close delay from the
last server activity, not acoustic latency.

## Operational notes

**Test the music chain** (volume-controlled): `aplay -D correction_substream
file.wav`. It goes through CamillaDSP, so `main_volume` applies. The product's
own measurement spawns resolve the same device through
`jasper.audio_measurement.correction_lane.correction_play_device`.

**Test the TTS chain**: use `jasper-voice`/cue playback or the canonical
local TTS socket, `/run/jasper-fanin/tts.sock`.

On Apple-dongle installs, the dongle `Headphone` control is pinned at 100% by
`jasper-dac-init`, watched by `jasper-headphone-monitor`, and checked by
`jasper-doctor`. Those units are enabled only when
`jasper-audio-hardware-reconcile` recognizes the selected final-output DAC as
the Apple USB-C dongle; DAC8x and unknown-output states disable them. The
reconciler runs at install/boot and from udev `controlC*` events, so DAC
changes converge without a deploy-only scan, and `outputd_dac` follows the
detected single-device final-output card. The same pass writes
`/run/jasper-output-hardware/output_hardware.json`, the observed state `/state`
exposes as `audio.output_hardware` and `/sound/output-topology` uses to seed an
unsaved draft when no topology is saved — so two Apple adapters can appear as
an observed four-output shape without outputd having switched to a dual-sink
graph.

**Adding a DAC**: add one `DacProfile` row to `jasper/audio_hardware/dac.py`.
That module owns detection (ALSA card label, HAT EEPROM product, or the
wizard's I2S HAT toggle), each row's `final_edge_format` and `latency_floor`,
and the lookup functions the wizard, classifier and boot-config writer resolve
rows through — so a new row needs no change outside it (ADR-0234). Its row
comments are the registry's own documentation; read them there.

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
- Chip AEC uses outputd's final speaker buffer as the XVF USB-IN reference,
  downsampled to the chip's 16 kHz playback contract. The profile and
  reconciler own production activation; the wake-corpus recorder owns its
  temporary comparison overlay. The bridge's UDP reference remains at
  outputd's 48 kHz graph rate. See the
  [microphone reference](../jasper/mics/README.md) for chip beam-plan support.
