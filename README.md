# JTS — the Jasper Tech Speaker

JTS is the **J**asper **T**ech **S**peaker — the debut build from the
[Jasper Tech](https://www.youtube.com/@Jasper_Tech) YouTube channel.

A custom voice-controlled smart speaker on a Raspberry Pi 5 running
Raspberry Pi OS Lite Trixie, with
[CamillaDSP](https://github.com/HEnquist/camilladsp) for audio. It is a
music streamer that is also a voice assistant, built from open hardware
and open audio software. The voice loop is provider-agnostic: any of
three real-time speech-to-speech APIs can drive it via a single env-var
switch —
[Gemini Flash Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview),
[OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime),
or [xAI Grok Voice Agent](https://docs.x.ai/docs/guides/voice/agent)
(`jasper/voice/{gemini,openai,grok}_session.py`). This is a personal
hobby project; not a product.

Privacy: [PRIVACY.md](PRIVACY.md) covers cloud egress, local retention,
voice-assistant pause scope, and the USB microphone export.

**Want to set one up?**

- **Using Claude Code?** Open this repo and say *"I want to set up a JTS
  speaker"*. Claude reads the [`/onboard-pi`](.claude/commands/onboard-pi.md)
  skill and walks through Raspberry Pi Imager, flash, first boot, network
  discovery (including multi-speaker collision detection), and install.
- **Prefer to read the steps?** [QUICKSTART.md](QUICKSTART.md) is the same
  flow as a human-readable walkthrough.
- **Doing the full long-form bringup** (hardware calibration, XVF firmware
  flashing)? See [BRINGUP.md](BRINGUP.md).

The setup docs default to the hostname `jts`, which becomes `jts.local` on
your home network. If you choose another hostname in Imager, such as
`jts3`, use `jts3.local` everywhere later.

---

## Hardware

| Component | Role |
|---|---|
| Raspberry Pi 5 (1GB or 2GB; **2GB recommended**) | Host |
| Apple USB-C → 3.5mm dongle | DAC for the speaker output (48 kHz, simple UAC2) |
| TPA3255 class-D amp + 32V supply | Speaker power |
| Speakers + speaker wire | (Whatever you have) |
| Seeed ReSpeaker XVF3800 (USB UA variant) | 4-mic array with on-chip XMOS DSP |

The Apple dongle above is the reference DAC; see the DAC section of
[docs/audio-paths.md](docs/audio-paths.md) for the full list of supported
output DACs and how to add one.

The XVF3800's onboard 3.5mm jack / AIC3104 codec is **not** connected —
speakers go to the Apple dongle. That non-standard choice is what drives
most of the AEC complexity below.

A Raspberry Pi Zero 2 W can run the `streambox` install profile: local
renderers, outputd/CamillaDSP, and the capability-filtered landing page,
but no local wake word or mic/AEC. A paired remote with a microphone enables
push-to-talk assistant use; see the [microphone reference](jasper/mics/README.md).

---

## Architecture

Audio path — the canonical reference is
[`docs/audio-paths.md`](docs/audio-paths.md):

```
Phone (AirPlay / Spotify Connect / BT)      Computer (USB audio)
        │                                          │
        ▼                                          ▼
  shairport-sync (AirPlay 2)              jasper-usbsink
  librespot (Spotify Connect)             (UAC2 gadget)
  bluealsa-aplay (BT A2DP)
        │                                          │
        │ private snd-aloop lanes: hw:Loopback,0,0..4
        ▼                                          ▼
  hw:Loopback,1,0..4  ──►  jasper-fanin ◄── /run/jasper-fanin/tts.sock
                              │ sums active renderer/test lanes + TTS
                              │ applies program duck before TTS mix
                              ▼
                       Ring A (program.ring)
                              │
                              ▼
                    jasper-camilla (CamillaDSP, port 1234)
                    - main_volume (listening level / source volume)
                    - crossover / correction / protection profile
                              │
                              ▼
                       Ring B (content.ring), or the ACTIVE ring
                       (active-content.ring) on a roleful box
                              │
                              ▼
                    jasper-outputd (final output owner)
                    - writes post-DSP content to the selected sink
                    - publishes runtime health / xrun counters
                              │
                              ▼
                    outputd_dac → Apple USB-C dongle or DAC8x → amp
```

```
  XVF3800 4-mic array  ── USB UAC2 ──  hw:CARD=Array,DEV=0
        │                                     │
        │                                     ▼
        │                            jasper-aec-bridge (6-channel profiles)
        │                            - selected chip beam or software AEC3
        │                                     │ UDP localhost
        │                                     ▼
        │                            jasper-voice
        │                            - openWakeWord + Silero VAD
        │                            - real-time LLM session
        │                              (Gemini | OpenAI | Grok)
        │                            - tool registry
        │                                     │
        │                                     ▼
        │                            TTS PCM → /run/jasper-fanin/tts.sock
        │                                     │
        └──── airborne echo back to mic ◄── speakers
```

`jasper-outputd` is the only normal writer to the physical DAC.
`jasper-camilla` writes post-DSP content to a private SHM slot ring, and
`jasper-voice` sends assistant PCM over fan-in's local TTS socket.
Wake/speech ducking happens in `jasper-fanin` **before** TTS is mixed, so
CamillaDSP applies the same crossover, correction, and protection path to
music and assistant audio. CamillaDSP `main_volume` stays the steady-state
listening-level knob.

`jasper-mux` arbitrates between the renderers. In auto mode every confirmed
source start is equal — a new inactive→active transition preempts the older
winner, so the rule is "latest source wins". Manual mode pins one renderer
lane instead, and `/sources/` can disable a source entirely. Before mux
moves the fan-in gate it asks `VolumeCoordinator` to make the target
source's volume carrier safe, so switching between push-volume sources
(Spotify/Bluetooth) and Camilla-master sources (AirPlay/USB) cannot expose
a full-scale transient.

`jasper-aec-bridge` is reconciler-managed. The XVF software-AEC path uses
outputd's final-speaker UDP monitor as the WebRTC AEC3 reference. The chip-AEC
path forwards the selected chip beam with AEC3 bypassed. Both feed
`jasper-voice` over UDP localhost; direct capture bypasses the bridge. See the
[microphone reference](jasper/mics/README.md) for capture support and evidence limits.

Management surfaces are stdlib HTTP wizards behind nginx, socket-activated
so they cost nothing resident between admin sessions. `deploy/nginx-jasper.conf`
is the authoritative route list; it covers the assistant
(`/assistant/voice/`, `/assistant/wake/`, `/assistant/tools/`,
`/assistant/chat/`, `/assistant/transit/`, `/assistant/weather/`,
`/assistant/google/`, `/assistant/ha/`), sound (`/sound/eq/`,
`/sound/speaker/`, `/sound/output/`, `/sound/pair/`,
`/sound/speaker/crossover/`, `/sound/bass/`), sources (`/sources/`,
`/spotify/`, `/bluetooth/`, `/airplay/`) and the system pages
(`/system/`, `/wifi/`, `/speaker/`).

---

## Repository layout

```
jasper/            Product Python: daemons, wizards, CLIs, tool packs
  voice_daemon.py    Main loop: wake → real-time LLM → tools → TTS
  mux.py             Renderer arbitration (latest-source-wins)
  camilla.py         CamillaDSP websocket control + ducking
  output_topology.py Output topology / DAC selection
  voice/             Provider-agnostic LiveConnection + per-provider adapters
  tools/             LLM tool packs and the tool registry
  web/               Setup wizards (primitives in web/_common.py, shell in web/chrome.py)
  control/           jasper-control: /state, management + automation HTTP API
  cli/               jasper-doctor, jasper-aec-*, measurement CLIs
  platform/          Control client, UDS + status-socket clients, systemd activation
  fanin/ multiroom/ transit/ cues/ peering/ usbsink/ accessories/
  sound/             CamillaDSP config emission and the graph carrier
  active_speaker/ audio_measurement/ correction/ attribution/
                     The speaker tuning + measurement program
  mics/ xvf/ audio_hardware/  Mic families, XVF3800 control, DAC registry
rust/              jasper-fanin (mixer), jasper-outputd (final output owner),
                     jasper-ring, jasper-resampler, jasper-clock and crates
c/                 jts-ring-ioplug: ALSA shared-memory ring plugin
jasper_aec3/       pybind11 binding for WebRTC AEC3 (optional built wheel)
wake_training/     Off-Pi wake-model training helpers (data prep only)
deploy/            install.sh + lib/install/, systemd units, nginx confs,
                     ALSA/CamillaDSP templates, web assets (app.css)
scripts/           Laptop-side operator tools (deploy, logs, diagnostics)
tests/             Hardware-free pytest suite; voice_eval/ makes paid calls
docs/              ADRs, designs, research archive
release/           first-party-arm64 artifact contract + BUILD-INFO schema
experiments/       Lab spikes — except usb-turntable/, which is production
                     (turntable-driven speaker measurement) despite the path
logs/              Landing directory fetch-pi-logs.sh writes into (gitignored)
LICENSES/          Apache-2.0 plus vendored third-party license texts
.claude/           Repo-scoped Claude Code commands (onboard-pi, reviews)
.github/           CI workflows, PR template, CODEOWNERS, dependabot
```

The audio path spans four of these: `deploy/` (ALSA + units), `rust/` and
`c/` (fan-in, output, ring), and `jasper/` (control, DSP, voice).

---

## Start here

- [QUICKSTART.md](QUICKSTART.md) — install a speaker from a fresh Raspberry Pi.
- [BRINGUP.md](BRINGUP.md) — perform full hardware bring-up and calibration.
- [docs/audio-paths.md](docs/audio-paths.md) — understand the live audio path.
- [docs/design-language.md](docs/design-language.md) — use the shared public
  interface language.
- [docs/web-ia.md](docs/web-ia.md) — place a management page and reuse its
  shared primitives.
- [docs/README.md](docs/README.md) — find current references, decisions, plans,
  research, and historical records.
- [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) — change the
  repository safely.

---

## Acoustic echo cancellation (AEC)

A speaker that plays music and listens for a wake word hears its own
output. AEC uses a playback reference to reduce that echo before wake
detection and speech capture.

Fresh installs default to `JASPER_AUDIO_INPUT_PROFILE=auto`. With a
registered production chip beam plan and a supported output DAC profile,
`sudo jasper-aec-commission` measures the alignment. Then `auto` resolves to
the chip-AEC profile: `jasper-outputd` fans the final speaker buffer out to the
XVF3800 USB-IN reference, the chip emits its fixed AEC beams, and the bridge
forwards the selected beam to `jasper-voice` with WebRTC AEC3 bypassed. If
chip-AEC cannot be armed, the managed XVF keeps hearing on the best leg its
mic can carry and discloses the reason and action rather than parking or
falling back silently.

Chip processing depends on the active profile; the software fallback uses
`SHF_BYPASS=1`. Chip-AEC profiles do not also arm software raw/DTLN wake
legs. `/assistant/wake/` exposes the household-level profile
choice (`auto`, `xvf_chip_aec`, `xvf_software_aec3`, `direct_mic`) and keeps the
per-leg toggles as advanced custom controls. Changing either runs
`jasper-aec-reconcile`, which restarts the affected services and updates
`/state`, doctor, and the dashboard.

The bridge opens a 6-channel XVF capture endpoint; 2-channel firmware uses
direct capture. Firmware and beam-plan support are owned by the
[microphone reference](jasper/mics/README.md). Flashing procedure:
[BRINGUP.md](BRINGUP.md#xvf-firmware-switch-to-6-channel-variant-via-dfu).

`jasper/xvf/xvf_host.py` is a JTS-owned USB control helper for the command
subset JTS uses, and is a useful standalone diagnostic:

```sh
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host --list
```

It deliberately does not expose filter-coefficient dumps, and runtime
profile writes belong to the commissioner rather than this surface. Never
call `SAVE_CONFIGURATION` — a known brick hazard.

---

## Getting started

Fresh Pi: start with [QUICKSTART.md](QUICKSTART.md). Long-form operator
runbook: [BRINGUP.md](BRINGUP.md).

Already deployed and just pushing changes:

```sh
# from your laptop:
bash scripts/deploy-to-pi.sh
# or with a non-default SSH target:
PI_HOST=192.168.1.42 JASPER_HOSTNAME=jts.local bash scripts/deploy-to-pi.sh
# or make the streambox intent explicit in the deploy log:
PI_HOST=jts4.local JASPER_INSTALL_PROFILE=streambox bash scripts/deploy-to-pi.sh
```

`deploy-to-pi.sh` captures the current git SHA and branch, preflights sudo,
rsyncs to the remote user's `${HOME}/jts/`, then runs the idempotent
`install.sh` under sudo with `JASPER_DEPLOY_SHA` / `JASPER_DEPLOY_BRANCH`
set. `install.sh` writes that metadata to `/var/lib/jasper/build.txt`, which
is what the `/system/` dashboard reports.

After merging a PR, `git fetch origin` and confirm `git merge-base
--is-ancestor <merge-commit> origin/main` before deploying — a fetch run
seconds after a merge can still miss GitHub's ref advance, and deploying
an unchanged SHA exits 0 without landing anything (`deploy-to-pi.sh`
flags that case as a same-SHA redeploy so it never reads as one).

There are exactly two install profiles, `full` and `streambox`. A fresh Pi
Zero 2 W with no persisted marker resolves to `streambox`; everything else
resolves to `full`. Both use the same repo and the same deploy path. The
older `endpoint`/`satellite` tokens still parse and migrate to `streambox`
on the next deploy. "Endpoint behaviour" is now purely the runtime multiroom
**follower** role.

---

## Debugging

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

`jasper-doctor` runs BRINGUP.md's smoke tests as code. `fetch-pi-logs.sh`
pulls journals, previous-boot OOM/watchdog clues, configs, and ALSA state
into `./logs/`, redacting environment-style secret assignments first.
`pi-run-diagnostic.sh` is the safe path for ad-hoc Pi-side experiments: it
wraps the command in a transient systemd unit with memory and runtime
bounds. `GET /state` on `jasper-control` returns one fail-soft JSON snapshot
of voice, audio, and renderers.

Common failure modes are at the bottom of [BRINGUP.md](BRINGUP.md). Start at
[docs/README.md](docs/README.md) for current subsystem references.
