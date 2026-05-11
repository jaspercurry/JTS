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
        │                            - openWakeWord ("Hey Jarvis")
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

There's also an opt-in software AEC bridge (`jasper-aec-bridge`)
that taps the music chain via a `pcm.jasper_capture` dsnoop, runs
WebRTC AEC3 echo cancellation against the chip's raw mic, and
emits a cleaned-up mono signal to a second snd-aloop card for
jasper-voice to consume. Disabled by default — see § below.

---

## Current status

`v1` (per [PLAN.md](PLAN.md)) is mostly landed:

- ✅ Music streaming (AirPlay 2, Spotify Connect, Bluetooth A2DP) via
  source-built shairport-sync + nqptp, librespot (rust, via raspotify
  .deb) with log volume curve, and bluez-alsa
- ✅ `jasper-mux` daemon for latest-source-wins preemption
- ✅ Always-on CamillaDSP with a passthrough `master_gain` mixer
- ✅ Wake-word detection ("Hey Jarvis", openWakeWord ONNX)
- ✅ Gemini Live voice loop with tool calling
- ✅ Provider-agnostic voice abstraction — `JASPER_VOICE_PROVIDER`
  flips between Gemini Live, OpenAI Realtime (`gpt-realtime-2`), and
  xAI Grok Voice Agent. See
  [docs/HANDOFF-voice-providers.md](docs/HANDOFF-voice-providers.md)
- ✅ Web setup wizard at `https://jts.local/voice/` — paste API keys,
  pick the active provider, save. Writes
  `/var/lib/jasper/voice_provider.env` at mode 0600 and restarts
  `jasper-voice`
- ✅ Tools: volume, transport (play/pause/skip/now-playing), Spotify
  search & queue, weather, NYC subway times
- ✅ Multi-user Spotify routing (each household member's account,
  routed by AirPlay title-match)
- ✅ Persistent live session with sustained-speech VAD
- ✅ Hardware AEC investigation completed and documented
- ⚠️  Software AEC infrastructure built but disabled by default
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
  modprobe.d/                   snd-aloop two-card config
  bin/                          jasper-librespot-event (--onevent hook)
  configure-bluez.sh            Speaker-mode pairing config
  shairport-sync.conf           AirPlay 2 receiver config
  nginx-jasper.conf             Standalone /spotify + /dial HTTPS site

docs/                           Subsystem deep-dives ("HANDOFF" docs)
  HANDOFF-aec.md                Acoustic echo cancellation
  HANDOFF-airplay-sync.md       AirPlay glitch troubleshooting guide
  HANDOFF-persistent-live-session.md
  HANDOFF-voice-music-control.md
  HANDOFF-volume.md             Source-aware volume coordinator
  multi-user-spotify.md
  audit-pending-followups.md    Open Tier 2/3 follow-ups

scripts/                        Operator helpers (run from laptop)
  fetch-pi-logs.sh              Pull journals + configs into ./logs/
  tail-pi-logs.sh               Live tail
  pi-bundle.sh                  One-shot diagnostic dump
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
  investigation
- [`HANDOFF-persistent-live-session.md`](docs/HANDOFF-persistent-live-session.md)
  — Long-running Gemini Live connection management (Gemini-specific
  details — see HANDOFF-voice-providers.md for the cross-provider
  architecture)
- [`HANDOFF-voice-music-control.md`](docs/HANDOFF-voice-music-control.md)
  — Source-aware transport (AirPlay/Spotify Connect) + volume
- [`HANDOFF-volume.md`](docs/HANDOFF-volume.md) — Source-aware
  volume coordinator (one canonical `listening_level`, dispatched
  to whichever source is active, observed inbound at 1 Hz)
- [`HANDOFF-airplay-sync.md`](docs/HANDOFF-airplay-sync.md) — AirPlay
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

