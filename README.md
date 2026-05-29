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

**Want to set one up?**
- **Using Claude Code?** Just open this repo and say *"I want to set up
  a JTS speaker"* (or *"set up a Pi"*, *"I just got a new Pi"*,
  whatever feels natural). Claude reads the
  [`/onboard-pi`](.claude/commands/onboard-pi.md) skill and walks you
  through every step — Pi Imager download, SD card flash, first boot,
  network discovery (including multi-speaker collision detection), and
  the install. ~30 minutes total.
- **Prefer to read the steps yourself?** [QUICKSTART.md](QUICKSTART.md)
  is the same flow as a human-readable walkthrough.
- **Doing the full long-form bringup** (hardware calibration, XVF
  firmware flashing, satellite devices)? See [BRINGUP.md](BRINGUP.md).

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
  bluealsa-aplay (BT A2DP)      jasper-usbsink (USB audio input)
        │                       │
        │ private snd-aloop lanes: hw:Loopback,0,0..4
        ▼                       ▼
  hw:Loopback,1,0..4  ──►  jasper-fanin
                              │ sums active renderer/test lanes
                              ▼
                       hw:Loopback,0,7
                              │
                              ▼ (loop)
                       pcm.jasper_capture / pcm.jasper_ref
                              │
                              ▼
                    jasper-camilla (CamillaDSP, port 1234)
                    - main_volume (the ducking knob)
                    - flat passthrough today
                              │
                              ▼
                    outputd_content_playback
                              │
                              ▼ (loop)
                    outputd_content_capture
                              │
                              ▼
                    jasper-outputd (final output owner)
                    - mixes post-DSP content + assistant audio
                    - clamps positive TTS gain
                    - publishes runtime health / xrun counters
                              │
                              ▼
                    outputd_dac (Apple dongle hw)
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
        │                            /run/jasper-outputd/tts.sock
        │                                     │
        │                                     ▼
        └──── airborne echo back to mic ◄── speakers
```

On the outputd cutover branch, `jasper-outputd` is the only normal
writer to the physical DAC. `jasper-camilla` writes post-DSP content
to a private loopback lane, and `jasper-voice` sends assistant PCM over
outputd's local TTS socket. Music still ducks on wake via a CamillaDSP
`SetMainVolume` call (the `main_volume` property, not the
`master_gain` mixer — that mixer is identity) over its websocket on
port 1234.

> ### Important: one final output owner
>
> Music still goes **through** CamillaDSP. TTS goes **around**
> CamillaDSP but no longer writes around the final output owner; it
> enters `jasper-outputd`, which mixes it with post-DSP content and
> owns the DAC timing loop. `main_volume` only attenuates music — TTS
> keeps up via a separate tracker (`TtsVolumeTracker`) that measures
> the actual music level downstream (`playback_rms`) and scales TTS to
> sit a configurable headroom above it. Works the same whether the user
> is turning the iPhone slider, the Spotify slider, the dial, the
> `listening_level` wizard, or an external amp downstream of the
> dongle. To test the chain at a controlled volume, play to
> `correction_substream`; the legacy `jasper_out` dmix remains only as
> the main-branch rollback path. Why the split and what the tracker
> does:
> [`docs/audio-paths.md`](docs/audio-paths.md).

`jasper-mux` arbitrates between the renderers. In auto mode, when a new
source transitions to playing while another is already active, it
preempts the older one so the user gets "latest source wins" UX. For
AirPlay, preempt means MPRIS `Stop` so shairport-sync drops the current
playback session instead of leaving an invisible paused sender behind.
The landing page
also exposes a lightweight Source selector: manual mode gates one
renderer lane through `jasper-fanin` without turning any source on/off.
Before mux moves the fan-in gate, it asks `VolumeCoordinator` to make the
target source's volume carrier safe, so switching between push-volume
sources (Spotify/Bluetooth) and Camilla-master sources (AirPlay/USB)
cannot expose a full-scale transient. When no source has a guarded
winner yet, mux keeps fan-in in `NONE` so a newly started renderer does
not leak through between polls.

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
- ✅ `jasper-mux` daemon for latest-source-wins preemption plus manual
  landing-page source selection with guarded volume handoff
- ✅ Always-on CamillaDSP with a passthrough `master_gain` mixer
- ✅ Outputd cutover branch: `jasper-outputd` owns direct DAC playback,
  mixes post-DSP content with assistant audio, exposes `/state.outputd`
  health, and leaves the main-branch Camilla statefile intact for
  rollback
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
  pick the active provider, choose tested/fallback models, manually
  refresh provider model lists for experimental trials, save. Writes
  `/var/lib/jasper/voice_provider.env` at mode 0600 and restarts
  `jasper-voice`
- ✅ Tools: volume, transport (play/pause/skip/now-playing), Spotify
  search & queue, weather (now including daily sunrise/sunset),
  NYC subway times, NYC MTA bus arrivals, NYC Citi Bike availability
  (split between classic bikes and e-bikes, with open-dock counts;
  all configured via the `/transit/` wizard), current wall-clock time
- ✅ Multi-user Spotify routing (each household member's account,
  routed by AirPlay title-match)
- ✅ Transit setup wizard at `http://jts.local/transit/` — type your
  address, the page geocodes via OSM Nominatim, shows nearest subway,
  bus stops, and Citi Bike stations, lets you pick. Multi-stop bus
  support — save both the eastbound and westbound stops at your
  corner and "next bus" unions arrivals across them. Subway "next
  train" returns every line at the station including service-change
  reroutes (an N rerouted onto D tracks at a D station appears in the
  same answer). Citi Bike multi-station picker with a household-wide
  "only e-bikes" toggle; voice answers always split classic from
  e-bike counts unless the toggle is on. Modular over
  `jasper.transit.REGISTRY` so future cities/modes (Berlin BVG,
  Capital Bikeshare DC, …) are a single new module under
  `jasper/transit/providers/`. NYC subway and Citi Bike are keyless;
  NYC bus requires a free MTA BusTime API key — that card is locked
  until the user pastes one.
