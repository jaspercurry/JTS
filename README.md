# JTS — Jasper smart speaker

A custom voice-controlled smart speaker built on a Raspberry Pi 5
plus [moOde audio](https://moodeaudio.org/) and
[CamillaDSP](https://github.com/HEnquist/camilladsp), with voice
via [Gemini 3.1 Flash Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview).
This is a personal hobby project; not a product.

The pitch: a music streamer that's also a voice assistant, built
from open hardware and open audio software, with the LLM costing
roughly $1–3/month at light use.

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
  moOde renderers (MPD / shairport-sync / librespot / bluealsa)
        │
        │  via /etc/alsa/conf.d/zz-jts-loopback.conf
        │  (rewrites pcm._audioout to point at snd-aloop)
        ▼
  hw:Loopback,0,sub0  ── snd-aloop ──  hw:Loopback,1,sub0
                                              │
                                              ▼
                                    pcm.jasper_capture
                                    (type plug → type dsnoop;
                                    multiple readers OK)
                                              │
                                              ▼
                                    jasper-camilla (CamillaDSP, port 1234)
                                    - master_gain mixer (the ducking knob)
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
        │                            jasper-voice (wake-word, Gemini Live, tools)
        │                            - openWakeWord ("Hey Jarvis")
        │                            - Silero VAD
        │                            - google-genai live session
        │                            - tool registry (volume, transport, Spotify, weather…)
        │                                     │
        │                                     ▼
        │                            TTS (Gemini-generated PCM)
        │                                     │
        │                                     ▼
        │                            pcm.jasper_out (same dmix as music)
        │                                     │
        │                                     ▼
        └──── airborne echo back to mic ◄── speakers
```

`jasper-camilla` and `jasper-voice` both write to the same dongle
dmix (`pcm.jasper_out`); dmix sums their streams. Music ducks on
wake via a CamillaDSP `SetVolume` call on the `master_gain` mixer
over its websocket on port 1234.

There's also an opt-in software AEC bridge (`jasper-aec-bridge`)
that taps the music chain via `pcm.jasper_capture`, runs SpeexDSP
echo cancellation against the chip's raw mic, and emits a
cleaned-up mono signal to a second snd-aloop card for jasper-voice
to consume. Disabled by default — see § below.

---

## Current status

`v1` (per [PLAN.md](PLAN.md)) is mostly landed:

- ✅ Music streaming (AirPlay / Spotify Connect / Bluetooth) via moOde
- ✅ Always-on CamillaDSP with a passthrough `master_gain` mixer
- ✅ Wake-word detection ("Hey Jarvis", openWakeWord ONNX)
- ✅ Gemini Live voice loop with tool calling
- ✅ Tools: volume, transport (play/pause/skip/now-playing), Spotify
  search & queue, weather, NYC subway times
- ✅ Multi-user Spotify routing (each household member's account,
  routed by AirPlay title-match)
- ✅ Persistent live session with sustained-speech VAD
- ✅ Hardware AEC investigation completed and documented
- ⚠️  Software AEC infrastructure built but disabled by default
- ⚠️  Custom "Hey Jasper" wake-word model is a v1.1 follow-up
- 🔄 ESP32 rotary dial: phase 1 landed (volume); phase 2/3
  (play/pause click + hold-to-talk) and phase 5 (LVGL display) pending.
  See "Rotary dial controller" in [CLAUDE.md](CLAUDE.md).

Known marginal items: the chip's onboard AEC isn't usable in this
topology (we drive the speaker from a separate USB DAC, not the
chip's codec), and software AEC delivers only modest attenuation
at significant RAM cost — see the AEC section below for the full
trade-off analysis. Live with `NO_INTERRUPTION` on the Gemini
session and a 5-second wake refractory until/unless that changes.

---

## Repository layout

```
jasper/                         Python daemon source
  voice_daemon.py               Main: wake → Gemini Live → tools → TTS
  audio_io.py                   MicCapture, TtsPlayout (sounddevice-based)
  camilla.py                    pycamilladsp websocket helpers
  voice/                        VoiceSession interface + Gemini adapter
  tools/                        Tool registry + per-tool implementations
  control/                      jasper-control: HTTP API for dial/automation
  cli/                          jasper-doctor, jasper-spotify-auth,
                                jasper-aec-{init,tune,bridge},
                                jasper-dial-onboard
  xvf/                          Vendored XMOS XVF3800 control library
  web/                          FastAPI: Spotify household OAuth web UI
  data/                         Static data (subway stops, etc.)
  ...                           accounts, spotify_router, vad,
                                volume_persistence, etc.

firmware/
  dial/                         PlatformIO project for the ESP32-S3
                                rotary dial (phase 1: volume only)

deploy/
  install.sh                    Idempotent installer (run as root on Pi)
  alsa/                         /root/.asoundrc + zz-jts-loopback.conf
  camilladsp/                   v1.yml passthrough config + master_gain
  systemd/                      jasper-{camilla,voice,control,aec-bridge,aec-init}
  modules-load.d/               snd-aloop autoload
  modprobe.d/                   snd-aloop two-card config
  nginx-jasper{,-https}.conf    /spotify reverse-proxy

docs/                           Subsystem deep-dives ("HANDOFF" docs)
  HANDOFF-aec.md                Acoustic echo cancellation
  HANDOFF-persistent-live-session.md
  HANDOFF-voice-music-control.md
  multi-user-spotify.md
  audit-pending-followups.md    Open Tier 2/3 follow-ups

scripts/                        Operator helpers (run from laptop)
  fetch-pi-logs.sh              Pull journals + configs into ./logs/
  tail-pi-logs.sh               Live tail
  pi-bundle.sh                  One-shot diagnostic dump
  switch-gemini-model.sh        Flip JASPER_GEMINI_MODEL between 3.1 / 2.5

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
| [docs/HANDOFF-*.md](docs/) | Deep-dive on a subsystem | Investigation history + design rationale |

The HANDOFF docs are the most engineer-relevant. Each one is the
canonical "if you're modifying this subsystem, read this first"
reference. Currently:
- [`HANDOFF-aec.md`](docs/HANDOFF-aec.md) — AEC architecture +
  investigation
- [`HANDOFF-persistent-live-session.md`](docs/HANDOFF-persistent-live-session.md)
  — Long-running Gemini Live connection management
- [`HANDOFF-voice-music-control.md`](docs/HANDOFF-voice-music-control.md)
  — Source-aware transport (AirPlay/Spotify/MPD) + volume
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
(`jasper-aec-bridge`) runs SpeexDSP echo cancellation between the
host's music chain (tapped via `pcm.jasper_capture` dsnoop) and
the chip's raw mic 0 (channel 2 of the 6-channel firmware). It
emits an AEC'd mono signal to a second snd-aloop card
(`hw:5,1` = LoopbackAEC) that jasper-voice can consume instead
of the chip's processed mic. Measured −2 to −8 dB attenuation
during sustained playback, ~110 MB RAM, ~3% of one CPU core.

It's disabled by default because:
- The 1GB Pi 5 is at the edge with 110 MB extra (~60% RAM use,
  ~160 MB swap when bridge is running)
- Whether the modest attenuation actually improves wake-word
  reliability hasn't been measured end-to-end yet
- The chip's beamformed conference channel (the default mic
  source) is good enough for typical use with `NO_INTERRUPTION`
  + ducking

To turn the bridge on for A/B testing, see [CLAUDE.md](CLAUDE.md)
"Acoustic echo cancellation" section or [BRINGUP.md](BRINGUP.md)
Phase 2A.2 (both have the same enable/disable commands). Requires
the chip to be on the 6-channel firmware variant — `v2.0.8 6chl`,
DFU procedure in BRINGUP.md Phase 2A.5; reversible.

### What's installed and at what cost

| Component | Default | RAM impact (Pi 5 1GB) | CPU impact |
|---|---|---|---|
| `jasper-camilla` (always-on CamillaDSP, ducking) | Active | ~8 MB | <1% |
| `jasper-voice` (wake + Gemini + tools) | Active | ~265 MB | ~12% of one core |
| `jasper-aec-bridge` (software AEC) | **Disabled** | +110 MB if enabled | +3% of one core if enabled |
| `jasper-aec-init` (boot-time chip init) | **Disabled** | one-shot, ~0 | ~0 |
| Two-card snd-aloop (Loopback + LoopbackAEC) | Loaded at boot | ~0 | ~0 |
| dsnoop tap on music chain | Always present | ~0 | ~0 |

The two-card snd-aloop and the dsnoop tap stay loaded even when
the bridge is disabled — they cost essentially nothing and let
the bridge be enabled later with no further setup. The 6-channel
XVF firmware (flashed once via DFU) also stays — its channel 0
is identical to the 2-channel firmware's channel 0, so it's
benign for non-bridge use.

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
  across AirPlay (MPRIS via shairport-sync), Spotify Connect
  (spotipy), and MPD. Gets non-trivial when AirPlay is carrying
  iPhone-Spotify (the title-match → Web API path).
- **Multi-user Spotify routing** ([multi-user-spotify.md](docs/multi-user-spotify.md))
  — Each household member OAuths their own account against one
  Spotify Developer App. Routing decides whose account a voice
  command targets by cross-referencing AirPlay metadata against
  each account's currently-playing track.

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
  ./ pi@jasper.local:/home/pi/jts/

ssh pi@jasper.local 'sudo bash /home/pi/jts/deploy/install.sh'
ssh pi@jasper.local 'sudo systemctl restart jasper-camilla jasper-voice'
```

The install script is idempotent. moOde stays untouched.

---

## Debugging

When something's broken:

```sh
# On the Pi:
sudo /opt/jasper/.venv/bin/jasper-doctor          # codified smoke tests

# From the laptop:
bash scripts/fetch-pi-logs.sh                     # pull journals to ./logs/
bash scripts/tail-pi-logs.sh                      # live tail all units
```

`jasper-doctor` codifies the smoke tests in BRINGUP.md and runs
them as code. `fetch-pi-logs.sh` pulls journals + configs +
ALSA state into `./logs/`, redacting secrets server-side.

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
