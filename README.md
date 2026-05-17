# JTS — Jasper smart speaker

A custom voice-controlled smart speaker on a Raspberry Pi 5 running
Raspberry Pi OS Lite Trixie, with
[CamillaDSP](https://github.com/HEnquist/camilladsp) for audio. The
voice loop is provider-agnostic: any of three real-time
speech-to-speech APIs can drive it via a single env-var switch —
[Gemini 3.1 Flash Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
(default), [OpenAI gpt-realtime-2](https://developers.openai.com/api/docs/guides/realtime),
or [xAI Grok Voice Agent](https://docs.x.ai/docs/guides/voice/agent).
This is a personal hobby project; not a product.

The pitch: a music streamer that's also a voice assistant, built
from open hardware and open audio software, with the LLM costing
roughly $1–3/month at light use on the cheapest provider.

---

## Hardware

| Component | Role |
|---|---|
| Raspberry Pi 5 (1GB or 2GB; **2GB recommended**) | Host |
| Apple USB-C → 3.5mm dongle | DAC for the speaker output (48 kHz, simple UAC2) |
| TPA3255 class-D amp + 32V supply | Speaker power |
| Speakers + speaker wire | (Whatever you have) |
| Seeed ReSpeaker XVF3800 (USB UA variant) | 4-mic array with on-chip XMOS DSP |
| ELECROW CrowPanel 1.28" HMI ESP32 Rotary Display (optional) | Wireless physical knob — volume, play/pause, hold-to-talk |
| Waveshare ESP32-S3-Touch-AMOLED-1.8 (optional) | Touchscreen + mic satellite — distributed mic, push-to-talk, aux display |

The optional ESP32 devices form a "satellite" family — see
[docs/satellites.md](docs/satellites.md) for the cross-cutting
design (shared protocols, multi-mic arbitration, roadmap per device).

The XVF3800's onboard 3.5mm jack / AIC3104 codec is **not**
connected — speakers go to the Apple dongle. This is the
non-standard part of the build and it's what drives a lot of the
AEC complexity (see § Acoustic echo cancellation below).

---

## Architecture

Audio path:

```
Phone (AirPlay / Spotify Connect / BT)
        │
        ▼
  shairport-sync (AirPlay 2)   librespot (Spotify Connect)
  bluealsa-aplay (BT A2DP)
        │
        │ each writes directly to hw:Loopback,0,0
        ▼
  hw:Loopback,0,sub0  ── snd-aloop ──  plughw:Loopback,1,0
                                              │
                                              ▼
                                    jasper-camilla (CamillaDSP, port 1234)
                                    - main_volume (the ducking knob)
                                    - flat passthrough today
                                              │
                                              ▼
                                    pcm.jasper_out (dmix on Apple dongle)
                                              │
                                              ▼
                                    Apple USB-C dongle → amp → speakers


  XVF3800 4-mic array  ── USB UAC2 ──  hw:CARD=Array,DEV=0
        │                                     │
        │                                     ▼
        │                            jasper-voice (wake-word, real-time LLM, tools)
        │                            - openWakeWord ("Jarvis" — community model,
        │                                also responds to "Hey Jarvis"; pickable
        │                                at http://jts.local/wake/)
        │                            - Silero VAD
        │                            - real-time LLM session (provider-agnostic):
        │                                Gemini Live | OpenAI Realtime | xAI Grok
        │                            - tool registry (volume, transport, Spotify, weather…)
        │                                     │
        │                                     ▼
        │                            TTS (provider-generated PCM, 24 kHz mono)
        │                                     │
        │                                     ▼
        │                            pcm.jasper_out (same dmix as music)
        │                                     │
        │                                     ▼
        └──── airborne echo back to mic ◄── speakers
```

`jasper-camilla` and `jasper-voice` both write to the same dongle
dmix (`pcm.jasper_out`); dmix sums their streams. Music ducks on
wake via a CamillaDSP `SetMainVolume` call (the `main_volume`
property, not the `master_gain` mixer — that mixer is identity)
over its websocket on port 1234.

> ### Important: two paths to the dongle
>
> Music goes **through** CamillaDSP. TTS goes **around** it; both sum at
> the dongle's dmix. `main_volume` only attenuates music — TTS matches
> user volume via a separate tracker (`TtsVolumeTracker`) that measures
> the actual music level downstream and scales TTS to match. To test the
> chain at a controlled volume, play to `plughw:Loopback,0,0` (the music
> input), not `jasper_out` (which bypasses the DSP). Why the split and
> what the tracker does: [`docs/audio-paths.md`](docs/audio-paths.md).

`jasper-mux` arbitrates between the three renderers — when a new
source transitions to playing while another is already active, it
pauses the older one so the user gets "latest source wins" UX.

There's also a reconciler-managed software AEC bridge
(`jasper-aec-bridge`) that taps the music chain via a
`pcm.jasper_capture` dsnoop, runs WebRTC AEC3 echo cancellation
against the chip's raw mic, and emits a cleaned-up mono signal over
UDP localhost for jasper-voice to consume. It runs automatically only
when the configured AEC mic is present with 6-channel firmware — see
§ below.

---

## Current status

`v1` (per [PLAN.md](PLAN.md)) is mostly landed:

- ✅ Music streaming (AirPlay 2, Spotify Connect, Bluetooth A2DP) via
  source-built shairport-sync + nqptp, librespot (rust, via raspotify
  .deb) with log volume curve, and bluez-alsa
- ✅ `jasper-mux` daemon for latest-source-wins preemption
- ✅ Always-on CamillaDSP with a passthrough `master_gain` mixer
- ✅ Wake-word detection — default is "Jarvis" (the
  [fwartner Home Assistant community model](https://github.com/fwartner/home-assistant-wakewords-collection)
  which also accepts "Hey Jarvis"); picker UI at
  http://jts.local/wake/ flips between Jarvis, Hey Jarvis, Alexa,
  Hey Mycroft. See [jasper/wake_models.py](jasper/wake_models.py)
  for the registry and the steps to add a new one.
- ✅ Gemini Live voice loop with tool calling
- ✅ Provider-agnostic voice abstraction — `JASPER_VOICE_PROVIDER`
  flips between Gemini Live, OpenAI Realtime (`gpt-realtime-2`), and
  xAI Grok Voice Agent. See
  [docs/HANDOFF-voice-providers.md](docs/HANDOFF-voice-providers.md)
- ✅ Web setup wizard at `http://jts.local/voice/` — paste API keys,
  pick the active provider, save. Writes
  `/var/lib/jasper/voice_provider.env` at mode 0600 and restarts
  `jasper-voice`
- ✅ Tools: volume, transport (play/pause/skip/now-playing), Spotify
  search & queue, weather, NYC subway times
- ✅ Multi-user Spotify routing (each household member's account,
  routed by AirPlay title-match)
- ✅ Per-source on/off wizard at `http://jts.local/sources/` —
  AirPlay / Bluetooth / Spotify Connect toggles. Bluetooth's off
  toggle prompts for confirmation when a paired wireless remote
  (e.g. the VK-01 volume knob) is present, since powering the
  adapter off would silently disconnect it. Same prompt fires on
  the Power switch at `http://jts.local/bluetooth/`.
- ✅ Wi-Fi network wizard at `http://jts.local/wifi/` — current
  network at top, scan + tap-to-connect for nearby networks,
  saved networks in a collapse section with Forget. Backed by
  `nmcli`. Connect rolls back to the previous network on failure
  (`nmcli --wait 30 dev wifi connect` + explicit `connection up
  <previous>` on non-zero exit). Hidden SSIDs + WPA-Enterprise
  deferred — home-network case only.
- ✅ Persistent live session with sustained-speech VAD
- ✅ Hardware AEC investigation completed and documented
- ✅ Software AEC bridge reconciles automatically on 6-channel XVF firmware
- ⚠️  Custom "Hey Jasper" wake-word model is a v1.1 follow-up
- ✅ Rotary dial — volume (with on-screen volume gauge), play/pause
  short-press, hold-to-talk long-press all working on hardware.
  Other LVGL scenes (clock / listening orb / speaking waveform /
  now-playing) have firmware scaffold but aren't yet on-device
  validated.
- 🔄 AMOLED touchscreen + mic satellite
  (Waveshare ESP32-S3-Touch-AMOLED-1.8) — Phase 0 (mic capture)
  + Phase 1.1 (WiFi/Improv-over-Serial provisioning, mDNS-SD,
  dlog) + Phase 1.2 (on-screen connection-status indicator on
  the SH8601 AMOLED via Arduino_GFX) shipped. Phase 1.3+ (LVGL
  "Tap to Talk", capacitive touch, UDP audio to Pi-side
  receiver) is the next milestone. Both ESP32 firmware projects
  (dial + satellite) on Arduino-ESP32 v3.x via pioarduino — one
  toolchain across the satellite family. See
  [docs/satellites.md](docs/satellites.md) for the family
  overview, multi-mic arbitration design, and per-device
  roadmap.

Known marginal items: the chip's onboard AEC isn't usable in this
topology (we drive the speaker from a separate USB DAC, not the
chip's codec). Software AEC via WebRTC AEC3 delivers −15 to −18 dB
on music at ~110 MB RAM — see the AEC section below for the full
setup. Live with `NO_INTERRUPTION` on the Gemini session and a
0.7-second wake refractory until/unless that changes.

---

## Repository layout

```
jasper/                         Python daemon source
  voice_daemon.py               Main: wake → real-time LLM → tools → TTS
  audio_io.py                   MicCapture, TtsPlayout (sounddevice-based)
  camilla.py                    pycamilladsp websocket helpers
  voice/                        Provider-agnostic LiveConnection / LiveTurn
                                  protocols + adapters (gemini_session,
                                  openai_session, grok_session) +
                                  shared reconnect supervisor helpers
  tools/                        Tool registry + per-tool implementations
                                  (provider-aware schema serializers)
  control/                      jasper-control: HTTP API for dial/automation
  cli/                          jasper-doctor, jasper-spotify-auth,
                                jasper-aec-{init,tune,bridge},
                                jasper-dial-onboard
  xvf/                          Vendored XMOS XVF3800 control library
  mics/                         Per-mic-family profile registry — one
                                  module per supported mic (xvf3800.py
                                  today). Identity, firmware variants,
                                  mixer invariants, helpers. See mics/README.md.
  web/                          stdlib http.server settings UIs at
                                  /spotify (account OAuth) and /voice
                                  (provider config + key paste)
  data/                         Static data (subway stops, etc.)
  ...                           accounts, spotify_router, vad,
                                volume_persistence, etc.

firmware/
  dial/                         PlatformIO project for the ESP32-S3
                                rotary dial (phase 1: volume only)

deploy/
  install.sh                    Idempotent installer (run as root on Pi)
  alsa/                         /root/.asoundrc template
  camilladsp/                   v1.yml passthrough config + master_gain
  systemd/                      jasper-{camilla,voice,control,mux,aec-bridge,aec-init}
                                + librespot, shairport-sync, nqptp, bt-agent
  modules-load.d/               snd-aloop autoload
  modprobe.d/                   snd-aloop single-card config
  bin/                          jasper-librespot-event (--onevent hook)
  configure-bluez.sh            Speaker-mode pairing config
  shairport-sync.conf           AirPlay 2 receiver config
  nginx-jasper.conf             Standalone /spotify + /dial HTTPS site

docs/                           Subsystem deep-dives ("HANDOFF" docs)
  HANDOFF-aec.md                Acoustic echo cancellation engine
  HANDOFF-xvf3800.md            Canonical reference for the XVF3800 mic
  HANDOFF-airplay.md       AirPlay glitch troubleshooting guide
  HANDOFF-persistent-live-session.md
  HANDOFF-voice-music-control.md
  HANDOFF-volume.md             Source-aware volume coordinator
  multi-user-spotify.md
  audit-pending-followups.md    Open Tier 2/3 follow-ups

scripts/                        Operator helpers (run from laptop)
  fetch-pi-logs.sh              Pull journals + configs into ./logs/
  tail-pi-logs.sh               Live tail
  pi-bundle.sh                  One-shot diagnostic dump
  xvf-interrogate.sh            Deep XVF3800 diagnostic — captures
                                everything (USB, ALSA, params, RMS)
                                tagged by chip iSerial. See HANDOFF-xvf3800.md.
  switch-voice-provider.sh      Flip JASPER_VOICE_PROVIDER between
                                gemini / openai / grok
  switch-gemini-model.sh        Within-Gemini fallback: 3.1 ↔ 2.5
  claim-librespot.sh            One-time: OAuth-claim librespot for a
                                Spotify account so cold-start "play X"
                                works without phone interaction

tests/                          Hardware-free pytest suite
```

---

## Documentation map

| File | Audience | Purpose |
|---|---|---|
| [README.md](README.md) | Anyone landing on the repo | What this is, where to look |
| [CLAUDE.md](CLAUDE.md) | AI assistants (Claude Code, etc.) | Operational rules + per-task guidance for AI sessions |
| [AGENTS.md](AGENTS.md) | OpenAI Codex agents | Same content as CLAUDE.md, separate for tooling reasons |
| [BRINGUP.md](BRINGUP.md) | Operator flashing a fresh Pi | Step-by-step from blank SD card to working speaker |
| [PLAN.md](PLAN.md) | Project planning | v1 phased build, future roadmap |
| [docs/audio-paths.md](docs/audio-paths.md) | Operator + AI | Reference: the two ALSA paths to the dongle and which volume knob attenuates which path |
| [docs/satellites.md](docs/satellites.md) | Anyone working on a satellite device | Cross-cutting design + roadmap for ESP32 satellites (dial, AMOLED mic, etc.) |
| [docs/HANDOFF-*.md](docs/) | Deep-dive on a subsystem | Investigation history + design rationale |

The HANDOFF docs are the most engineer-relevant. Each one is the
canonical "if you're modifying this subsystem, read this first"
reference. Currently:
- [`HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md) —
  Multi-provider voice loop architecture: how `LiveConnection` /
  `LiveTurn` abstract Gemini Live, OpenAI Realtime, and Grok Voice
  Agent behind one switch, plus the per-provider trade-offs and the
  steps for adding a fourth backend
- [`satellites.md`](docs/satellites.md) — The home base for the
  satellite-device family. Existing dial + planned AMOLED mic
  satellite, shared protocols (Improv / mDNS-SD / control HTTP / UDP
  logs), and the multi-mic arbitration design (with prior-art survey
  across HA Assist, Sonos, Apple, Amazon ESP).
- [`HANDOFF-aec.md`](docs/HANDOFF-aec.md) — AEC architecture +
  investigation (engine: why software AEC, why not chip AEC)
- [`HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md) — Canonical
  reference for the Seeed ReSpeaker XVF3800 (USB UA) microphone:
  hardware identity, firmware variants, full parameter space, DFU
  flow, documented failure modes (notably the post-firmware-flash
  ALSA mute trap), diagnostic cookbook. Start here for any
  mic-side investigation.
- [`HANDOFF-resilience.md`](docs/HANDOFF-resilience.md) — The
  five-tier resilience ladder, the 2026-05-11 incident, the
  decision to swap the bridge→voice transport from snd-aloop to
  UDP. Read before touching `jasper/watchdog.py` or the
  `Type=notify` / `WatchdogSec=` blocks in any service unit.
- [`HANDOFF-remote-updates.md`](docs/HANDOFF-remote-updates.md) —
  Research only, no implementation yet. Design space for an OTA
  "Check for updates" button on the management dashboard: option
  survey (`git pull` → GitHub Releases + poll → RAUC A/B
  partition swap), recommended staged build-out (CI first,
  auto-release, then the button), and the open questions before
  specing. Referenced from PLAN.md.
- [`HANDOFF-persistent-live-session.md`](docs/HANDOFF-persistent-live-session.md)
  — Long-running Gemini Live connection management (Gemini-specific
  details — see HANDOFF-voice-providers.md for the cross-provider
  architecture)
- [`HANDOFF-voice-music-control.md`](docs/HANDOFF-voice-music-control.md)
  — Source-aware transport (AirPlay/Spotify Connect) + volume
- [`HANDOFF-volume.md`](docs/HANDOFF-volume.md) — Source-aware
  volume coordinator (one canonical `listening_level`, dispatched
  to whichever source is active, observed inbound at 1 Hz)
- [`HANDOFF-airplay.md`](docs/HANDOFF-airplay.md) — AirPlay
  glitch troubleshooting guide. **Start here if you hear audio
  artifacts on AirPlay.** Symptom → pattern decision flow, concrete
  diagnostic recipes, per-pattern playbooks (with confirmed fixes for
  the patterns we've seen), the source-cited first-principles
  reference, what's been tried, and an escalation ladder for new
  scenarios. Patterns currently fixed: CamillaDSP rate_adjust +
  AsyncSinc oscillation (PR #75), shairport `resync_threshold`
  misfire on snd-aloop fill (PR #83).
- [`multi-user-spotify.md`](docs/multi-user-spotify.md) — Per-household-
  member Spotify account routing

---

## Acoustic echo cancellation (AEC)

This is the subsystem that took the most engineering and has the
most going on. Worth understanding the architecture before you
touch it.

### The problem

A smart speaker that **plays music** and **listens for a wake
word in the same physical box** has a fundamental signal-processing
problem. The microphone hears both the user's voice (what we want)
and the speaker's own output, which can be 20–40 dB louder when
music is playing. Without AEC, the wake-word detector fires on
phonemes from the music or — worse — on the TTS responses we
just synthesised, causing a feedback loop.

There are three places to address this:

1. **Hardware AEC on the mic chip** (the XVF3800's purpose-built
   DSP). Lowest cost, lowest latency, highest quality — but only
   works in topologies the chip's firmware was designed for.
2. **Software AEC on the host** (Pi). Topology-agnostic but costs
   CPU/RAM and is generally less effective at high SPL.
3. **Avoid it**: push-to-talk, physical mic-speaker isolation, or
   ducking music to silence on wake. Eliminates AEC at a UX cost.

### What this project does

**Hardware AEC is OFF**, deliberately. We tried it; the
XVF3800's AEC pipeline was designed assuming the chip drives the
speaker via its own codec, but our topology routes audio through
a separate USB DAC (the Apple dongle). The chip's internal AEC
gain stage auto-mirrors the host's USB-OUT volume control on the
chip's UAC2 sink, which actively sabotages the reference signal
in our topology. Measured ≤2 dB attenuation across every
configuration we tried. See
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md) for the full
investigation including the smoking-gun XMOS docs quote.

The chip is still useful — its **beamforming, noise suppression,
and AGC** all run on the conference channel (channel 0 of the
USB capture endpoint). We use that processed channel; just not
the chip's on-chip AEC.

**Software AEC ships ON by default when the chip is on the 6-channel
firmware variant.** A Python daemon (`jasper-aec-bridge`) runs
WebRTC AEC3 echo cancellation between the host's music chain
(tapped via `pcm.jasper_capture` dsnoop) and the chip's raw mic 0
(channel 2 of the 6-channel firmware). It sends an AEC'd mono signal
over UDP localhost (`127.0.0.1:9876`) to `jasper-voice`'s
`UdpMicCapture` instead of the chip's processed mic. The engine is
the `jasper_aec3` pybind11 binding around Trixie's
`libwebrtc-audio-processing-1` v1.3-3 — delivers −15 to −18 dB on
music with the production REF_GAIN/MIC_GAIN tunings, at ~3-8% of
one Pi 5 core and ~95 MB RAM.

The transport is UDP (not snd-aloop's `LoopbackAEC` card, which is
what the original design used) because snd-aloop's kernel-side
`loopback_cable` wedges when a consumer is SIGKILL'd, requiring a
reboot to clear. Hit in production May 2026; UDP localhost has no
kernel state to corrupt and `sendto()` is non-blocking. See
[docs/HANDOFF-resilience.md](docs/HANDOFF-resilience.md) for the
full architectural rationale and the multi-tier resilience design
the speaker now uses.

The bridge needs the **6-channel XVF firmware variant** since it
taps raw mic 0 (channel 2 of 6) — the 2-channel firmware Seeed
ships by default doesn't expose those raw channels. As of
2026-05-15 the recommended file is
`respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin` (the only
6-channel variant in upstream `master`); browse the
[upstream firmware directory](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb)
before flashing in case a newer one has shipped. The full
procedure is in BRINGUP.md Phase 2A.5; the known-good version
constants are tracked in [`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py).
On the 2-channel firmware the bridge stays disabled and voice
reads the chip's processed conference channel directly. `install.sh` runs
`jasper-aec-reconcile`, which auto-detects + auto-enables when the
hardware is ready and clears stale UDP mic config when the Array is
missing.

### What's installed and at what cost

Numbers are **Pss** (proportional set size — shared libs deduplicated;
the honest "private cost" measure) on a Pi 5, after the lazy-import
and openwakeword stub diet landed.

| Component | Default | RAM (Pss) | CPU |
|---|---|---|---|
| `jasper-voice` (wake + LLM + tools) | Active | ~140-150 MB | ~12% of one core during a session |
| `jasper-aec-bridge` (software AEC) | **Active** on 6-ch firmware, **disabled** on 2-ch | +85 MB | +3% of one core |
| `jasper-aec-init` (boot-time chip init) | follows aec-bridge | one-shot, ~0 | ~0 |
| `jasper-camilla` (always-on CamillaDSP, ducking) | Active | ~12 MB | <1% |
| `jasper-control` (HTTP API + dial routing) | Active | ~35 MB | ~0.1% idle |
| `jasper-input` (HID accessory bridge) | Active | ~28 MB | ~0% idle |
| `jasper-mux` (renderer arbitration) | Active | ~13 MB | ~0% idle |
| `jasper-web` (Spotify / voice / Google / AirPlay / Sources / Wake / Wi-Fi wizards) | **Socket-activated** | ~0 idle, ~22 MB when open | n/a idle |
| `jasper-bluetooth-web` (BT pair UI) | **Socket-activated** | ~0 idle, ~17 MB when open | n/a idle |
| `jasper-correction-web` (room correction UI) | **Socket-activated** | ~0 idle, ~15 MB when open | n/a idle |
| `jasper-dial-web` (dial onboarding UI) | **Socket-activated** | ~0 idle, ~9 MB when open | n/a idle |
| Single-card snd-aloop (Loopback) | Loaded at boot | ~0 | ~0 |
| dsnoop tap on music chain | Always present | ~0 | ~0 |

The four web-wizard daemons are socket-activated — systemd holds
their ports open and only spawns the daemon when a tab opens any of
its pages. `jasper-web` alone hosts seven URL surfaces (Spotify, voice,
Google, AirPlay, Sources, Wake, Wi-Fi) on seven loopback ports; the
other three daemons each host one. All four exit after 10 min of no
requests, so the resident cost is zero between admin sessions. First
request after idle takes ~500-800 ms (Python startup); invisible
during the OAuth round-trip or BT pair flow.

**Total Pss baseline with AEC on**: ~330 MB jasper-* daemons +
~80 MB system/OS plumbing + page cache → typically ~770 MB used
out of 2 GB. On a 1 GB Pi, ~200 MB headroom with AEC on; ~280 MB
with AEC off.

The single-card music-chain snd-aloop and the dsnoop tap stay loaded
even when the bridge is disabled — they cost essentially nothing and
let the bridge be enabled later with no further setup. The bridge's
output path is UDP, not a second snd-aloop card. The 6-channel XVF
firmware (flashed once via DFU) also stays — its channel 0 is identical
to the 2-channel firmware's channel 0, so it's benign for non-bridge use.

`install.sh` enables `jasper-aec-reconcile.service`, seeds
`/var/lib/jasper/aec_mode.env` with `JASPER_AEC_MODE=auto`, and runs
the reconciler once. On 6-channel firmware it selects
`JASPER_MIC_DEVICE=udp:9876` and starts the bridge; on 2-channel
firmware or no Array it leaves voice on direct mic when possible and
keeps the bridge off. Either way, AEC is reversible at runtime — see
CLAUDE.md "Acoustic echo cancellation" section.

### The chip control library

`jasper/xvf/xvf_host.py` is vendored from
`respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control/` and
is useful as a diagnostic tool independent of AEC:

```sh
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host --list  # all params
```

Read AEC convergence, dump filter coefficients, change beam
parameters, etc. Don't call `SAVE_CONFIGURATION` — known brick
hazard on certain firmware versions (respeaker repo issue #8).

---

## Other major subsystems

These have their own HANDOFF docs that are worth reading before
modifying. One-line summaries here:

- **Voice loop** ([HANDOFF-persistent-live-session.md](docs/HANDOFF-persistent-live-session.md))
  — Long-lived Gemini Live connection with manual VAD,
  `activity_start`/`activity_end` markers, sustained-speech
  detection. The choice of manual VAD over server-side auto VAD
  is empirically derived (auto VAD silently drops turn 2 on a
  paused-resumed connection).
- **Music transport** ([HANDOFF-voice-music-control.md](docs/HANDOFF-voice-music-control.md))
  — Source-aware `next_track`/`pause`/`resume`/etc. routing
  across AirPlay (MPRIS via shairport-sync) and Spotify Connect
  (spotipy). Gets non-trivial when AirPlay is carrying
  iPhone-Spotify (the title-match → Web API path). Bluetooth
  has no graceful pause API; "nothing playing" returns a clean
  error.
- **Multi-user Spotify routing** ([multi-user-spotify.md](docs/multi-user-spotify.md))
  — Each household member OAuths their own account against one
  Spotify Developer App. Routing decides whose account a voice
  command targets by cross-referencing AirPlay metadata against
  each account's currently-playing track.
- **Audible failure feedback** ([HANDOFF-audible-feedback.md](docs/HANDOFF-audible-feedback.md))
  — Pre-rendered Gemini-TTS WAVs that the daemon plays in two
  situations instead of falling silent: **reactive cues** when a
  wake event hits a wake-blocking failure (spend cap reached, voice
  backend in reconnect/backoff, etc.), and **proactive cues** that
  background supervisors fire when something's wrong even if the
  user hasn't woken the speaker (today: 5 consecutive identical
  reconnect failures → `cant_reach_cloud`, rate-limited to once
  per hour). Content-addressable cache keyed on `(rendered text,
  voice, model, format)` so any input change auto-invalidates.
  CLI: `jasper-cues regenerate|list|play <slug>`.

---

## Getting started

If you have a fresh Pi and want to deploy from scratch, follow
[BRINGUP.md](BRINGUP.md) end-to-end. It walks from "blank SD card"
through "Hey Jarvis works" in ~3-4 hours.

If the repo is already deployed and you're just pushing changes:

```sh
# from your laptop:
bash scripts/deploy-to-pi.sh
# or with a non-default host:
PI_HOST=192.168.1.42 bash scripts/deploy-to-pi.sh
```

This is a thin wrapper that captures the current git SHA + branch
(via `git rev-parse`), rsyncs to `/home/pi/jts/`, then runs install.sh
under sudo with `JASPER_DEPLOY_SHA` / `JASPER_DEPLOY_BRANCH` env vars
set. install.sh writes those into `/var/lib/jasper/build.txt` so the
/system dashboard's "Software" card shows the real deployed version
instead of "unknown" (.git/ is excluded from the rsync for speed).

If you'd rather drive the rsync + install yourself, the equivalent
raw form is:

```sh
rsync -avz --delete \
  --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' \
  --exclude '.pio' --exclude '.claude/worktrees' \
  ./ pi@jts.local:/home/pi/jts/

ssh pi@jts.local 'sudo JASPER_DEPLOY_SHA=$(git -C ~/jts rev-parse --short HEAD) \
    bash /home/pi/jts/deploy/install.sh'
```

The install script is idempotent.

---

## Debugging

When something's broken:

```sh
# On the Pi:
sudo /opt/jasper/.venv/bin/jasper-doctor          # codified smoke tests
curl -s http://jts.local:8780/state | jq          # cross-daemon snapshot

# From the laptop:
bash scripts/fetch-pi-logs.sh                     # pull journals to ./logs/
bash scripts/tail-pi-logs.sh                      # live tail all units
bash scripts/jasper-trace.sh                      # filter to event= lines
```

`jasper-doctor` codifies the smoke tests in BRINGUP.md and runs
them as code. `fetch-pi-logs.sh` pulls journals + configs +
ALSA state into `./logs/`, redacting secrets server-side.
`jasper-trace.sh` is the live-tail equivalent narrowed to the
cross-daemon `event=` lines emitted by `jasper.camilla.Ducker`,
the dial volume routes, etc. — useful when you want to see
duck/preempt/route timing without the rest of each daemon's
chatter. `GET /state` on `jasper-control` returns one JSON
snapshot of voice / audio / renderers / satellites, fail-soft
per section.

Common failure modes are documented at the bottom of
[BRINGUP.md](BRINGUP.md). For subsystem-specific issues, the
relevant HANDOFF doc almost certainly addresses your symptom.

---

## What's deferred

See [PLAN.md](PLAN.md) "What comes after v1" for the full
sequenced roadmap. Highlights of what's NOT in v1: room
correction web tool, captive portal (Balena WiFi Connect),
Snapcast stereo pair, wireless subwoofer, mesh AP+STA, USB
gadget mode, Home Assistant bridge, custom "Hey Jasper" wake-word
training. Don't build these until v1 actually plays music with
voice control end-to-end (it does).