- ✅ Per-source on/off wizard at `http://jts.local/sources/` —
  AirPlay / Bluetooth / Spotify Connect / USB Audio Input toggles.
  Bluetooth's off toggle prompts for confirmation when a paired
  wireless remote (e.g. the VK-01 volume knob) is present, since
  powering the adapter off would silently disconnect it. Same
  prompt fires on the Power switch at `http://jts.local/bluetooth/`.
- ✅ Sound curve + preference EQ wizard at `http://jts.local/sound/` —
  Off / Saved / Draft tabs as the live source, stock Flat / Harman-style
  / B&K-style presets, a five-band Simple EQ (Sub-bass / Bass / Mid /
  Presence / Treble) plus an exclusive PEQ editing mode, and named custom
  profiles (save / overwrite / rename / delete). Built on the canonical
  design system ([`deploy/assets/app.css`](deploy/assets/app.css)).
  Applying emits a CamillaDSP config that preserves any active
  room-correction PEQs; Off turns off only preference shaping without
  clearing room correction. See
  [docs/HANDOFF-sound-preferences.md](docs/HANDOFF-sound-preferences.md)
  for the composition contract, profile semantics, and observability
  hooks.
- ✅ Speaker-name wizard at `http://jts.local/speaker/` — one display
  name for AirPlay, Spotify Connect, Bluetooth, and USB Audio. Defaults
  to `JTS`; the URL remains `jts.local`.
- ✅ **USB Audio Input** (`jasper-usbsink`) — fourth music source.
  Plug a computer into the Pi's USB-C port (via the 8086
  Consultancy USB-C/PWR Splitter) and the host sees the configured
  speaker name as a USB audio output device. Off by default; toggle at
  `http://jts.local/sources/` enables it. The host's volume slider
  drives JTS's canonical `listening_level` (feels like spinning the
  dial). Joins the existing mux arbitration for latest-source-wins
  preemption. Zero RAM cost when off, ~22 MB when on. See
  [docs/HANDOFF-usbsink.md](docs/HANDOFF-usbsink.md) for the full
  design.
- ✅ Wi-Fi network wizard at `http://jts.local/wifi/` — current
  network at top, scan + tap-to-connect for nearby networks,
  manual join-by-name fallback for hidden or scan-suppressed networks,
  saved networks in a collapse section with Forget. Backed by
  `nmcli`. On Pi 5 brcmfmac scan suppression, `/wifi/scan` attempts a
  bounded non-disruptive self-heal before falling back to manual join.
  Connect rolls back to the previous network on failure
  (`nmcli --wait 30 dev wifi connect` + explicit `connection up
  <previous>` on non-zero exit). WPA-Enterprise deferred — home-network
  case only.
- ✅ Persistent live session with sustained-speech VAD
- ✅ Hardware AEC investigation: production approach decided (chip
  AEC off in the dongle topology, software AEC3 instead); Option D
  (chip AEC with USB-IN reference) is the one remaining open variant
  — infrastructure shipped + shelved at [`docs/CHIP-AEC-EXPERIMENT.md`](docs/CHIP-AEC-EXPERIMENT.md)
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
  audio_io.py                   MicCapture, TtsPlayout, outputd TTS transport
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
  transit/                      Modular transit-provider registry —
                                  base Protocol + geocode.py + per-city
                                  providers/. NYC subway + bus + Citi
                                  Bike today, contributor-extensible
                                  (Berlin BVG, Capital Bikeshare, …).
  web/                          stdlib http.server settings UIs at
                                  /spotify (account OAuth) and /voice
                                  (provider config + key paste) +
                                  /transit (address geocode + stop pick)
  data/                         Static data (subway stops, etc.)
  ...                           accounts, spotify_router, vad,
                                volume_persistence, etc.

firmware/
  dial/                         PlatformIO project for the ESP32-S3
                                rotary dial (phase 1: volume only)

deploy/
  install.sh                    Idempotent installer (run as root on Pi)
  alsa/                         /etc/asound.conf template
  camilladsp/                   main v1.yml + outputd-cutover.yml baselines
  systemd/                      jasper-{camilla,voice,control,mux,outputd,aec-bridge,aec-init}
                                + librespot, shairport-sync, nqptp, bt-agent
  modules-load.d/               snd-aloop autoload
  modprobe.d/                   snd-aloop single-card config
  bin/                          jasper-librespot-event (--onevent hook)
  configure-bluez.sh            Speaker-mode pairing config
  shairport-sync.conf           AirPlay 2 receiver config
  index.html                    Static landing page
  assets/fonts/                 Local web fonts for static management UI
  correction-preflight.html     HTTP warning before HTTPS room correction
  integrations.html             Static integrations sub-page
  nginx-jasper.conf             Main nginx site: HTTP wizards + HTTPS correction UI

docs/                           Subsystem deep-dives ("HANDOFF" docs)
  HANDOFF-wake-training-experiment.md  Primary active workstream: custom wake-model training
  HANDOFF-wake-corpus-quality.md  Methodology for wake-corpus audio QA / artifact review
  HANDOFF-usb-mic-wake.md   Parked cheap-USB mic wake/AEC follow-up plan
  HANDOFF-mic-quality-v2.md     Empirical history: AEC sweeps, BEST_A, triple-stream architecture
  HANDOFF-mic-fusion-architecture.md  Design/plan (draft): pluggable-mic boundary + N-leg wake fusion
  HANDOFF-vad-experiments.md    Active workstream: VAD/mic-stream A/B matrix, why Cell 0 wins, raw+AGC followup
  HANDOFF-aec.md                Acoustic echo cancellation engine
  HANDOFF-speaker-output-reference.md  Chosen output-owner / true speaker-reference direction
  HANDOFF-wake-telemetry.md     Triple-stream wake + per-event SQLite + funnel
  HANDOFF-xvf3800.md            Canonical reference for the XVF3800 mic
  HANDOFF-airplay.md       AirPlay glitch troubleshooting guide
  HANDOFF-apple-music.md   Apple Music integration research + plan (no code yet)
  HANDOFF-peering.md            Multi-Pi wake arbitration (off by default)
  HANDOFF-persistent-live-session.md
  HANDOFF-voice-music-control.md
  HANDOFF-volume.md             Source-aware volume coordinator
  multi-user-spotify.md
  audit-pending-followups.md    Open Tier 2/3 follow-ups