**Software AEC is BUILT but DISABLED by default.** A Python daemon
(`jasper-aec-bridge`) runs WebRTC AEC3 echo cancellation between
the host's music chain (tapped via `pcm.jasper_capture` dsnoop) and
the chip's raw mic 0 (channel 2 of the 6-channel firmware). It
emits an AEC'd mono signal to a second snd-aloop card
(`hw:7,1` = LoopbackAEC) that jasper-voice can consume instead of
the chip's processed mic. The engine is the `jasper_aec3` pybind11
binding around Trixie's `libwebrtc-audio-processing-1` v1.3-3 —
delivers −15 to −18 dB on music with the production REF_GAIN/
MIC_GAIN tunings, at ~3-8% of one Pi 5 core and ~110 MB RAM.

It's disabled by default because:
- The 1GB Pi 5 is at the edge with 110 MB extra (~60% RAM use,
  ~160 MB swap when bridge is running) — the 2GB SKU is
  recommended if you want the bridge on
- It requires the 6-channel XVF firmware variant (`v2.0.8 6chl`)
  flashed via DFU (BRINGUP.md Phase 2A.5)
- The chip's beamformed conference channel (the default mic
  source) is good enough for typical use with `NO_INTERRUPTION`
  + ducking — the bridge is most useful when you want wake-word
  detection during loud music playback

To turn the bridge on for A/B testing, see [CLAUDE.md](CLAUDE.md)
"Acoustic echo cancellation" section or [BRINGUP.md](BRINGUP.md)
Phase 2A.2 (both have the same enable/disable commands). Requires
the chip to be on the 6-channel firmware variant — `v2.0.8 6chl`,
DFU procedure in BRINGUP.md Phase 2A.5; reversible.

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
| `jasper-web` (Spotify/voice/Google/AirPlay wizards) | **Socket-activated** | ~0 idle, ~20 MB when open | n/a idle |
| `jasper-bluetooth-web` (BT pair UI) | **Socket-activated** | ~0 idle, ~17 MB when open | n/a idle |
| `jasper-correction-web` (room correction UI) | **Socket-activated** | ~0 idle, ~15 MB when open | n/a idle |
| `jasper-dial-web` (dial onboarding UI) | **Socket-activated** | ~0 idle, ~9 MB when open | n/a idle |
| Two-card snd-aloop (Loopback + LoopbackAEC) | Loaded at boot | ~0 | ~0 |
| dsnoop tap on music chain | Always present | ~0 | ~0 |

The four web wizards are socket-activated — systemd holds their
ports open and only spawns the daemon when a tab opens the page.
They exit after 10 min of no requests, so the resident cost is
zero between admin sessions. First request after idle takes
~500-800 ms (Python startup); invisible during the OAuth round-trip
or BT pair flow.

**Total Pss baseline with AEC on**: ~330 MB jasper-* daemons +
~80 MB system/OS plumbing + page cache → typically ~770 MB used
out of 2 GB. On a 1 GB Pi, ~200 MB headroom with AEC on; ~280 MB
with AEC off.

The two-card snd-aloop and the dsnoop tap stay loaded even when
the bridge is disabled — they cost essentially nothing and let
the bridge be enabled later with no further setup. The 6-channel
XVF firmware (flashed once via DFU) also stays — its channel 0
is identical to the 2-channel firmware's channel 0, so it's
benign for non-bridge use.

`install.sh` auto-enables AEC if the chip is on the 6-channel
firmware variant at install time. On 2-channel firmware (the
ReSpeaker shipping default), AEC stays disabled and the installer
prints a one-line hint pointing at the BRINGUP.md DFU procedure.
Either way, AEC is reversible at runtime — see CLAUDE.md
"Acoustic echo cancellation" section.

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
# from your laptop, with rsync set up to the Pi:
rsync -avz --delete \
  --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' \
  --exclude '.pio' --exclude '.claude/worktrees' \
  ./ pi@jts.local:/home/pi/jts/

ssh pi@jts.local 'sudo bash /home/pi/jts/deploy/install.sh'
ssh pi@jts.local 'sudo systemctl restart jasper-camilla jasper-voice jasper-correction-web'
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
