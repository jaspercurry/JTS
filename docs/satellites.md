# Satellite devices

Home base for the family of optional ESP32-based devices that surround
the Pi-based JTS speaker. Each satellite extends the speaker with one
or more of:

1. **Physical controls** — a knob, button, or touch surface for volume,
   transport, and hold-to-talk.
2. **Distributed microphones** — a mic placed across the room (or in
   another room) that's geometrically better positioned than the
   speaker's built-in array, and isn't sitting next to the loudspeaker.
3. **Auxiliary displays** — small screens that can show now-playing
   metadata, time, weather, listening state, or anything else.

Per-device firmware lives under [`firmware/`](../firmware) and per-device
deployment scripts live under [`jasper/cli/`](../jasper/cli). This doc
owns the cross-cutting concerns: shared protocols, the multi-mic
arbitration question, design rationale, roadmap. **Update this file as
the satellite story evolves** — it's intended to stay current as the
canonical reference.

---

## Why satellites at all

The Pi-as-monolith has three structural problems that satellite devices
address well:

**The speaker is a bad place for a microphone.** The chip mic is sitting
inside the same physical box as a loudspeaker that can be 20–40 dB
louder than the user's voice when music plays. Hardware AEC didn't work
for this topology (see [HANDOFF-aec.md](HANDOFF-aec.md)); software AEC
delivers modest attenuation at meaningful RAM cost. A mic placed 6 feet
*away* from the speaker — even an inferior single-MEMS — has a much
better signal-to-echo ratio just from inverse-square law geometry, plus
it's likely closer to where the user actually is. **A second mic in
another room may also obviate AEC entirely** when music is playing
through the main speaker — the satellite hears the user clearly while
the chip mic struggles.

**One mic, one room.** A single device works in the room it lives in.
JTS lives in a kitchen-living-room; the moment you walk into a bedroom
and want to ask for the next track, you're shouting across the
apartment. Satellite mics extend the listening surface to other rooms.

**Physical controls beat voice for trivial actions.** "Turn it down a
notch" is faster as a knob twist than as a wake-word + utterance + LLM
round-trip. Same for play/pause. The dial (already deployed for volume,
firmware-ready for transport + hold-to-talk) is the existing answer.
The AMOLED satellite extends it with a touchscreen and a mic.

The third pillar — **displays** — is mostly future work. The dial has
LVGL scenes for now-playing/clock/volume/listening/speaking but they're
in iteration. The AMOLED satellite's 368×448 screen is meaningfully
larger and could host richer UI (album art, lyrics, weather glance).
Useful but not urgent — we ship satellites for control and microphones
first, displays later.

---

## Devices in the family

### Jasper Dial — CrowPanel 1.28" HMI ESP32 Rotary Display