scripts/                        Operator helpers (run from laptop)
  fetch-pi-logs.sh              Pull journals, reboot/OOM forensics,
                                and configs into ./logs/
  pi-run-diagnostic.sh          Run ad-hoc Pi diagnostics in a bounded
                                transient systemd unit
  tail-pi-logs.sh               Live tail
  pi-bundle.sh                  One-shot diagnostic dump
  xvf-interrogate.sh            Deep XVF3800 diagnostic — captures
                                everything (USB, ALSA, params, RMS)
                                tagged by chip iSerial. See HANDOFF-xvf3800.md.
  switch-voice-provider.sh      Flip JASPER_VOICE_PROVIDER between
                                gemini / openai / grok
  switch-gemini-model.sh        Within-Gemini fallback: 3.1 ↔ 2.5
  disable-outputd-cutover.sh    Stop persistent outputd unit before/after
                                rolling this cutover branch back to main
  claim-librespot.sh            One-time: OAuth-claim librespot for a
                                Spotify account so cold-start "play X"
                                works without phone interaction

tests/                          Hardware-free pytest suite
  voice_eval/                   End-to-end scenario tests against
                                the live voice provider. Paid API
                                calls — see AGENTS.md "Voice-eval
                                cost discipline" before running.
```

---

## Onboarding pattern (Apache 2.0 — fork this for your own project)

The setup flow we landed for JTS is generalizable. Four pieces:

1. **`.claude/commands/onboard-pi.md`** — a Claude Code skill with a
   pushy auto-trigger description. Body lays out phases (hardware
   sanity → Pi Imager → flash → boot → install → configure) with a
   one-question-per-turn discipline and front-loaded anti-pattern
   warnings. Pre-flight LAN probe handles multi-instance collision
   avoidance before the user picks a hostname.
2. **`scripts/onboard.sh`** — the deterministic shell side. Idempotent.
   Probes reachability, persists state, calls into the existing
   `deploy-to-pi.sh`, validates with `jasper-doctor`. Emits
   structured `event=onboard.<phase> status=<s>` lines parallel to
   the Pi-side daemon logging convention.
3. **`scripts/_lib.sh`** — shared header. Sources `.env.local`,
   exports `PI_HOST`/`PI_USER` with a documented fallback chain,
   exposes a `write_laptop_state` helper so the onboarder and the
   `use` switcher stay in template-sync.
4. **`CLAUDE.local.md`** (gitignored, written by the onboarder) —
   loaded via `CLAUDE.md`'s `@`-import so every Claude Code session
   in the checkout automatically knows which Pi is active. Includes
   a behavioral directive (*"prefer `ssh <alias>` over inline
   `user@host`"*) that fixes "Claude reinvents the connection method
   every session" at the prompt layer rather than the script layer.

The split: **deterministic shell does what must be reliable; Claude
does what benefits from intelligence** (discovery, troubleshooting,
recovery, multi-instance reasoning). This is opposite to PostHog's
[wizard](https://github.com/PostHog/wizard) (Claude Agent SDK in a
standalone CLI with hosted LLM gateway) because the user is already
in Claude Code when they start — we don't need to host an agent;
we are one. The pattern composes for any project where setup
involves (a) external hardware/software prerequisites, (b)
multi-instance collision concerns, and (c) idempotent install
steps. Apache 2.0 like the rest of the repo.

---

## Documentation map

| File | Audience | Purpose |
|---|---|---|
| [README.md](README.md) | Anyone landing on the repo | What this is, where to look |
| [AGENTS.md](AGENTS.md) | All AI agents (canonical) | Operational rules + per-subsystem guidance + documentation paradigm. Edit here. |
| [CLAUDE.md](CLAUDE.md) | Claude Code only | Thin import shim (`@AGENTS.md` + per-checkout `@CLAUDE.local.md`). Don't edit; AGENTS.md is canonical. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | First-time contributors | Quick start, PR flow, testing, doc layout |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | All contributors | Contributor Covenant 2.1 |
| [SECURITY.md](SECURITY.md) | Security reporters / maintainers | Supported versions, vulnerability reporting path, current LAN-appliance security model |
| [LICENSE](LICENSE) | Anyone redistributing | Apache 2.0 |
| [NOTICE](NOTICE) | Anyone redistributing | Project notice plus pointer to third-party attribution inventory |
| [LICENSE-third-party.md](LICENSE-third-party.md) | Redistributors / maintainers | First-pass third-party software, asset, model, and data attribution inventory |
| [QUICKSTART.md](QUICKSTART.md) | First-time speaker builder | Pi Imager → boot → `scripts/onboard.sh` → working speaker in ~30 min. Imager 2.0.6+ required. |
| [BRINGUP.md](BRINGUP.md) | Operator flashing a fresh Pi | Step-by-step from blank SD card to working speaker — XVF firmware, dial, satellites, calibration |
| [PLAN.md](PLAN.md) | Project planning | v1 phased build, future roadmap |
| [docs/OSS-READINESS-TOP-FIVE.md](docs/OSS-READINESS-TOP-FIVE.md) | Maintainers / OSS reviewers | Living top-five OSS-readiness worklist, hotspot register, software-only dev-path notes, and deliberate deferrals |
| [docs/REVIEW-google-oss-readiness.md](docs/REVIEW-google-oss-readiness.md) | Maintainers / OSS reviewers | Historical point-in-time OSS-readiness review; not current operational truth |
| [docs/audio-paths.md](docs/audio-paths.md) | Operator + AI | Reference: the two ALSA paths to the dongle, which volume knob attenuates which path, how end-of-turn timing anchors on TTS drain, and the canonical checklist for adding a new music source |
| [docs/HANDOFF-speaker-output-reference.md](docs/HANDOFF-speaker-output-reference.md) | Audio / voice architects | Chosen direction for a JTS-native output owner, true speaker-output reference, TTS playout ledger, and robust assistant-speech barge-in |
| [docs/satellites.md](docs/satellites.md) | Anyone working on a satellite device | Cross-cutting design + roadmap for ESP32 satellites (dial, AMOLED mic, etc.) |
| [docs/HANDOFF-supply-chain.md](docs/HANDOFF-supply-chain.md) | Maintainers / release engineers | Canonical provenance policy for deploy/build-time third-party inputs, checksum expectations, and accepted gaps |
| [docs/testing-tooling.md](docs/testing-tooling.md) | Anyone writing a test/measurement script | Index of every capture / wake-word-scoring / forensic / diagnostic tool in the repo. **Read before writing a new one** — many parallel tools have been built before this index existed. |
| [docs/doc-map.toml](docs/doc-map.toml) | Maintainers / AI agents | Documentation impact routing: code globs → canonical docs to scan, safety class, and suggested verification. Used by `scripts/docs-impact.py` and the non-blocking PR comment workflow. |
| [docs/audit-pending-followups.md](docs/audit-pending-followups.md) | Maintainers | Deferred/rejected follow-ups from the May 2026 architectural-pattern audit |
| [docs/historical/](docs/historical/) | Maintainers / archaeology | Completed or superseded runbooks preserved for context; not current operational truth |
| [docs/research/](docs/research/) | Maintainers / archaeology | Raw external or model-generated research inputs preserved for traceability; use canonical handoffs for shipped guidance |
| [docs/HANDOFF-*.md](docs/) | Deep-dive on a subsystem | Investigation history + design rationale |

The HANDOFF docs are the most engineer-relevant. Each one is the
canonical "if you're modifying this subsystem, read this first"
reference. Currently:
- [`HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md) —
  Multi-provider voice loop architecture: how `LiveConnection` /
  `LiveTurn` abstract Gemini Live, OpenAI Realtime, and Grok Voice
  Agent behind one switch, plus the per-provider trade-offs and the
  steps for adding a fourth backend
- [`HANDOFF-prompting.md`](docs/HANDOFF-prompting.md) — The voice
  prompting playbook. Cross-provider principles (conditional over
  absolute, positive framing for tool calls, brevity vs. structure),
  provider deltas (OpenAI gpt-realtime-2 / Gemini 3.1 Flash Live /
  Grok think-fast-1.0), a section-by-section walk-through of the
  current `SYSTEM_INSTRUCTION`, a tool-prompt cookbook including
  the `build_tool()` first-paragraph truncation, a pitfalls
  catalog with symptoms, and a "Recommended edits to current code"
  punch list. **Start here for any edit to `SYSTEM_INSTRUCTION` in
  `jasper/voice_daemon.py` or any tool description in
  `jasper/tools/`.** Refreshed against provider docs 2026-05-23.
- [`satellites.md`](docs/satellites.md) — The home base for the
  satellite-device family. Existing dial + planned AMOLED mic
  satellite, shared protocols (Improv / mDNS-SD / control HTTP / UDP
  logs), and the multi-mic-around-one-Pi arbitration design (with
  prior-art survey across HA Assist, Sonos, Apple, Amazon ESP).
- [`HANDOFF-peering.md`](docs/HANDOFF-peering.md) — Multi-Pi wake
  arbitration. When a household runs multiple JTS speakers on the
  same LAN, peering picks exactly one winner per wake event so they
  don't all answer at once. Off by default; user flips it on at
  `http://jts.local/peers/`. P2P via mDNS-SD + multicast UDP, no
  hub, no SPOF. **Start here for `jasper/peering/`, the wake-handler
  restructure, or anything related to the `/peers/` wizard.**
- [`HANDOFF-aec.md`](docs/HANDOFF-aec.md) — AEC architecture +
  investigation (engine: why software AEC, why not chip AEC)
- [`CHIP-AEC-EXPERIMENT.md`](docs/CHIP-AEC-EXPERIMENT.md) —
  **Shelved indefinitely.** Not on the roadmap, no active work.
  Infrastructure preserved on `main` as a user-authorized carve-out
  from the AGENTS.md "Architecture is fixed; swap the engine, not
  the topology" rule, in case we ever revive the chip-AEC
  convergence question ([HANDOFF-aec.md](docs/HANDOFF-aec.md)
  Option D). The four `scripts/chip-aec-*.sh` scripts +
  `jasper/chip_aec_experiment.py` are dormant until a human opts
  in via `bash scripts/chip-aec-setup.sh`; `chip-aec-teardown.sh`
  fully reverts. **Read the doc before running.** The carve-out
  is scoped narrowly — does not re-open PipeWire `module-echo-
  cancel`, dual-USB-sink, or custom firmware elsewhere.
- [`HANDOFF-mic-quality-v2.md`](docs/HANDOFF-mic-quality-v2.md) —
  Active workstream. The sequencing + lever inventory + decision
  history for getting the mic to work reliably across whisper /
  yell / fast / slow / music / silence. **Read this first when
  picking up mic-quality work in a fresh session.** Cross-refs
  HANDOFF-aec.md (engine internals) + HANDOFF-wake-telemetry.md
  (measurement infrastructure already deployed) so this doc stays
  short on what's documented elsewhere.