**Status:** Phases 1–3 working end-to-end on hardware — volume
control with a procedural LVGL volume gauge, play/pause on short-press,
hold-to-talk Gemini session on long-press. Phase 5 LVGL is partial:
the procedural LVGL gauge is pending on-device validation; the other
scenes (clock face, listening orb, speaking waveform, now-playing card)
have firmware scaffold ([commit `493bf60`](https://github.com/jaspercurry/JTS/commit/493bf60))
but aren't yet validated on-device.

**Hardware:** ESP32-S3R8 (8 MB OPI PSRAM, 16 MB flash), GC9A01 240×240
round IPS display over SPI, CST816D capacitive touch (unused so far),
WS2812 5-LED ring, mechanical rotary encoder + push-switch, 5 exposed
GPIO pads. Native USB-CDC (no separate USB-UART chip).

**Firmware:** [`firmware/dial/`](../firmware/dial) — PlatformIO,
Arduino-ESP32. Provisioning over Improv-over-Serial; runtime
communication over HTTP to `jasper-control` on the Pi. LVGL scenes are
defined in [`firmware/dial/src/scenes.h`](../firmware/dial/src/scenes.h):
IDLE → analog clock; NOW_PLAYING → album art + title + artist;
VOLUME → transient arc + percent; LISTENING → soft pulsing orb during
hold-to-talk; SPEAKING → slow waveform circle while Gemini is producing
TTS. The volume scene auto-reverts ~2 s after the last detent.

**Onboarding:**
[`jasper-dial-onboard`](../jasper/cli/dial_onboard.py) reads the Pi's
current WiFi credentials via NetworkManager and pushes them to the dial
over USB-CDC Improv. ~30 s, end-to-end, no laptop or browser needed.
Re-run after a network change to push new creds.

**Phases (planned and shipped):**
- ✅ **Phase 1**: WiFi (Improv) + encoder volume → `POST /volume/adjust`,
  with procedural LVGL gauge pending on-device validation.
- ✅ **Phase 2**: button short-press → `POST /transport/toggle`.
  Working on hardware.
- ✅ **Phase 3**: button long-press → `POST /session/start`; release →
  `POST /session/end`. Hold-to-talk bypasses the wake word entirely.
  Working on hardware.
- 🔄 **Phase 5**: LVGL display polish — clock face, listening orb
  (during hold-to-talk session), speaking waveform (during Gemini
  TTS), now-playing card with album art. Scaffold landed; not yet
  on-device validated. The volume gauge piece of phase 5 is procedural
  LVGL, pending on-device validation (see Phase 1).
- 🔮 **Phase 6**: time zone from the Pi's environment so dials shipped
  to other time zones don't need a re-flash.

The dial is **not** a microphone-bearing satellite — it has no mic.
It's the canonical reference for the "physical control" pillar, and
its onboarding/discovery/control patterns are reused by every satellite
that follows.

### Jasper AMOLED Satellite — Waveshare ESP32-S3-Touch-AMOLED-1.8

**Status:** Phase 0 (mic capture, 2026-05-08) and Phase 1.1 (WiFi +
Improv-over-Serial provisioning) shipped. Phase 1.2 (on-screen
connection-status indicator on the SH8601 AMOLED) shipped 2026-05-08
pending hardware validation — colored circle + text label drawn
directly with Arduino_GFX (no LVGL yet), redraws on `Status` enum
transitions, comes up within ~100 ms of power-on so the user sees
"Awaiting WiFi" before the WiFi join even begins. See
["Roadmap"](#roadmap) for the full phase list.

**Hardware:** ESP32-S3 (8 MB PSRAM, 16 MB flash), 1.8" 368×448 AMOLED
(SH8601, QSPI), FT3168 capacitive touch, **ES8311 I²S codec with
analog mic + speaker** (this is the audio interface that matters),
QMI8658 6-axis IMU, AXP2101 PMIC + Li-ion connector, USB-C, WiFi 2.4
GHz b/g/n + BLE 5, 7 exposed GPIO pads, BOOT + PWR buttons.
Vendor docs: [docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8).

**Why this device.** It has all three pillars in one box: a touchscreen
for control + display, a mic for distributed voice input, and 8 MB
PSRAM with room for microWakeWord + LVGL + WiFi audio buffers. It's
also battery-powerable (Li-ion connector + AXP2101 PMIC + USB-C
charging), which makes "pick it up and carry it to another room" a
real option, though battery life with always-on WiFi audio is going
to be measured in hours, not days.

**Mic caveats** — important to flag up front. Single MEMS mic into a
stereo codec ADC. No on-chip beamforming, no AGC, no AEC. The Pi-side
chip mic is a 4-mic XMOS array with all of the above; **this is a
different SNR regime**. Phase 0 is non-negotiable: capture audio
across a typical room and compare against the chip mic before
committing to deeper firmware work. If the SNR is dramatically worse,
the architecture (e.g. wake-on-Pi vs wake-on-device) shifts.

**Use cases this device targets:**
- Bedroom or kitchen mic that hears the user better than the
  living-room speaker can.
- Push-to-talk surface (touchscreen) for use cases where wake-word is
  inconvenient — e.g. a quick "next track" without saying the wake
  word out loud while someone else is on a call.
- Auxiliary display for now-playing / time / weather / room state.

**Factory firmware backup.** Before any firmware work, the
as-shipped 16 MB flash was read off via `esptool read-flash` and
saved in two places (laptop + Pi) for safekeeping. To restore the
device to factory state in the future:

```sh
sudo /opt/jasper/.venv/bin/python -m esptool \
    --chip esp32s3 --port /dev/ttyACM0 --baud 921600 \
    write_flash 0x0 \
    /home/pi/jts-firmware-backups/waveshare-amoled-1p8-factory-3CDC756E2F9C-20260508T170357Z.bin
```

SHA256 of the backup:
`6f9e8e3fc6d47b9396b903bfdb9d84e7bbfeabc7d8ec54e28d93d76d23210a11`.
The MAC `3C:DC:75:6E:2F:9C` in the filename identifies the
physical device — only restore an image to the device it was
read from. Backups are not committed to git (16 MB binary, not
our code).

---

## The microphone arbitration problem

> **Two related problems, two docs.** This section covers
> *multi-mic-around-one-Pi* arbitration — one Pi running the LLM
> session, satellite mics contributing audio. The
> *multi-Pi-on-a-LAN* case — N autonomous JTS speakers, each with
> its own mic and LLM session — is implemented and lives in
> [HANDOFF-peering.md](HANDOFF-peering.md). The two share
> signal-priority intuition (confidence > raw energy) but diverge
> in nearly every implementation dimension.

When two or more mics around the home hear "Hey Jarvis" at the same
time, **which one owns the resulting voice session?** This is the
hardest design question for a multi-satellite setup, and it deserves
real thought rather than a coin flip.

### What "winning" actually means

Concretely: only one mic's audio gets fed into the Gemini Live turn.
The others' audio is either dropped for the duration of the session,
fed in as a second channel (Gemini Live is mono), or routed somewhere
else entirely. The chosen mic also defines *where the user is* —
which has knock-on effects we may want later (reply through that
satellite's speaker; show the listening orb on that satellite's
screen).

Stakes:
- **Wrong winner → garbled audio.** If the kitchen mic wins but the
  user is in the bedroom, the resulting STT is bad and Gemini fails
  the request.
- **No winner → no session.** If both mics race and both think they
  won, you might double-trigger the spend cap, double-duck the music,
  etc.
- **Slow winner → laggy UX.** If arbitration takes 500 ms before audio
  starts flowing to Gemini, the user's first phoneme is gone.

### What a naive first instinct gets wrong

The intuitive answer is "whichever mic has the strongest signal on the
user's voice." That's *roughly* right but the literal interpretation —
compare audio RMS across mics — fails empirically:

> Amazon's published bandpass-filtered (1.5–6.5 kHz) energy argmax
> baseline is beaten by ~48% relative by their learned cross-device
> arbitrator, with reverberation (not noise) being the dominant
> failure mode.
> ([End-to-end Alexa Device Arbitration, ICASSP 2022](https://arxiv.org/abs/2112.04914))

Three reasons "loudest wins" doesn't work in practice:
1. **Reverberation in real rooms** flattens energy differences. Hard
   surfaces (kitchens, hallways) produce reflections that look like
   high energy at a far mic.
2. **Mic gain calibration mismatch.** Different mics (XMOS array vs
   ES8311 single MEMS vs whatever satellite ships next) have different
   dBFS-to-SPL curves. Raw RMS is not commensurable across them.
3. **Cross-device clock skew is large** (Amazon's training jitter
   σ=100 ms). Trying to align phase/STFT across mics over WiFi is a
   non-starter at room scale.

### What everyone else ships

#### Open-source: phrase-keyed timestamp cooldown

Home Assistant's Assist pipeline — the most-deployed open-source
arbitration mechanism — uses a **first-to-arrive timestamp race**, not
a quality comparison. When any satellite reports a wake event, HA
records the timestamp keyed by wake-word phrase; any other satellite
reporting the same phrase within `WAKE_WORD_COOLDOWN = 2` seconds gets
`DuplicateWakeUpDetectedError`.
Source:
[homeassistant/components/assist_pipeline/pipeline.py:801](https://github.com/home-assistant/core/blob/dev/homeassistant/components/assist_pipeline/pipeline.py)
and [const.py:16](https://github.com/home-assistant/core/blob/dev/homeassistant/components/assist_pipeline/const.py).

This is dumb, but it works because in practice, the satellite *closest
to the user* is also the one whose detector fires first (faster
internal pipeline path) and whose network packet arrives first
(slightly less USB / WiFi queuing latency). It's an implicit proxy —
not a correct one.

`wyoming-satellite` and `linux-voice-assistant` only do per-device
refractory; they delegate cross-device arbitration entirely to HA's
cooldown. Mycroft / OVOS sidesteps the problem — recommends *different*
wake words per device. **No open-source assistant compares confidence
scores across devices**, even though [openWakeWord scores are
comparable](https://github.com/dscripka/openWakeWord) across instances
of the same model. The ingredients are there; nobody's wired them up.

#### Commercial: confidence-score broadcast (Apple, Sonos), or learned embeddings (Amazon)

Per-device wake-detector confidence is the most commonly compared
signal in shipped commercial systems:

- **Sonos** ([US10181323B2](https://patents.google.com/patent/US10181323B2/en)):
  each NMD broadcasts "[a] measure of confidence of how well the
  wakeword was detected" plus a voice/wakeword identifier; the device
  with the largest confidence wins. Peer-to-peer or centralized.
- **Apple HomePod** ([support.apple.com/en-us/105077](https://support.apple.com/en-us/105077),
  [AU2016410253B2](https://patents.google.com/patent/AU2016410253B2/en)):
  peer-to-peer over Bluetooth, broadcast values include "a confidence
  value indicative of [a] likelihood that the audio input was provided
  by [a] particular user," plus device-state heuristics (recently
  raised, recently used). HomePod gets a soft priority over phones.
- **Amazon Echo Spatial Perception (ESP), cloud version** ([ICASSP 2022 paper](https://arxiv.org/abs/2112.04914)):
  each device sends a 2-second LFBE window through a small CNN to
  produce a 128-D embedding; cloud aggregates across all firing
  devices via a permutation-equivariant Deep-Sets-style classifier.
  Trained with σ=100 ms timing jitter to model lack of clock sync.
  This is the high-end approach — overkill for a home setup but
  reproducible if confidence-based arbitration ever falls down.
- **Google Home / Nest** ([US9812126B2](https://patents.google.com/patent/US9812126B2/en)):
  patent describes peer-to-peer over Wi-Fi Direct/SSDP using *rule-based*
  selection (primary device designation, recently used, sensor
  activity) — no signal comparison documented publicly. Likely uses
  signals too in production; just hasn't published.

### Proposed approach for JTS

Sonos/Apple-style **confidence-score broadcast with debounce**, with
device-state tie-breakers. Concretely:

1. Every mic source (chip mic, dial — eventually — , AMOLED satellite,
   future satellites) runs the same openWakeWord model and produces a
   per-frame score.
2. When any source crosses threshold, it sends a `WAKE` event to the
   Pi-side `WakeLoop` containing:
   - `source_id` (e.g. `chip`, `satellite-bedroom`)
   - `score` (the openWakeWord float that crossed threshold)
   - `frame_t_local` (ms-resolution local clock, for diagnostics only —
     not used for ordering)
3. `WakeLoop` opens a **debounce window** of ~200 ms after the first
   `WAKE` event. During that window, additional `WAKE` events from
   other sources are collected, not dispatched.
4. **Normalize each `score` by the source's gain calibration offset
   before comparison.** This is non-optional — see "Per-satellite gain
   calibration" gotcha below. Without it, cross-device comparison is
   meaningless.
5. At end of debounce: pick the winning source via tiebreakers in this
   priority order (first match wins):
   1. **Hard touch signal within last 5 s** — recent dial rotation,
      AMOLED touchscreen tap, or IMU "device was just picked up" event
      (Phase 4+). The user explicitly told us where they are.
   2. **DoA agreement** (XVF3800-class satellites only — the AMOLED
      satellite has a single MEMS and skips this rung). If two
      satellites both report a DoA pointing at the same physical zone,
      prefer the one with the user-facing beam.
   3. **Recently-used satellite** — if any source has been the active
      mic within the last 30 s, prefer it (Apple-style "session
      affinity"; reduces ping-pong when the user pauses mid-utterance).
   4. **Calibrated confidence margin > 0.10** — if the top source's
      score is more than 0.10 above the runner-up, pick it.
   5. **Else: chip mic** — when scores are within 0.10 of each other,
      default to the Pi's chip mic. It has chip-side beamforming + AGC
      and is the most-validated path; fall back here when the satellite
      data isn't decisive.
6. **Session affinity (negative broadcast):** once the winner is
   chosen, broadcast `{session_active: <source_id>}` to every other
   source for the duration of the session plus 2 s after end. Peers
   suppress their own VAD/wake-fire during that window. Prevents the
   "user shifts position mid-utterance and a different mic captures
   the second half" failure mode.
7. Open the Gemini turn, route audio from the winning source for the
   duration of the session. Other sources' audio is dropped until
   session end.

**Reference-side wake suppression** (orthogonal to the above, runs
continuously on the Pi): an openWakeWord instance reads from the
post-CamillaDSP playback signal. When *that* detector fires — i.e. the
music or TV is producing "Hey Jarvis"-shaped phonemes — the Pi
broadcasts `{suppress_wakes_for_ms: 200}` to every satellite. Filters
song-lyric and TV-dialog false fires across the entire constellation
in one place rather than relying on each satellite's per-mic threshold.
Cost: ~5% of one Pi 5 core, well within budget.

**Why this and not something fancier:**
- Open-source has *nothing* in this space, so we don't lose
  interoperability by inventing.
- Commercial published evidence (Sonos, Apple, Amazon's "loudest is
  weak" baseline) all converge on confidence > raw energy.
- openWakeWord scores are already comparable across devices running
  the same model — we don't need to train anything.
- Debounce is small (~200 ms) and only adds latency to the *second*
  mic to fire. The first mic's audio is buffered; we can replay
  pre-roll from whichever wins, the same way the existing daemon
  replays pre-roll on a single-mic wake ([voice_daemon.py:1061](../jasper/voice_daemon.py:1061)).
- Heterogeneous mic gain doesn't break it — confidence is a learned
  invariant against gain.

**Known gotchas, in order of likelihood:**
- **Per-satellite gain calibration is REQUIRED, not optional.**
  openWakeWord scores claim to be a learned invariant against gain,
  but in practice the dBFS-to-SPL curve still varies across mic
  hardware (XMOS array vs. ES8311 single MEMS vs. whatever ships next)
  enough that raw confidence comparison gives ~50/50 winners. The
  one-time calibration: play a known pink-noise burst through the JTS
  speaker at known SPL, the satellite measures RMS dBFS at its mic
  input, the Pi stores the per-source offset. All future scores are
  normalized by that offset before arbitration. This is a **must-have
  before multi-source arbitration ships** — otherwise the arbitrator
  is effectively a coin flip across heterogeneous mics.
- **Same-room satellite hears its own speaker bleed.** A satellite in
  the same room as the main speaker will pick up TTS / music
  reflections. Either keep satellites in *different* rooms from the
  speaker (the actual goal), or per-satellite VAD/refractory while the
  main speaker is producing audio.
- **WiFi queuing variance** can delay a satellite's `WAKE` event past
  the 200 ms debounce. Tune the window empirically; 200 ms is a
  starting guess. Amazon trains with σ=100 ms timing jitter; we should
  expect 100–300 ms in a normal home.
- **False fires correlated across devices** — if a TV phoneme triggers
  both mics, both will report high confidence. Arbitration assumes
  the wake event is real; doesn't filter false fires. Existing
  per-source openWakeWord threshold is the only filter today.
- **Device-state tie-breakers are mutable.** "Currently playing music"
  changes second-by-second; "recently used" changes per-utterance. The
  rule has to be evaluated *at arbitration time*, not cached.
- **Single-mic mode must remain the default.** When only the chip mic
  is configured, none of this code path runs — the existing
  `MicCapture` → `WakeLoop` flow stays unchanged. Multi-source is a
  superset.

### What we explicitly are not doing (yet)

- **No TDOA / cross-mic phase analysis.** Amazon explicitly tested
  multi-mic phase and found it doesn't help for range — only for DOA,
  which we don't need.
- **No learned arbitration model.** The Amazon ICASSP architecture is
  reproducible from the paper alone, but it's a lot of training data
  for a marginal gain over confidence-broadcast in a 2–4 mic home setup.
- **No cloud-side arbitration.** All arbitration runs on the Pi over
  LAN; no network dependency.

---

## Shared satellite architecture

All satellites talk to the Pi via the same surfaces. **New satellites
should reuse these patterns rather than inventing new ones.**

### Toolchain — Arduino-ESP32 v3.x via pioarduino

Both ESP32 firmware projects (`firmware/dial/` and
`firmware/satellite-amoled/`) pin to
[pioarduino/platform-espressif32 @ 55.03.38-1](https://github.com/pioarduino/platform-espressif32/releases/tag/55.03.38-1)
(Arduino-ESP32 v3.3.8 on ESP-IDF v5.5.4). PlatformIO's stock
`espressif32@^6.x` is held back at Arduino-ESP32 v2.x and no longer
tracks upstream — most notably it lacks `esp32-hal-periman.h`, which
is required by current `Arduino_GFX` (and therefore the SH8601 driver
the AMOLED satellite uses). pioarduino is the maintained fork that
follows espressif/arduino-esp32 master. Keeping both projects on the
same platform release avoids a split toolchain across `firmware/`.

A few v2.x → v3.x deltas matter for new satellite code:
- `MDNS.IP(idx)` was renamed to `MDNS.address(idx)`. `MDNS.port(idx)`
  is unchanged.
- The LEDC PWM API moved from channel-keyed (`ledcSetup` +
  `ledcAttachPin` + `ledcWrite(channel, val)`) to pin-keyed
  (`ledcAttach(pin, freq, res)` + `ledcWrite(pin, val)`).
- The legacy `<driver/i2s.h>` API survives as a deprecated
  compatibility shim — the satellite's audio path uses it intentionally;
  migrating to `<driver/i2s_std.h>` has a PSRAM/GDMA gotcha (see
  "Audio init footguns" below).

Local PlatformIO setup needs Python ≥ 3.10 (pioarduino requires it).
On macOS: `brew install python@3.11` then make a venv with that
Python, `pip install platformio`, and prefix `pio` invocations with
`PATH="/opt/homebrew/bin:$PATH"` so PIO's subprocess can find git
(needed for the Improv-WiFi library install). On the Pi, install PIO
only when you are doing accessory firmware work:
`sudo /opt/jasper/.venv/bin/pip install platformio`.

Normal speaker installs copy `firmware/` into `/opt/jasper/firmware/`
but do **not** build ESP32 binaries by default. Satellites are optional
accessories, and first-run PlatformIO setup is a large
accessory-specific download. When you intentionally want install-time
rebuilds on a Pi, set `JASPER_BUILD_OPTIONAL_FIRMWARE=1` for
`deploy/install.sh`. For normal maintainer verification, run the local
explicit check instead:

```sh
scripts/check-firmware-builds.sh              # dial + AMOLED
scripts/check-firmware-builds.sh dial         # just the rotary dial
```

This check is deliberately not part of always-on PR CI yet; it is the
right thing to run when touching `firmware/`, `platformio.ini`, or the
accessory onboarding flow.

### Discovery — `_jasper-control._tcp` over mDNS-SD

The Pi advertises `_jasper-control._tcp` on port 8780 via avahi
([deploy/avahi/jasper-control.service](../deploy/avahi/jasper-control.service)).
Satellites do `MDNS.queryService("jasper-control", "tcp")` at every
WiFi-up to find whichever Pi is on the network — **so the Pi's hostname
or IP can change without re-flashing satellite firmware.** Reference
implementation in [`firmware/dial/src/discovery.cpp`](../firmware/dial/src/discovery.cpp).
Fall back to a compile-time `JASPER_HOST` if mDNS doesn't resolve.

### Provisioning — Improv-over-Serial

WiFi credentials are pushed over USB-CDC using the
[Improv-over-Serial](https://www.improv-wifi.com/serial/) protocol.
Satellite firmware uses [jnthas/Improv-WiFi-Library](https://github.com/jnthas/Improv-WiFi-Library);
Pi-side, [`jasper.cli._improv`](../jasper/cli/_improv.py) owns the
wire framing/credential push and
[`jasper.cli._esp32_onboard`](../jasper/cli/_esp32_onboard.py) owns
the shared USB probe, flash, WiFi credential, and mDNS flow.
[`jasper-dial-onboard`](../jasper/cli/dial_onboard.py) and
[`jasper-satellite-onboard`](../jasper/cli/satellite_onboard.py) are
thin `DeviceProfile` wrappers with device-specific USB signatures,
firmware paths, boot-log signatures, mDNS names, and done-message copy.
For new ESP32 satellites, add another profile/shim rather than cloning
the flow.

### Control plane — HTTP `:8780` and the voice control socket

Satellites POST control actions to `jasper-control` on the Pi
([`jasper/control/server.py`](../jasper/control/server.py)). Routes:

- `GET  /healthz` — liveness
- `GET  /volume` — current canonical listening level
- `POST /volume/adjust` — body `{"delta_percent": int}` or legacy
  `{"delta_db": float}` (50 dB scale)
- `POST /volume/set` — body `{"percent": int}` or legacy `{"db": float}`
- `POST /transport/toggle` — auto play↔pause based on backend state
- `POST /session/start` — manual wake bypass (long-press / push-to-talk)
- `POST /session/end` — finalize input
- `POST /cue/play` — body `{"slug": "<cue-slug>"}` — play a registered
  audio cue through the daemon's gain-tracked TtsPlayout
- `GET  /dial/status` — heartbeat snapshot for `jasper-doctor`

`session/*` and `cue/play` proxy through to the voice daemon's UDS at
`/run/jasper/voice.sock` ([voice_daemon.py:1167](../jasper/voice_daemon.py:1167))
so that satellites don't need to know the daemon's internal IPC.
**Unauthenticated** — home LAN trust posture, same as the dial.

### Diagnostics — UDP `:5514`

Satellites fire diagnostic log lines as one-line UTF-8 datagrams to
port 5514. Listener is in
[`jasper/control/server.py:558`](../jasper/control/server.py:558);
log lines are re-emitted as `journalctl -u jasper-control` records
tagged with the satellite's IP. **Fire-and-forget — UDP loss is
acceptable.** This pattern lets satellites debug even when their HTTP
control plane is broken, and removes the need to tether USB.

### Status / UI conventions

The dial established a six-state LED/status model
([main.cpp:48](../firmware/dial/src/main.cpp:48)). New satellites should
mirror this on whatever indicator hardware they have:

| State | Color / cue | Meaning |
|---|---|---|
| BOOT | magenta solid | Power on, before any setup |
| PROVISION | yellow blink | No WiFi creds; awaiting Improv push |
| CONNECTING | yellow solid | Joining WiFi with stored creds |
| ONLINE | dim green | WiFi up, Pi reachable |
| HTTP_ERROR | red blink | WiFi up but `jasper-control` POST failed |
| OFFLINE | red solid | WiFi dropped; reconnecting |

For satellites with displays, the dial's
[scenes.h](../firmware/dial/src/scenes.h) state model — IDLE,
NOW_PLAYING, VOLUME (transient), LISTENING, SPEAKING — is the reference
LVGL graph. Reuse where possible; don't invent parallel state machines.

---

## Audio path for satellite mics — design proposal

**Status: not yet implemented.** This section is the working design;
update it as code lands.

### Wire format

- **Codec:** raw PCM, 16 kHz int16 mono — same shape `MicCapture`
  produces today ([audio_io.py:28](../jasper/audio_io.py:28)).
- **Frame size:** 1280 samples (80 ms) — matches openWakeWord's expected
  frame size and the existing `MicCapture.OUTPUT_FRAME_SAMPLES`.
- **Transport:** UDP datagrams to a fixed Pi-side port, one frame per
  datagram, no framing protocol beyond the implicit length. Bandwidth:
  ~32 KB/s per satellite — trivial on home WiFi. **Lossy on packet
  drop is acceptable** for the same reason it's acceptable for the
  diagnostic UDP log: voice tolerates the occasional lost frame and
  the alternative (TCP head-of-line blocking) hurts latency more than
  packet loss hurts intelligibility.
- **Headers:** prepend a small fixed header containing `source_id`
  (uint32) and `frame_seq` (uint32). `source_id` lets the Pi identify
  which satellite is talking; `frame_seq` lets the Pi detect drops
  for diagnostic purposes.

### Pi-side integration

The voice daemon currently has a single audio source bound at startup
([voice_daemon.py:1367](../jasper/voice_daemon.py:1367)). Multi-source
support requires extending `WakeLoop` to accept N sources and run
arbitration as described above. Sketch:

1. New `NetworkMicSource` class implementing the same async-iterable
   frame interface as `MicCapture`. Listens on a UDP socket; demultiplexes
   by `source_id`; produces 80 ms frames per source.
2. `WakeLoop.__init__` accepts `sources: list[MicSource]` instead of a
   single `mic`. Each source has its own `WakeWordDetector` instance
   (cheap — the model is a few hundred KB).
3. Wake-loop main task fans out frames per source. When any detector
   fires, opens a debounce window (default 200 ms) and collects
   `(source_id, score)` tuples from any other detector that fires in
   the window.
4. At end of debounce: pick max-score with device-state tie-breakers
   (see ["Proposed approach"](#proposed-approach-for-jts)).
5. `_begin_turn` uses the pre-roll buffer **of the winning source**.
   Every source maintains its own pre-roll ring — cheap (a few seconds
   of int16 mono per source).

### Satellite-side options

- **Wake-on-Pi (always streaming):** satellite always sends frames; Pi
  runs detector. Simplest firmware. Always-on WiFi → battery hungry,
  not viable on Li-ion. Use this for AC-powered satellites.
- **Wake-on-device + push:** satellite runs microWakeWord locally,
  only opens the audio stream after wake fires. Ports an INT8 TFLite
  Inception model ([microWakeWord, kahrendt/microWakeWord](https://github.com/kahrendt/microWakeWord))
  — "hey_jarvis" is one of the pretrained models. Battery-friendly.
  More firmware work.
- **Push-to-talk only:** no wake at all. Touch a button → POST
  `/session/start` → stream during the press → release → POST
  `/session/end`. Cheapest path, lowest battery.

The AMOLED satellite plan starts with push-to-talk (validates the
audio plumbing end-to-end with minimum risk), then adds always-streaming
mode as a settings toggle, then optionally adds on-device wake for
battery operation. Per-device roadmap below.

---

## Roadmap

### Jasper Dial

| Phase | Description | Status |
|---|---|---|
| 1 | WiFi (Improv) + encoder volume + procedural LVGL volume gauge | ✅ volume control working on hardware; gauge pending on-device validation |
| 2 | Button short-press → transport toggle | ✅ working on hardware |
| 3 | Button long-press → hold-to-talk Gemini session | ✅ working on hardware |
| 5 | Remaining LVGL scenes — clock / listening orb / speaking waveform / now-playing card | 🔄 firmware scaffold present, not yet on-device validated |
| 6 | Time zone from Pi (no per-region re-flash) | 🔮 future |

### Jasper AMOLED Satellite

| Phase | Description | Status |
|---|---|---|
| 0 | **Mic characterization.** Built an end-to-end PlatformIO firmware (boot + I²C scan + ES8311 init + I²S RX + USB-CDC PCM stream) and validated it captures clean 16 kHz mono audio across a typical room. **PASSED 2026-05-08** — music plays back recognizably, voice is clear; capture WAVs in `captures/`. Took 5 firmware iterations to get past two non-obvious bugs (see "Hardware gotchas" below). | ✅ done |
| 1.1 | **WiFi + Improv-over-Serial provisioning.** Cred storage in NVS, mDNS-SD discovery of `_jasper-control._tcp`, dlog over USB-CDC + UDP :5514, watchdog reconnect. Mirrors `firmware/dial/`'s skeleton. | ✅ shipped 2026-05-08 |
| 1.2 | **On-screen connection-status indicator.** SH8601 AMOLED (368×448) over QSPI driven directly with Arduino_GFX (no LVGL yet — direct draws). Colored circle + label keyed off the `Status` enum, redrawn only on transitions. Comes up before WiFi join so the user sees a "Boot" / "Awaiting WiFi" frame within ~100 ms of power-on. | ✅ shipped 2026-05-08, pending on-device validation |
| 1.3+ | **Push-to-talk + audio + measurement gate.** Capacitive touch driver (FT3168), LVGL "Tap to Talk" surface, control-plane HTTP POSTs (`/session/start`, `/session/end`), I²S mic capture gated on touch, UDP audio stream to a new Pi-side endpoint. Includes the FRR/WER measurement pass (see ["Validation methodology"](#validation-methodology--measurement-driven-phase-13-gate) below) that gates whether the constellation buildout continues here or pivots to one of the [Phase 2 fallbacks](#phase-2-fallback-architectures). | ⬜ not started |
| 2 | **Always-streaming "second mic" mode + multi-source `WakeLoop`.** Settings toggle on the AMOLED. Satellite streams continuously when enabled. Pi gains a second `MicSource` and runs openWakeWord on the satellite stream as a parallel source. **This is where multi-mic arbitration lands** (per-satellite gain calibration, debounce, tiebreakers, negative broadcast, reference-side wake suppression — see "Proposed approach for JTS" above). | ⬜ not started — depends on Phase 1.3+ |
| 3 | **On-device wake (microWakeWord).** Port the "hey_jarvis" pretrained microWakeWord model onto the satellite (matches HA Voice PE precedent — better-supported pretrained "hey_jarvis" than ESP-SR's WakeNet). Only stream after wake fires locally. Required only if battery operation is desired; AC-powered satellite is fine on Phase 2. | ⬜ not started — depends on Phase 2 |
| 4 | **Display polish.** Now-playing card with album art (368×448 has room for a real art tile), clock, weather glance, listening orb mirroring the dial's. Software rotation in PSRAM if portrait orientation matters (SH8601 has no hardware 90° rotation). | 🔮 future — independent of audio phases |

### Hardware gotchas (learned during Phase 0)

Verified pin map for the AMOLED-1.8 (cross-checked between vthinkxie's
board header and an HA-community ESPHome YAML, then confirmed by I²C
scan + I²S audio capture on actual hardware):

| Signal | GPIO |
|---|---|
| I²C SDA / SCL (shared bus, 200 kHz) | 15 / 14 |
| I²S MCLK | 16 (but see below — not used in the working config) |
| I²S BCLK | 9 |
| I²S LRCK / WS | 45 |
| I²S DIN (mic, ESP RX) | 10 |
| I²S DOUT (speaker, ESP TX) | 8 |
| Speaker amp enable (active-high) | 46 |
| BOOT button (active-low) | 0 |

I²C addresses observed: ES8311 0x18, TCA9554 0x20, AXP2101 0x34,
FT3168 0x38 (only after release from TCA9554 P1 reset), PCF85063
0x51, QMI8658 0x6B.

**Audio init footguns — all three matter:**

1. **Use the legacy `<driver/i2s.h>` API, NOT `i2s_std.h`.**
   `i2s_std`'s DMA descriptor allocator can land descriptors in PSRAM
   when the build has `qio_opi` PSRAM mode (which we need for the
   AMOLED), triggering a GDMA "user context not in internal RAM"
   failure. Legacy driver pins descriptors in internal SRAM via
   `MALLOC_CAP_DMA`. Documented in vthinkxie's `audio.cpp` comments.

2. **I²S RX must be configured as `I2S_CHANNEL_FMT_RIGHT_LEFT`
   (stereo), even though the codec is mono — discard the right
   channel in software.** With `ONLY_LEFT`, the legacy driver's BCLK
   timing math doesn't produce an integer number of BCLK ticks per
   half-LRCK frame for our (4.096 MHz MCLK, 16 kHz LRCK, 16-bit)
   config. Result: ESP32 samples on misaligned BCLK edges and you
   get bit-quantized-sounding audio (intelligible but ~13-bit). Fix:
   match Espressif's official `i2s_es8311` example, which uses
   stereo. See [esp-idf/examples/peripherals/i2s/i2s_codec/i2s_es8311](https://github.com/espressif/esp-idf/tree/master/examples/peripherals/i2s/i2s_codec/i2s_es8311)
   and [IDF issue #10630](https://github.com/espressif/esp-idf/issues/10630).

3. **When `REG01 = 0xBF` (SCLK-derived MCLK), `REG02 pre_multi`
   MUST be 3 (= 8× multiplier).** REG01 = 0x30 (MCLK from MCLK pin)
   appears not to work on this board — the codec produces constant
   samples, suggesting the MCLK trace from ESP32 GPIO16 isn't
   actually wired through to the codec on the AMOLED-1.8. The
   working config is REG01 = 0xBF (codec derives MCLK from BCLK
   internally via PLL). When in that mode, the codec's internal
   DIG_MCLK = BCLK × pre_multi. Our 16 kHz config requires DIG_MCLK
   ≈ 4.096 MHz, BCLK = 512 kHz, so pre_multi = 8 (= datmp 3 in REG02
   bits 4:3 = 0x18). **Without this, the ADC samples at 1/8 the
   expected rate and outputs each sample held ~8× into the I²S
   frame** — produces an unmistakable "pixel-y / sample-and-held"
   bitcrushed sound. Espressif's `es8311_config_sample()` overrides
   `datmp = 3` for this case; our inline init must do the same.

4. **PGA gain (REG16) is NOT a simple 8-value enum** despite what
   the `es8311_mic_gain_t` enum implies. The register is a 6-bit
   field (writes >0x3F mask off bits 7:6). 0x3F mutes the codec
   (probably a control flag bit). 0x37 is hot enough to clip music.
   0x32 ≈ 0x37 - 5 dB gives a safe peak around -5 dBFS for music
   while keeping speech well above ambient at room distance. Each
   register step ≈ 0.95 dB.

5. **ADC needs ~100 ms to settle after analog power-up** before it
   produces stable PCM. The first ~1k–2k samples are at the chip's
   quiescent value (we observed `-7` LSB constant). Add a delay +
   drain a few I²S DMA buffers before declaring `[stream-start]`,
   or the host's first samples will be silence.

6. **`Serial.println()` emits `\r\n`, not `\n`.** Capture scripts
   on the Pi side must search for the bare marker `"[stream-start]"`
   (no newline in the search) and skip past whatever line ending
   follows.

**Display init footguns (learned during Phase 1.2):**

7. **SH8601 reset is on the TCA9554 expander (P0), not a direct
   GPIO.** Arduino_GFX's `Arduino_SH8601` defaults to driving reset
   over a pin you specify. Our reset line is behind the I²C expander
   at 0x20, so we pass `GFX_NOT_DEFINED` to the constructor and toggle
   reset over I²C ourselves before calling `gfx->begin()` —
   sequence: assert reset (P0 low) for 20 ms, release (P0 high) for
   20 ms. Without the external toggle, the panel never wakes from
   reset and `begin()` reports success but the screen stays black.

8. **DSI_PWR_EN (TCA9554 P2) controls the panel's power rail.**
   It must be driven HIGH before reset is released, otherwise the
   SH8601 has no power to come up with. ~5 ms settle between
   power-enable and reset-release is sufficient.

9. **Use `Arduino_SH8601 *` not `Arduino_GFX *` if you need
   `setBrightness()`.** The brightness API is on the `Arduino_OLED`
   subclass, not the GFX core. A pointer typed as `Arduino_GFX *` will
   compile-error on `setBrightness()` even though the underlying
   object supports it. Drawing primitives (`fillScreen`, `fillCircle`,
   `print`) are inherited from `Arduino_GFX` so the more-specific
   pointer type costs nothing.

10. **Arduino_GFX has no `BLACK`/`WHITE` macro defines.** Other GFX
    libraries (Adafruit_GFX, TFT_eSPI) define color constants;
    moononournation's library does not. Use raw RGB565 hex values
    (`0x0000`, `0xFFFF`, `0xF800`, `0x07E0`, `0x001F`) or define your
    own constants at the top of your display module.

### Cross-cutting

- **Multi-source `WakeLoop` refactor.** Current code assumes one mic.
  Phase 2 above is the trigger; the refactor itself is the long pole
  on the Pi side.
- **Per-source pre-roll buffers.** Today there's one pre-roll
  ([voice_daemon.py:1061](../jasper/voice_daemon.py:1061)); multi-source
  needs one per mic so we replay the *winner's* pre-roll, not the
  union.
- **Onboarding CLI generalization.** Either fork
  [`jasper-dial-onboard`](../jasper/cli/dial_onboard.py) into
  per-satellite CLIs, or generalize into `jasper-satellite-onboard`
  with a `--device` flag. Lean toward the latter once we have two
  device classes.

---

## Validation methodology — measurement-driven Phase 1.3 gate

**Before the Pi-side multi-source `WakeLoop` and UDP audio infra get
built (the "Phase 1.3+" roadmap row above), we run a measurement
pass on a single satellite to find out whether the architecture is
actually viable in this room with this hardware.** Anecdotal "it
seems to work" is not data. Architecture decisions need numbers.

### Why this gate exists

The AMOLED satellite's mic is a single MEMS into the ES8311 codec —
no on-chip beamforming, AGC, or AEC. The Pi-side chip mic is a 4-mic
XMOS array with all of the above. The bet is that **inverse-square
law geometry plus close-range placement (1-1.5 m to user) beats
far-but-good (3-4 m to chip mic)**. That's a measurable claim.

If the bare-mic-at-close-range numbers don't hit acceptable thresholds
even with `NO_INTERRUPTION` and ducking, none of the multi-source
infra adds value — we'd be routing worse audio into Gemini. The
cheap measurement up front saves weeks of building a Pi-side
arbitrator that can't actually arbitrate well.

### Pass/fail criteria

50 wake-word trials per cell, recorded as `(wake_fired, score,
post_wake_wer)`:

| Condition                              | FRR pass | post-wake WER pass |
|----------------------------------------|----------|--------------------|
| 1 m from user, quiet                   | < 5%     | < 10%              |
| 1 m from user, music at 65 dB SPL      | < 10%    | < 15%              |
| 1 m from user, music at 75 dB SPL      | < 25%    | < 25%              |
| 3 m from user, quiet                   | < 15%    | < 20%              |
| 3 m from user, music at 65 dB SPL      | < 30%    | < 30%              |

Decision tree:
- 1 m quiet AND 1 m music-65 both pass → architecture viable, proceed
  to multi-source `WakeLoop` + push-to-talk.
- 1 m cells fail FRR by < 10 percentage points → consider satellite-
  side AEC as a fallback (see "Phase 2 fallback architectures" below)
  before deciding.
- 1 m cells fail FRR by > 10 percentage points even with AEC
  enabled → fall back to Seeed XVF3800-with-XIAO satellites (see
  fallback architectures below).

### Test harness shape (Pi-side scaffolding)

Sketch — not yet built. Lives in `jasper/cli/satellite_phase1_3_*.py`
and `jasper/satellites/`:

- **Corpus generator**: pre-render a 50+-utterance "Hey Jarvis"
  corpus with Piper TTS using multiple voices and varied prosody.
  Cache to disk; regenerate only on `--regen-corpus`.
- **SPL calibration helper**: integrates with the user's UMIK-1/
  UMIK-2 + REW workflow (or a sounddevice-based recorder if
  simpler) to confirm playback levels at the listener position
  before each test condition.
- **Test runner**: plays the corpus through the JTS main speaker at
  calibrated SPL while the satellite under test is mounted at one
  of the test distances. Records wake events + confidence scores
  via the satellite's UDP/HTTP path. Runs Whisper or Gemini STT on
  the captured post-wake audio and computes WER vs. ground-truth.
- **Output**: `phase1_3/{date}/{condition}.csv` with columns
  `utterance_id, wake_fired, score, wake_latency_ms, wer,
  snr_estimate`. Plus `summary.md` with FRR / FAR / WER mean / p50
  / p90 per condition.

The PCM streaming infra needs to land first before this harness can
run end-to-end (the satellite has to be able to deliver post-wake
audio to the Pi). That makes the measurement pass an artifact of
the early Phase 1.3 work, not a prerequisite to starting it — but
the **decision to keep building the constellation** is gated on the
measurement results.

---

## Phase 2 fallback architectures

Two documented escape hatches if the bare-mic measurement at close
range doesn't hit thresholds. Both are real options; neither is
committed to upfront.

### Fallback A — satellite-side AEC via Snapcast reference

The architectural problem with running AEC at a satellite (mic) that
doesn't drive the main speaker (echo source): the reference signal
and the mic signal don't share a clock. ESP-SR's AEC pipeline
assumes a same-I²S-peripheral reference; without that, sample-rate
offset between the Pi's playback clock and the satellite's mic clock
diverges the adaptive filter within tens of seconds.

**[Snapcast](https://github.com/badaix/snapcast)** solves the clock
problem. It's a multi-room audio sync protocol that does adaptive
resampling on the client side to match the playback content into the
client's local clock domain. If the satellite runs
[`CarlosDerSeher/snapclient`](https://github.com/CarlosDerSeher/snapclient)
(ESP-IDF native ESP32 port) routing the audio into the same I²S
peripheral that drives the ES8311 DAC at near-zero codec volume, the
buffer the AEC consumes as a reference is now a clock-locked
sample-accurate replica of what the main speaker is emitting.

Pi-side adds: snapserver as a systemd unit, an snd-aloop tap on the
post-CamillaDSP signal feeding it. Satellite-side adds: snapclient
in firmware, ES8311 playback volume to ~0%, ESP-SR AFE config
switched from single-mic-no-AEC to single-mic-with-reference.

**Cost:** ~22% of one ESP32-S3 core for AEC + microWakeWord + WiFi
+ Snapcast resampler + app — fits but no slack. Significant new
infra (snapserver, snd-aloop tap, sync diagnostics, satellite-side
snapclient integration). Worth investigating only after measurement
shows mic-only is the bottleneck. The framework choice for ESP-SR is
cleaner under ESP-IDF than Arduino-ESP32, but ESP-SR can be consumed
from Arduino-ESP32 as a component — we don't need to migrate the
whole project to enable this.

### Fallback B — Seeed XVF3800-with-XIAO satellites

The "buy known-good hardware instead of debugging cheap hardware"
escape. Same XMOS chip family as the Pi-side mic, hardware DSP
(AEC + multi-adaptive beamforming + AGC + dereverb + DoA) on the
chip itself. No Snapcast, no satellite-side ESP-SR, no AEC tuning.
Reference integration:
[formatBCE/Respeaker-XVF3800-ESPHome-integration](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration).

Cost: ~$54 per satellite vs. ~$30 for the AMOLED. No display, but
DoA reporting becomes a real arbitration tiebreaker (see "Proposed
approach for JTS" step 5.2 above).

Caveat: needs custom XVF3800 firmware to act as I²S master because
ESPHome can't generate the 12.288 MHz MCLK on its own.

### When to fall back

Trigger fallback A or B if any of:
- Phase 1.3 FRR > 10 percentage points worse than pass criteria at
  1 m, even with fallback A's Snapcast-AEC enabled.
- Snapcast clock sync proves unreliable on the user's network
  (sustained sync error > 1 ms after a week of operation).
- WiFi airtime contention from a constellation creates noticeable
  latency spikes in JTS daily use.

The XVF3800 path is the proven path; the AMOLED-with-software-AEC
path is the experimental one. Don't fall back prematurely (the
measurement gate above should drive the decision), but don't be
precious about the experiment either if the data says it's not
delivering.

---

## Open questions

These are deliberately undecided. **Update this list as questions get
answered or as new ones surface.**

1. **TTS routing on a satellite-won session.** If the bedroom satellite
   wins, does TTS reply through the main speaker (current setup, with
   CamillaDSP loudness-anchor tracking) or through the satellite's
   onboard ES8311 speaker (room-local reply)? Most likely the main
   speaker, but room-local reply is interesting for "quiet response in
   one room only" use cases.
2. **Listening-orb routing.** When a satellite wins, should the dial's
   LISTENING scene also light up? Or only the winning satellite's
   screen? Both? This is a UX call, not a technical one.
3. **Authentication.** Today the control plane is unauthenticated (home
   LAN trust). If satellites ever leave the home LAN — guest network,
   another household — we need real auth. Probably a per-satellite
   shared secret signed into request headers. Defer until needed.
4. **Multiple satellites of the same kind.** "Bedroom AMOLED" + "office
   AMOLED" both running — does the device-state tie-breaker need a
   spatial hint (which room is the user actually in)? IMU-based
   "recently picked up" plus "currently being touched" is a Phase 2.5
   refinement, not a Phase 2 must-have.
5. **Audio cue routing on satellite wake-blockers.** When a satellite's
   wake fires but the spend cap is hit, today the Pi's audible cue
   (per [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md))
   plays through the main speaker. Should it instead play through the
   satellite that fired? Same UX question as #1.
6. **Debounce window tuning.** 200 ms is a starting guess. Need real
   measurements once two mics are running. The instrumentation —
   per-source `WAKE` event log with timestamp + score — should land
   with the multi-source refactor.
7. **What happens to the chip mic when AEC is enabled.** The opt-in
   software AEC bridge ([HANDOFF-aec.md](HANDOFF-aec.md)) feeds a
   cleaned chip-mic signal into `JASPER_MIC_DEVICE`. Multi-source
   arbitration treats the AEC'd chip mic as one source like any other
   — but its confidence scores will be different from raw chip-mic
   scores, which may bias arbitration. May need per-source score
   calibration. Defer until Phase 2 ships and we have measurements.

---

## References

- [End-to-end Alexa Device Arbitration (Amazon, ICASSP 2022)](https://arxiv.org/abs/2112.04914)
  — the strongest published prior art on multi-device wake arbitration.
- [Sonos US10181323B2 — Arbitration-based voice recognition](https://patents.google.com/patent/US10181323B2/en)
  — clearest published "wake-confidence broadcast" rule.
- [Apple AU2016410253B2 — Intelligent device arbitration and control](https://patents.google.com/patent/AU2016410253B2/en)
  — peer-to-peer broadcast over Bluetooth.
- [Home Assistant assist_pipeline.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/assist_pipeline/pipeline.py)
  — the most-deployed open-source mechanism (phrase-keyed timestamp
  cooldown), reference for what *not* to settle for.
- [microWakeWord (kahrendt)](https://github.com/kahrendt/microWakeWord)
  — INT8 TFLite Inception wake-word for ESP32-S3, "hey_jarvis"
  pretrained.
- [openWakeWord (dscripka)](https://github.com/dscripka/openWakeWord)
  — wake-word framework currently used Pi-side.
- [Improv-over-Serial protocol](https://www.improv-wifi.com/serial/) —
  satellite WiFi provisioning over USB-CDC.
- [ESP-SR (Espressif speech recognition)](https://github.com/espressif/esp-sr)
  — AFE pipeline (NS, AGC, AEC) for on-device pre-processing if the
  Snapcast-AEC fallback is needed.
- [Snapcast](https://github.com/badaix/snapcast) — multi-room audio
  sync; clock-locked reference for satellite-side AEC fallback.
- [CarlosDerSeher/snapclient](https://github.com/CarlosDerSeher/snapclient)
  — ESP-IDF native Snapcast client for ESP32-S3.
- [formatBCE/Respeaker-XVF3800-ESPHome-integration](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration)
  — reference integration for the Seeed XVF3800-with-XIAO fallback path.