- [`HANDOFF-mic-fusion-architecture.md`](docs/HANDOFF-mic-fusion-architecture.md) —
  **Design/plan (living draft, updated as phases land).** Architecture for
  the pluggable-mic boundary + the leg-count-agnostic wake-fusion layer:
  the `wake_legs` registry, the `CaptureProfile` / `LegRuntime` seam, and
  the staged PR plan (Phase 0 → 5). Companion to HANDOFF-mic-quality-v2.md
  (empirical tuning) and HANDOFF-wake-telemetry.md (schema). Read for the
  boundary design + phase sequencing.
- [`HANDOFF-wake-training-experiment.md`](docs/HANDOFF-wake-training-experiment.md) —
  **Current primary workstream (2026-05-26).** The forward-looking
  plan for training a custom `jarvis_jts_*_v1` wake-word model
  matched to the JTS audio chain, replacing the community
  `jarvis_v2` model (published recall 26%). Sequenced phases
  (−1 → 0 → 1 → 2 → 3), pre-committed failure criteria, five
  explicit listening checkpoints. Capture tooling shipped end-to-
  end via the browser recorder at http://jts.local/wake-corpus/
  (PRs #303 → #323, plus the 2026-05-26 USB/ref/DTLN follow-up) with
  a 4th `raw0` leg and corpus-only cheap USB mic/reference/DTLN legs
  for future cheaper-mic experiments. Read this before working on wake-
  word reliability, training data collection, or testing methodology.
- [`HANDOFF-wake-corpus-quality.md`](docs/HANDOFF-wake-corpus-quality.md) —
  Current methodology for programmatic audio-quality review of the
  deliberate wake corpus: deterministic artifact metrics, tear/click
  detection, clipping and AGC diagnostics, cross-leg comparison, scoring
  schema, and human review packages. Read this before building or
  expanding wake-corpus quality analyzers.
- [`HANDOFF-usb-mic-wake.md`](docs/HANDOFF-usb-mic-wake.md) —
  Parked follow-up for making the cheap USB mic path competitive after
  the XVF3800 AEC tuning round is settled: delay/alignment hypotheses,
  offline ref-delay sweep plan, hardware-processing checks, and
  corpus-only guardrails.
- [`HANDOFF-vad-experiments.md`](docs/HANDOFF-vad-experiments.md) —
  Active workstream (May 2026). The VAD / mic-stream A/B test matrix:
  why local Silero on the AEC stream (Cell 0) is the production default,
  why server VAD configurations all failed in different ways (wake-word
  interference, threshold cliff, sentence-cutting), and the open
  hypothesis that a raw mic stream with real WebRTC AGC1 may be the
  ultimate answer once ducking is doing its job. **Read this first when
  touching `_SimpleAGC` in `aec_bridge.py`, the server-VAD code path in
  `voice_daemon.py`, or `set_turn_detection` in `openai_session.py`.**
  Also documents the new debug-WAV recording instrumentation
  (`JASPER_DEBUG_RECORD_OPENAI_AUDIO`).
- [`HANDOFF-barge-in.md`](docs/HANDOFF-barge-in.md) —
  Historical costing record for robust barge-in options under the
  earlier measure-first policy. Useful archaeology, but superseded
  as the current recommendation by
  [`HANDOFF-speaker-output-reference.md`](docs/HANDOFF-speaker-output-reference.md).
- [`HANDOFF-speaker-output-reference.md`](docs/HANDOFF-speaker-output-reference.md)
  — Chosen architecture direction for moving from today's split
  music/TTS output paths to a JTS-native output owner that publishes
  a true `speaker_output_reference`, owns TTS/cue playout accounting,
  and enables robust barge-in during assistant speech.
- [`HANDOFF-wake-telemetry.md`](docs/HANDOFF-wake-telemetry.md) —
  Triple-stream wake-word detection (AEC ON + AEC OFF + DTLN, OR-gated)
  plus SQLite-backed per-event telemetry with audio capture and
  funnel tracking through to LLM response / tool call. Replaces
  the synthetic phone-track wake-rate methodology with real
  production-attempt data. Read for the schema, the per-PR
  staging plan, and the design decisions (no real-time labelling,
  no "I just said Jarvis" button, OR-gate fires immediately).
- [`HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md) — Canonical
  reference for the Seeed ReSpeaker XVF3800 (USB UA) microphone:
  hardware identity, firmware variants, full parameter space, DFU
  flow, documented failure modes (notably the post-firmware-flash
  ALSA mute trap), diagnostic cookbook. Start here for any
  mic-side investigation.
- [`HANDOFF-resilience.md`](docs/HANDOFF-resilience.md) — The
  resilience ladder: Tiers 1–3 (sd_notify per-daemon watchdog +
  shairport supervisor), Stage 1 memory-pressure prevention
  (OOMScoreAdjust ladder + MGLRU + zram + sysctls + RAM-aware
  defaults), T5.1 `StartLimitAction=reboot` escalation, T5.2
  `SystemSupervisor` userspace-liveness probing, and the Tier 5
  kernel hardware watchdog with persistent journal forensics. The
  2026-05-11 and 2026-05-23 incidents that drove each addition.
  Read before touching `jasper/watchdog.py`,
  `jasper/control/{shairport,system}_supervisor.py`, or the
  `Type=notify` / `WatchdogSec=` / `StartLimitAction=` blocks in
  any service unit.
- [`HANDOFF-tier5-watchdog-liveness.md`](docs/HANDOFF-tier5-watchdog-liveness.md) —
  Design + shipped implementation (T5.1 + T5.2, May 2026) for
  closing the Tier 5 liveness gap exposed by the 2026-05-23
  incident. Industry survey (HAOS, balenaOS, OpenWrt, Meskes
  `watchdog`), option matrix (probing system supervisor /
  `StartLimitAction=reboot` / shorter `RuntimeWatchdogSec` /
  external hardware / PSI gate), sequencing rationale, and
  revisit triggers for the still-deferred T5.3–T5.5 options.
  Read before any further work on Tier 5.
- [`HANDOFF-homeassistant.md`](docs/HANDOFF-homeassistant.md) —
  Smart-home integration. The speaker delegates "turn on the
  bedroom lights" / "good night" / household sentence triggers
  to whatever Home Assistant the user has on the LAN, via HA's
  REST conversation API (not MCP — covered in the doc with
  primary-source citations: HA's MCP server has no
  `automation.trigger` tool, and sentence triggers only fire
  through HA's conversation pipeline). Wizard at
  `http://jts.local/ha/`. **Start here for
  `jasper/home_assistant.py`, the `home_assistant` voice tool,
  or anything related to the `/ha/` wizard.**
- [`HANDOFF-transit-citibike.md`](docs/HANDOFF-transit-citibike.md) —
  Citi Bike (NYC + Jersey City + Hoboken) bikeshare integration via
  the GBFS open standard. Architecture (sync provider + sync runtime
  client + async tool wrapper), the in-process 30 s / 1 h TTL cache
  with stale-on-error semantics, the household-wide e-bike-only flag,
  station-drift handling (Lyft retires stations periodically), and
  the survey of prior art (Alexa skills, Home Assistant CityBikes,
  Raycast extension, citibike.live). **Start here for
  `jasper/citibike.py`, `jasper/transit/providers/citibike.py`, the
  `get_citibike_status` voice tool, or the Citi Bike card in the
  `/transit/` wizard.**
- [`HANDOFF-apple-music.md`](docs/HANDOFF-apple-music.md) —
  Research only, no implementation yet. Feasibility analysis for
  adding Apple Music as a voice-controllable source: why no
  librespot equivalent exists, the Music Assistant Widevine L3
  streaming pipeline (the only proven headless approach), the
  Cider RPC alternative (requires Mac), the chosen Path C
  (vendor MA's streaming code, own everything else), sequenced
  build plan, and the prior-art survey across Cider, pyatv,
  Volumio, Sonos SMAPI, etc. **Start here before any
  `jasper/apple_music/` work.**
- [`HANDOFF-remote-updates.md`](docs/HANDOFF-remote-updates.md) —
  Research only, no implementation yet. Design space for an OTA
  "Check for updates" button on the management dashboard: option
  survey (`git pull` → GitHub Releases + poll → RAUC A/B
  partition swap), recommended staged build-out (CI first,
  auto-release, then the button), and the open questions before
  specing. Referenced from PLAN.md.
- [`HANDOFF-persistent-live-session.md`](docs/HANDOFF-persistent-live-session.md)
  — **Historical** (per AGENTS.md rule #9). Frozen-in-time
  session-pickup brief from 2026-05-05 when the persistent-single
  Gemini Live rework was scoped. Preserved for primary-source
  archaeology; do NOT read for current state. Current operational
  truth: [HANDOFF-voice-providers.md](docs/HANDOFF-voice-providers.md).
- [`HANDOFF-voice-music-control.md`](docs/HANDOFF-voice-music-control.md)
  — Source-aware transport (AirPlay/Spotify Connect) + volume
- [`HANDOFF-volume.md`](docs/HANDOFF-volume.md) — Source-aware
  volume coordinator (one canonical `listening_level`, dispatched
  to whichever source is active, observed inbound at 1 Hz)
- [`HANDOFF-source-capabilities.md`](docs/HANDOFF-source-capabilities.md)
  — Planned provider/source capability boundary for future music
  integrations: volume, transport, metadata, health, and contributor
  checklist
- [`HANDOFF-airplay.md`](docs/HANDOFF-airplay.md) — AirPlay
  glitch troubleshooting guide. **Start here if you hear audio
  artifacts on AirPlay.** Symptom → pattern decision flow, concrete
  diagnostic recipes, per-pattern playbooks (with confirmed fixes for
  the patterns we've seen), the source-cited first-principles
  reference, what's been tried, and an escalation ladder for new
  scenarios. Patterns currently fixed: CamillaDSP rate_adjust +
  AsyncSinc oscillation (PR #75), shairport `resync_threshold`
  misfire on snd-aloop fill (PR #83), renderer-side dmix buffer
  invisible to shairport's latency model (PR #308), and the
  WiFi-burst × dmix write-timing interaction fixed by the fan-in
  topology (PR #329).
- [`HANDOFF-fan-in-daemon.md`](docs/HANDOFF-fan-in-daemon.md) —
  Production fan-in renderer topology: each renderer gets its own
  snd-aloop substream pair; the Rust `jasper-fanin` daemon sums the
  capture sides into substream 7, which both CamillaDSP and the AEC
  bridge dsnoop. Covers buffer sizing (`4096` frames for WiFi-burst
  absorption), systemd resilience, observability, and the retired
  dmix failure mode.
- [`HANDOFF-supply-chain.md`](docs/HANDOFF-supply-chain.md) —
  Deploy/build provenance: the canonical manifest, checksum policy,
  install-time git/source pins, firmware dependency pins, hash-checked
  model downloads, and accepted gaps for apt, Python, and PlatformIO
  transitive resolution.
- [`HANDOFF-usbsink.md`](docs/HANDOFF-usbsink.md) — Optional USB
  audio-input gadget: ConfigFS setup, host-control preemption,
  source wizard behavior, and how the USB-in lane feeds fan-in.
- [`HANDOFF-audible-feedback.md`](docs/HANDOFF-audible-feedback.md) —
  Pre-rendered audio cue subsystem: registry, cache lifecycle, CLI,
  how to add a new reactive or proactive cue. Start here when a
  failure path needs to "say something" rather than fall silent.
- [`HANDOFF-correction.md`](docs/HANDOFF-correction.md) — Room
  correction v2 at `/correction/`: iPhone-mic measurement flow,
  calibrated mic ingest, configurable correction strategies,
  design-audit bundles, replay-grade analysis artifacts,
  `jasper-correction-bundle` inspect/export/FIR-inspect tooling, PEQ
  generation, CamillaDSP hot-swap. Active workstream — read the Status
  section first to see which phase is in flight.
- [`HANDOFF-sound-preferences.md`](docs/HANDOFF-sound-preferences.md)
  — `/sound/` preference-EQ layer: Off / Saved / Draft live source,
  stock curves, five-band Simple EQ + exclusive PEQ editing, named custom
  profile library, room-correction composition order, generated config
  ownership, durable apply + live-draft semantics, doctor and
  `/state` observability, and the future AI boundary.
- [`HANDOFF-calibration-agent.md`](docs/HANDOFF-calibration-agent.md) —
  **Research + early substrate** (2026-05-25). Proposal
  for a guided speaker-tuning system layered on top of
  `/correction/`: calibrated mic ingest (Dayton/miniDSP serial lookup
  plus manual upload fallback), richer measurement bundles,
  FIR/target-curve research corpus, read-only
  `jasper-calibration-agent` bundle-intake tooling, and eventually an
  LLM "audio engineer" that critiques the auto-filter, explains
  trade-offs, and iterates across re-measurements. Also captures the
  longer-term preference-tuning vision: voice entry point, user feedback like
  "more bass" / "brighter," and safe reversible EQ layered separately
  from room correction.
- [`HANDOFF-active-speaker-dsp.md`](docs/HANDOFF-active-speaker-dsp.md)
  — Active speaker DSP / crossover commissioning workstream
  planning baseline (2026-05-25, updated 2026-05-26). Canonical
  handoff for future JTS hardware where CamillaDSP directly drives
  woofer/mid/tweeter amplifier channels: speaker-baseline layer,
  strict room-correction/preference separation, 2-way/3-way preset
  model, safe bring-up, channel-map hazards, TTS/cue bypass risk,
  near-field/null-depth/gated measurement triad, LR/IIR-first default,
  and delay/null verification.
- [`docs/calibration-agent/`](docs/calibration-agent/README.md) —
  Calibration/tuning knowledge corpus:
  measurement-quality guidance, FIR research landing zone,
  active-speaker DSP / crossover notes, preference-EQ vocabulary,
  house-curve notes, and the public schema for private runtime
  context.
- [`docs/research/2026-05-25-calibration-agent/`](docs/research/2026-05-25-calibration-agent/README.md)
  — Verbatim raw deep-research artifacts that seeded the
  room-correction, FIR, and active-speaker DSP handoffs. Treat as
  source material, not operational truth.
- [`docs/research/mic-quality-v2-report.md`](docs/research/mic-quality-v2-report.md)
  — Verbatim external calibration-wizard report preserved because
  `HANDOFF-mic-quality-v2.md` references it. Treat as source material,
  not operational truth.
- [`docs/research/2026-05-27-room-correction-research/`](docs/research/2026-05-27-room-correction-research/README.md)
  — Verbatim raw reports plus one synthesis per topic for mobile
  browser audio reliability, target/preference tuning, FIR/phase room
  correction, and multi-position confidence. Treat as source material
  and research synthesis, not operational truth.
- [`HANDOFF-management-ui.md`](docs/HANDOFF-management-ui.md) —
  Proposal (created 2026-05-22, not yet implemented) for
  restructuring the `jts.local` management surface into a tighter
  layout with a first-run setup wizard.
- [`HANDOFF-volume-control-redesign.md`](docs/HANDOFF-volume-control-redesign.md)
  — **Superseded** (2026-05-14) — historical record of why AirPlay
  receiver-originated volume reflection didn't work. Keep for
  the next person who's tempted to retry that path.
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

The rejection above was for the variants we tested — none of
them fed music to the chip's USB-IN as the AEC reference. **One
chip-AEC variant remains untested**: option D in
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md) — feed music back
into the chip's USB-IN as the reference signal, then read its
hardware-AEC'd mic stream. The chip's USB Adaptive Mode means
mic and reference would share a clock, avoiding the cross-clock
drift that's typically fatal for chip AEC in split-DAC topologies.
We built [the infrastructure to test it](docs/CHIP-AEC-EXPERIMENT.md)
but have **shelved** the experiment indefinitely — software AEC3
is good enough today, and resolving Option D would take focused
hours of speaker downtime that aren't currently justified. The
infrastructure stays in the repo so we don't have to re-derive
the question if AEC3 ever plateaus.

The chip is still useful — its **beamforming, noise suppression,
and AGC** all run on the ASR beam channel (channel 1 of the
USB capture endpoint). We use that processed channel; just not
the chip's on-chip AEC.

**Software AEC ships ON by default when the chip is on the 6-channel
firmware variant.** A Python daemon (`jasper-aec-bridge`) runs
WebRTC AEC3 echo cancellation between the host's music chain
(tapped via `pcm.jasper_capture` dsnoop) and the chip's ASR beam
(channel 1 of the 6-channel firmware). It sends an AEC'd mono signal
over UDP localhost (`127.0.0.1:9876`) to `jasper-voice`'s
`UdpMicCapture` instead of the chip's processed mic. The engine is
the `jasper_aec3` pybind11 binding around Trixie's
`libwebrtc-audio-processing-1` v1.3-3 — delivers −15 to −18 dB on
music with the production REF_GAIN/MIC_GAIN tunings, at ~3-8% of
one Pi 5 core and ~95 MB RAM.

**Wake detection runs as up to three OR-gated layers** the user
controls from the `/system/` Wake detection card: AEC3 (the
master, always-on when the bridge is up), plus two additive
sub-layers. Defaults out of the box: AEC3 on, raw chip-direct on
(dual-stream — cheap, ~5 MB), DTLN neural off (heavy, ~75 MB / ~25%
one core — opt-in for 2 GB Pis). Toggle any of them at
[http://jts.local/system](http://jts.local/system); the reconciler
restarts the bridge + voice and the change takes effect in ~15 s.
Sensitivity slider lives on the same card. Full lever set in
[AGENTS.md "AEC bridge — reconciler toggle"](AGENTS.md#aec-bridge--reconciler-toggle).

The transport is UDP (not snd-aloop's `LoopbackAEC` card, which is
what the original design used) because snd-aloop's kernel-side
`loopback_cable` wedges when a consumer is SIGKILL'd, requiring a
reboot to clear. Hit in production May 2026; UDP localhost has no
kernel state to corrupt and `sendto()` is non-blocking. See
[docs/HANDOFF-resilience.md](docs/HANDOFF-resilience.md) for the
full architectural rationale and the multi-tier resilience design
the speaker now uses.

The bridge needs the **6-channel XVF firmware variant** because it
opens the 6-channel USB capture endpoint and reads the ASR beam on
channel 1; the 2-channel firmware Seeed ships by default does not
match that capture shape. As of
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
| `jasper-wifi-guardian` (boot-time NM keyfile self-heal) | Active (oneshot) | one-shot, ~0 | ~3-5 ms |
| `jasper-camilla` (always-on CamillaDSP, ducking) | Active | ~12 MB | <1% |
| `jasper-control` (HTTP API + dial routing) | Active | ~35 MB | ~0.1% idle |
| `jasper-input` (HID accessory bridge) | Active | ~28 MB | ~0% idle |
| `jasper-mux` (renderer arbitration) | Active | ~13 MB | ~0% idle |
| `jasper-usbsink` (USB audio source) | **Disabled by default**, ~22 MB when on | 0 MB off, ~22 MB on | ~3% of one core while host streams |
| `jasper-usbsink-init` (gadget ConfigFS oneshot) | follows usbsink | one-shot, ~0 | ~0 |
| `jasper-web` (Spotify / voice / Google / AirPlay / Sources / Wake / Wi-Fi / Peers / Transit / Home Assistant / Weather / Sound wizards) | **Socket-activated** | ~0 idle, ~22 MB when open | n/a idle |
| `jasper-bluetooth-web` (BT pair UI) | **Socket-activated** | ~0 idle, ~17 MB when open | n/a idle |
| `jasper-correction-web` (room correction UI) | **Socket-activated** | ~0 idle, ~15 MB when open | n/a idle |
| `jasper-dial-web` (dial onboarding UI) | **Socket-activated** | ~0 idle, ~9 MB when open | n/a idle |
| `jasper-system-web` (system dashboard at `/system/`) | **Socket-activated** | ~0 idle, ~12 MB when open | n/a idle |
| Single-card snd-aloop (Loopback) | Loaded at boot | ~0 | ~0 |
| dsnoop tap on music chain | Always present | ~0 | ~0 |

The five web-wizard daemons are socket-activated — systemd holds
their ports open and only spawns the daemon when a tab opens any of
its pages. `jasper-web` alone hosts thirteen URL surfaces (Spotify,
voice, Google, AirPlay, Sources, Wake, Wi-Fi, Peers, Transit, Home
Assistant, Weather, Sound, Wake-Corpus) on thirteen loopback ports; the
other four daemons each host one. All five exit after 10 min of no
requests, so the resident cost is zero between
admin sessions. First request after idle takes ~500-800 ms (Python
startup); invisible during the OAuth round-trip or BT pair flow.

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
[the Acoustic echo cancellation section](#acoustic-echo-cancellation-aec).

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

- **Voice loop** ([HANDOFF-voice-providers.md](docs/HANDOFF-voice-providers.md))
  — Long-lived Gemini Live / OpenAI Realtime / Grok connection
  with manual VAD, `activity_start`/`activity_end` markers,
  sustained-speech detection. The choice of manual VAD over
  server-side auto VAD is empirically derived (auto VAD silently
  drops turn 2 on a paused-resumed connection). Original rework
  rationale (archaeology only):
  [HANDOFF-persistent-live-session.md](docs/HANDOFF-persistent-live-session.md).
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
bash scripts/pi-run-diagnostic.sh -- <command>    # bounded Pi-side probe
bash scripts/tail-pi-logs.sh                      # live tail all units
bash scripts/jasper-trace.sh                      # filter to event= lines
```

`jasper-doctor` codifies the smoke tests in BRINGUP.md and runs
them as code. `fetch-pi-logs.sh` pulls journals, previous-boot
OOM/watchdog/reboot clues, configs, and ALSA state into `./logs/`,
redacting environment-style secret assignments before writing
snapshots to disk. `pi-run-diagnostic.sh` is the safe path for
ad-hoc Pi-side experiments: it wraps the command in a transient
systemd unit with memory/runtime bounds so a bad diagnostic gets
killed before it starves the speaker.
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

README does not carry a detailed deferred-work list; those age quickly
as features ship. Use the living sources instead:

- [docs/OSS-READINESS-TOP-FIVE.md](docs/OSS-READINESS-TOP-FIVE.md)
  for current OSS-readiness priorities, the refactor hotspot register,
  the software-only development path, and deliberate deferrals.
- [PLAN.md](PLAN.md) for product roadmap items and small test/dev
  follow-ups that are not tied to a release.
- The relevant `docs/HANDOFF-*.md` file for subsystem-specific
  rejected paths, revisit triggers, and implementation history.
