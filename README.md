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

The XVF3800's onboard 3.5mm jack / AIC3104 codec is **not** connected —
speakers go to the Apple dongle. That non-standard choice is what drives
most of the AEC complexity below.

A Raspberry Pi Zero 2 W can run the `streambox` install profile: local
renderers, outputd/CamillaDSP, and the capability-filtered landing page,
but no voice, wake word, or mic/AEC.

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

`jasper-aec-bridge` is reconciler-managed. In non-XVF/custom software-AEC
profiles it consumes outputd's final-speaker UDP monitor, runs WebRTC AEC3
against the mic, and emits cleaned mono over UDP localhost for
`jasper-voice`. In the chip-AEC profile the same process bypasses AEC3 and
forwards the selected hardware-AEC chip beam over that carrier.

Management surfaces are stdlib HTTP wizards behind nginx, socket-activated
so they cost nothing resident between admin sessions. `deploy/nginx-jasper.conf`
is the authoritative route list; it covers setup (`/voice/`, `/tools/`,
`/sources/`, `/wake/`, `/wifi/`, `/transit/`, `/ha/`, `/weather/`,
`/speaker/`, `/rooms/`, `/spotify/`, `/bluetooth/`), sound (`/eq/`,
`/sound/setup/`, `/sound/room/`, `/sound/crossover/`, `/sound/bass/`),
and read-only dashboards (`/system/`, `/chat/`). Reference:
[`docs/HANDOFF-management-ui.md`](docs/HANDOFF-management-ui.md).

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
  web/               Setup wizards (shared primitives in web/_common.py)
  control/           jasper-control: /state, management + automation HTTP API
  cli/               jasper-doctor, jasper-aec-*, measurement CLIs
  fanin/ multiroom/ transit/ cues/ peering/ usbsink/ accessories/
  sound/             CamillaDSP config emission and the graph carrier
  active_speaker/ audio_measurement/ correction/ attribution/
                     The speaker tuning + measurement program
  mics/ xvf/ audio_hardware/  Mic families, XVF3800 control, DAC registry
  capture_relay/     Pi side of the phone-mic capture relay
  calibration_agent/ Runtime tuning-knowledge corpus + bundle intake
rust/              jasper-fanin (mixer), jasper-outputd (final output owner),
                     jasper-ring, jasper-resampler, jasper-clock and crates
c/                 jts-ring-ioplug: ALSA shared-memory ring plugin
jasper_aec3/       pybind11 binding for WebRTC AEC3 (optional built wheel)
wake_training/     Off-Pi wake-model training helpers (data prep only)
deploy/            install.sh + lib/install/, systemd units, nginx confs,
                     ALSA/CamillaDSP templates, web assets (app.css)
scripts/           Laptop-side operator tools (deploy, logs, diagnostics)
tests/             Hardware-free pytest suite; voice_eval/ makes paid calls
docs/              ADRs, subsystem HANDOFF spines, designs, research archive
capture-page/      Static phone-mic capture page (separate trust boundary)
relay/             Cloudflare Worker dead-drop relay for that page
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

## Documentation map

[AGENTS.md](AGENTS.md) is canonical for how to work in this repo;
[docs/adr/](docs/adr/) is canonical for why things are the way they are.
This README owns architecture and layout only — everything below is a
pointer, not a second copy.

### Repo-root docs

| File | Purpose |
|---|---|
| [AGENTS.md](AGENTS.md) | Operational rules for every AI agent. Canonical — edit here. |
| [CLAUDE.md](CLAUDE.md) | Thin import shim (`@AGENTS.md` + per-checkout `@CLAUDE.local.md`). |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Quick start, PR flow, CI lanes, branch protection |
| [QUICKSTART.md](QUICKSTART.md) | Imager → boot → `scripts/onboard.sh --adopt` → working speaker |
| [BRINGUP.md](BRINGUP.md) | Long-form operator runbook: flash, XVF firmware, calibration |
| [PLAN.md](PLAN.md) | v1 phased build and the forward roadmap |
| [CHANGELOG.md](CHANGELOG.md) | Keep-a-Changelog release notes |
| [SECURITY.md](SECURITY.md) | Supported versions, reporting path, LAN-appliance security model |
| [PRIVACY.md](PRIVACY.md) | What leaves the device, what stays local, retention defaults |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Contributor Covenant 2.1 |
| [LICENSE](LICENSE) / [NOTICE](NOTICE) | Apache 2.0 and the project notice |
| [LICENSE-third-party.md](LICENSE-third-party.md) | Third-party software, asset, model, and data attribution |

### Decisions

- [`docs/adr/`](docs/adr/) — append-only decision records, one decision per
  file. Start with
  [ADR-0001](docs/adr/0001-operating-model-reset.md) (the operating model)
  and read the family for a subsystem's *why* before its HANDOFF.
- [`docs/extensibility.md`](docs/extensibility.md) — **read before adding a
  modular subsystem:** host-mediated indirection, the five extension
  contracts, and the what-kind → which-pattern decision tree.
- [`docs/testing-tooling.md`](docs/testing-tooling.md) — index of every
  capture / scoring / forensic tool. Read before writing a new one.
- [`docs/doc-map.toml`](docs/doc-map.toml) — advisory code-glob → doc routing
  used by `scripts/docs-impact.py`.

### Subsystem spines

One line each; the doc is the canonical "read this before modifying".

- [`HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md) — the
  `LiveConnection`/`LiveTurn` abstraction over Gemini/OpenAI/Grok
- [`HANDOFF-prompting.md`](docs/HANDOFF-prompting.md) — voice prompting
  playbook; start here for `jasper/voice/prompt.py` or tool descriptions
- [`HANDOFF-pricing-editor.md`](docs/HANDOFF-pricing-editor.md) — where
  per-model rate data comes from and the `/voice/` rates editor
- [`HANDOFF-barge-in.md`](docs/HANDOFF-barge-in.md) — assistant-speech
  barge-in plan and current-code gap analysis
- [`HANDOFF-aec.md`](docs/HANDOFF-aec.md) — AEC architecture and operations
  (chip-AEC commissioning, the disclosed AEC3 fallback, bridge lifecycle)
- [`HANDOFF-enhanced-aec.md`](docs/HANDOFF-enhanced-aec.md) — optional
  vendored AEC3 v2: verified marker, background build, licensing boundary
- [`HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md) — canonical XVF3800
  reference: identity, firmware variants, parameters, DFU, failure modes
- [`HANDOFF-mic-quality-v2.md`](docs/HANDOFF-mic-quality-v2.md) — mic-quality
  workstream: sweeps, lever inventory, decision history
- [`HANDOFF-mic-fusion-architecture.md`](docs/HANDOFF-mic-fusion-architecture.md)
  — pluggable-mic boundary and the leg-count-agnostic wake-fusion layer
- [`HANDOFF-vad-experiments.md`](docs/HANDOFF-vad-experiments.md) — what
  endpoints a turn, why server VAD stays off, and the open raw-stream question
- [`HANDOFF-wake-training-experiment.md`](docs/HANDOFF-wake-training-experiment.md)
  — custom per-leg wake-model training plan
- [`HANDOFF-custom-wakeword-training.md`](docs/HANDOFF-custom-wakeword-training.md)
  — off-Pi custom wake-model training and deploy workflow
- [`HANDOFF-wake-corpus-quality.md`](docs/HANDOFF-wake-corpus-quality.md) —
  wake-corpus audio quality review
- [`HANDOFF-wake-telemetry.md`](docs/HANDOFF-wake-telemetry.md) — wake
  detection telemetry and funnel
- [`HANDOFF-usb-mic-wake.md`](docs/HANDOFF-usb-mic-wake.md) — cheap-USB-mic
  wake follow-up
- [`HANDOFF-audio-capability-platform.md`](docs/HANDOFF-audio-capability-platform.md)
  — who owns which mic/AEC/DAC fact, the profile vocabulary, validation
  artifacts
- [`HANDOFF-fan-in-daemon.md`](docs/HANDOFF-fan-in-daemon.md) — per-renderer
  snd-aloop lanes, the Rust summing daemon, buffer sizing
- [`HANDOFF-speaker-output-reference.md`](docs/HANDOFF-speaker-output-reference.md)
  — the output owner, true speaker reference, and TTS playout ledger
- [`HANDOFF-usb-low-latency.md`](docs/HANDOFF-usb-low-latency.md) — the
  shipped `usb_low_latency_48k` route and its doctor artifact gate
- [`HANDOFF-usb-latency-measurement.md`](docs/HANDOFF-usb-latency-measurement.md)
  — measurement reference and bench reproduction for USB-input latency
- [`HANDOFF-audio-latency-foundation.md`](docs/HANDOFF-audio-latency-foundation.md)
  — latency levers and the hard rules against re-architecting the topology
- [`HANDOFF-volume.md`](docs/HANDOFF-volume.md) — source-aware volume
  coordinator, one canonical `listening_level`
- [`HANDOFF-source-lifecycle.md`](docs/HANDOFF-source-lifecycle.md) —
  persisted source intent vs effective state, boot/deploy convergence
- [`HANDOFF-source-capabilities.md`](docs/HANDOFF-source-capabilities.md) —
  the Sources contract: vocabulary, capability map, new-source checklist
- [`HANDOFF-voice-music-control.md`](docs/HANDOFF-voice-music-control.md) —
  source-aware voice volume, transport, and Spotify play routing
- [`HANDOFF-airplay.md`](docs/HANDOFF-airplay.md) — AirPlay glitch
  troubleshooting; start here for audio artifacts on AirPlay
- [`multi-user-spotify.md`](docs/multi-user-spotify.md) — per-household-member
  Spotify account routing
- [`HANDOFF-usb-gadget.md`](docs/HANDOFF-usb-gadget.md) — **canonical** for the
  composite USB gadget: management network plus optional audio functions
- [`HANDOFF-usbsink.md`](docs/HANDOFF-usbsink.md) — the USB audio-input source
  and how its lane feeds fan-in
- [`HANDOFF-multiroom.md`](docs/HANDOFF-multiroom.md) — grouped playback:
  stereo pair, wireless sub, multi-room over Snapcast
- [`HANDOFF-peering.md`](docs/HANDOFF-peering.md) — multi-Pi wake arbitration,
  hubless P2P over mDNS-SD, off by default
- [`HANDOFF-identity.md`](docs/HANDOFF-identity.md) — the three speaker names,
  the identity reconciler, and the supported rename flow
- [`HANDOFF-resilience.md`](docs/HANDOFF-resilience.md) — the resilience
  ladder: watchdogs, memory pressure, reboot escalation, forensics
- [`HANDOFF-hotplug-resilience.md`](docs/HANDOFF-hotplug-resilience.md) —
  runtime mic/DAC/accessory attach-detach convergence with no redeploy
- [`HANDOFF-tier5-watchdog-liveness.md`](docs/HANDOFF-tier5-watchdog-liveness.md)
  — why the kernel watchdog cannot see userspace, and the deferred dials
- [`HANDOFF-runtime-memory.md`](docs/HANDOFF-runtime-memory.md) — **the RAM
  budget:** always-on footprint decisions and the remaining levers
- [`HANDOFF-observability.md`](docs/HANDOFF-observability.md) — the `event=`
  spine, journald retention, the debug card, and the flight recorder
- [`HANDOFF-privilege-separation.md`](docs/HANDOFF-privilege-separation.md) —
  threat model and the de-rooting ladder
- [`HANDOFF-control-plane-auth.md`](docs/HANDOFF-control-plane-auth.md) —
  device-to-device / household control-plane auth
- [`HANDOFF-supply-chain.md`](docs/HANDOFF-supply-chain.md) — provenance,
  checksum policy, and accepted gaps for build-time inputs
- [`HANDOFF-build-sandbox.md`](docs/HANDOFF-build-sandbox.md) — RAM-bounded,
  cgroup-contained builds so an OOM kills only the build
- [`HANDOFF-install-update-transaction.md`](docs/HANDOFF-install-update-transaction.md)
  — an update as a transaction: build manifest, deploy verification, rollback
- [`HANDOFF-pi-image-delivery.md`](docs/HANDOFF-pi-image-delivery.md) —
  stock-OS → bootstrap → hybrid-image gradient and promotion gates
- [`HANDOFF-first-party-arm64-artifacts.md`](docs/HANDOFF-first-party-arm64-artifacts.md)
  — the ARM64 build lane, bundle format, and reproducibility boundary
- [`HANDOFF-homeassistant.md`](docs/HANDOFF-homeassistant.md) — smart-home
  delegation through Home Assistant's conversation API; `/ha/` wizard
- [`HANDOFF-transit-citibike.md`](docs/HANDOFF-transit-citibike.md) — subway,
  Citi Bike, and Routes: config ownership, caching, and fallback contracts
- [`HANDOFF-audible-feedback.md`](docs/HANDOFF-audible-feedback.md) —
  pre-rendered cues; start here when a failure path must not fall silent
- [`HANDOFF-management-ui.md`](docs/HANDOFF-management-ui.md) — management-
  surface IA, anti-patterns, and remaining roadmap
- [`design-language.md`](docs/design-language.md) — the craft layer under the
  UI: type ladder, depth, radii, touch targets, motion, interface writing
- [`HANDOFF-dlna.md`](docs/HANDOFF-dlna.md) — DLNA/UPnP media input (design
  only, no code yet)
- [`adr/0145-remote-updates-stay-a-laptop-deploy.md`](docs/adr/0145-remote-updates-stay-a-laptop-deploy.md)
  — why there is no OTA update button, and the shape if that changes
- [`dumb-endpoint-bringup.md`](docs/dumb-endpoint-bringup.md) — Zero 2 W
  streambox lab runbook and the two-install-profile decision
- [`docs/audio-paths.md`](docs/audio-paths.md) — the two ALSA paths, which
  volume knob attenuates which, and the checklist for a new music source

### Speaker tuning and measurement program

Its own doctrine and cadence; start at the doctrine, not the plans.

- [`measurement-loop-doctrine.md`](docs/measurement-loop-doctrine.md) —
  **canonical doctrine:** the measure → analyze → recommend → loop → save
  cycle, the authority model, the ethos rulings, the hard-stop list
- [`tuning-master-plan.md`](docs/tuning-master-plan.md) — ratified plan:
  declared-design executor, linearization tournaments, LLM operator
- [`tuning-operator-runbook.md`](docs/tuning-operator-runbook.md) — the one
  operational map: what the `/sound/crossover/` commission session is, how to
  drive a round over SSH, and what the doors refuse
- [`crossover-v2-engine-design.md`](docs/crossover-v2-engine-design.md) — the
  engine's architecture: the session, its seams, the file map, and the contracts
  a refactor must preserve
- [`HANDOFF-active-speaker-dsp.md`](docs/HANDOFF-active-speaker-dsp.md) —
  active-speaker DSP commissioning, baseline lifecycle, safety invariants
- [`HANDOFF-bass-extension-plan.md`](docs/HANDOFF-bass-extension-plan.md) —
  commissioned, volume-scheduled low-frequency alignment. Waves 1–3 are merged
  plus the Wave 4 `ladder.py` slice; commissioning backend and runtime
  scheduling have not shipped. Per-wave prompts:
  [`docs/bass-extension-waves/`](docs/bass-extension-waves/README.md)
- [`HANDOFF-correction.md`](docs/HANDOFF-correction.md) — the HTTPS
  measurement service behind `/sound/room/`, `/sound/crossover/`, `/sound/bass/`
- [`HANDOFF-sound-preferences.md`](docs/HANDOFF-sound-preferences.md) — the
  `/eq/` preference layer and `/sound/setup/` global-output surface
- [`HANDOFF-dsp-graph-carrier.md`](docs/HANDOFF-dsp-graph-carrier.md) —
  composing preference EQ + correction on any output topology
- [`HANDOFF-audio-measurement-core.md`](docs/HANDOFF-audio-measurement-core.md)
  — the shared measurement/calibration core the flows build on
- [`HANDOFF-distributed-active.md`](docs/HANDOFF-distributed-active.md) —
  running an active speaker's driver-domain crossover as a wireless follower
- [`HANDOFF-calibration-agent.md`](docs/HANDOFF-calibration-agent.md) —
  calibrated-mic ingest and the eventual LLM "audio engineer"
- [`active-speaker-tuning-layers-design.md`](docs/active-speaker-tuning-layers-design.md)
  — the adopted five-layer tuning model and its decision register
- [`active-crossover-information-design.md`](docs/active-crossover-information-design.md)
  and [`room-correction-information-design.md`](docs/room-correction-information-design.md)
  — product/architecture designs of record for the two builder surfaces
- [`correction-journey-design.md`](docs/correction-journey-design.md) — the
  three-step Crossover → Room → Bass journey (design record)
- [`historical/linearization-campaign-2026-07.md`](docs/historical/linearization-campaign-2026-07.md)
  — the 2026-07 linearization campaign's archived decision record: the flat
  spec, the six fundamentals, the non-goals, the boost ruling, and the
  integrity ladder that production constants cite as provenance
- [`gating-v2-plan.md`](docs/gating-v2-plan.md),
  [`room-correction-regime-plan.md`](docs/room-correction-regime-plan.md),
  [`two-stage-commission-flow-plan.md`](docs/two-stage-commission-flow-plan.md)
  — adopted work orders, each scoped to one campaign
- [`crossover-measurement-productization-design.md`](docs/crossover-measurement-productization-design.md)
  — decision archaeology for the phone-mic measurement flow
- [`PROPOSAL-dac-profile-registry.md`](docs/PROPOSAL-dac-profile-registry.md) —
  the data-driven DAC profile registry in `jasper/audio_hardware/dac.py`
- [`dual-apple-dac-lab.md`](docs/dual-apple-dac-lab.md) — lab-only runbook for
  two Apple dongles as one stereo DAC
- [`phone-mic-relay-plan.md`](docs/phone-mic-relay-plan.md) — the capture page
  plus stateless end-to-end-encrypted dead-drop relay design and build record
- [`jasper/calibration_agent/corpus/`](jasper/calibration_agent/corpus/README.md)
  — tuning knowledge the product reads at runtime (a package resource)

### Plans and proposals

- [`tool-platform-plan.md`](docs/tool-platform-plan.md) — the extensible tool
  platform and its trust gradient; records the shipped Phase-1.5 pieces
- [`research-tool-plan.md`](docs/research-tool-plan.md) — the async
  "research this and tell me later" tool
- [`conversation-history-plan.md`](docs/conversation-history-plan.md) — the
  `/chat/` household-visible conversation log
- [`docs/examples/tool_pack_starter.py`](docs/examples/tool_pack_starter.py) —
  copyable capability-pack example; tests import it so it cannot drift
- [`install-update-resilience-plan.md`](docs/install-update-resilience-plan.md)
  and [`install-hardware-tier-and-staleness.md`](docs/install-hardware-tier-and-staleness.md)
  — the install/update hardening brief and its tier-awareness finding
- [`multiroom-pairing-reliability-plan.md`](docs/multiroom-pairing-reliability-plan.md)
  — rescued, not-yet-executed pairing-reliability plan (2026-07-28 snapshot)
- [`PLAN-usb-mic-export-latency-fix.md`](docs/PLAN-usb-mic-export-latency-fix.md)
  — verbatim point-in-time plan and execution record
- [`barge-in-build-prompts.md`](docs/barge-in-build-prompts.md) — execution
  artifact for building barge-in; retire once it ships
- [`docs/correction-ux-wave3/`](docs/correction-ux-wave3/README.md) — staged
  execution prompts for the correction/crossover IA rework
- [`audit-pending-followups.md`](docs/audit-pending-followups.md) — deferred
  and rejected follow-ups from the May 2026 pattern audit
- [`OSS-READINESS-TOP-FIVE.md`](docs/OSS-READINESS-TOP-FIVE.md) — contributor
  "files to know" register and OSS-readiness priorities
- [`DEEP-AUDIT-PLAYBOOK.md`](docs/DEEP-AUDIT-PLAYBOOK.md) — the whole-codebase
  audit method behind the `/deep-audit` command
- [`REVIEW-deep-audit-ledger.md`](docs/REVIEW-deep-audit-ledger.md) — live
  findings tracker joined to the deep-audit reports by DA-NNNN id

### Historical and research

Preserved for archaeology; **not** current operational truth.

- [`docs/historical/`](docs/historical/) — completed or superseded runbooks,
  campaign records, and investigation histories
- [`docs/research/`](docs/research/) — verbatim external and model-generated
  research inputs, one directory per study
- [`CHIP-AEC-EXPERIMENT.md`](docs/CHIP-AEC-EXPERIMENT.md) — 2026-05/06 lab
  evidence that proved external-DAC chip AEC; use `HANDOFF-aec.md` instead
- [`historical/chip-aec-dac-portability-2026-06.md`](docs/historical/chip-aec-dac-portability-2026-06.md)
  — clock-domain measurements and the rejected rate-matcher design
- [`historical/volume-control-redesign-2026-05.md`](docs/historical/volume-control-redesign-2026-05.md)
  — why AirPlay receiver-originated volume reflection did not work
- [`RESEARCH-pipewire-low-latency.md`](docs/RESEARCH-pipewire-low-latency.md) —
  what PipeWire's source does, and the JTS verdict per technique
- [`crossover-design-guide-deep-research-2026-08-19.md`](docs/crossover-design-guide-deep-research-2026-08-19.md)
  and [`crossover-measurement-deep-research-2026-07-18.md`](docs/crossover-measurement-deep-research-2026-07-18.md)
  — owner-supplied primary-source research reports
- [`AEC-DIAG-06-xvf-format-level-profile.md`](docs/AEC-DIAG-06-xvf-format-level-profile.md)
  — entry point to the dated AEC diagnostic notes
- [`REVIEW-2026-06-04-deep-dive.md`](docs/REVIEW-2026-06-04-deep-dive.md),
  [`-big-rocks`](docs/REVIEW-2026-06-04-big-rocks.md),
  [`-small-wins`](docs/REVIEW-2026-06-04-small-wins.md),
  [`REVIEW-2026-06-12-oss-due-diligence.md`](docs/REVIEW-2026-06-12-oss-due-diligence.md),
  [`REVIEW-google-oss-readiness.md`](docs/REVIEW-google-oss-readiness.md),
  [`REVIEW-deep-audit-2026-07-11.md`](docs/REVIEW-deep-audit-2026-07-11.md) —
  point-in-time review snapshots

---

## Acoustic echo cancellation (AEC)

A speaker that plays music and listens for a wake word in one box hears its
own output 20–40 dB louder than the user. Without AEC the detector fires on
the music, or on the TTS it just synthesised. There are three places to
address it: the mic chip's DSP (cheapest and best, but only in topologies
its firmware supports), software on the host (topology-agnostic, costs
CPU/RAM), or design around it (push-to-talk, isolation, ducking to silence).

Fresh installs default to `JASPER_AUDIO_INPUT_PROFILE=auto`. On 6-channel
XVF3800 hardware with a supported output DAC profile — and after the
installation passes `sudo jasper-aec-commission` — `auto` resolves to the
chip-AEC profile: `jasper-outputd` fans the final speaker buffer out to the
XVF3800 USB-IN reference, the chip emits its fixed AEC beams, and the bridge
forwards the selected beam to `jasper-voice` with WebRTC AEC3 bypassed. If
chip-AEC cannot be armed, the managed XVF keeps hearing on the best leg its
mic can carry and discloses the reason and action rather than parking or
falling back silently. Software AEC3 remains the normal path for non-XVF
microphones.

The chip's beamforming, noise suppression, and AGC run either way; the rule
is not to double-process, so chip-AEC profiles do not also arm software
raw/DTLN wake legs. `/wake/` exposes the household-level profile choice
(`auto`, `xvf_chip_aec`, `xvf_software_aec3`, `direct_mic`) and keeps the
per-leg toggles as advanced custom controls. Changing either runs
`jasper-aec-reconcile`, which restarts the affected services and updates
`/state`, doctor, and the dashboard.

The bridge transport is UDP localhost, not a second snd-aloop card:
snd-aloop's kernel-side `loopback_cable` wedges when a consumer is
SIGKILL'd, which cost a reboot in production. The bridge also needs the
6-channel XVF firmware variant, because it opens the 6-channel USB capture
endpoint; the 2-channel firmware Seeed ships by default does not match that
capture shape. Flashing procedure:
[BRINGUP.md](BRINGUP.md#xvf-firmware-switch-to-6-channel-variant-via-dfu);
version constants live in [`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py).

`jasper/xvf/xvf_host.py` is a JTS-owned USB control helper for the command
subset JTS uses, and is a useful standalone diagnostic:

```sh
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host --list
```

It deliberately does not expose filter-coefficient dumps, and runtime
profile writes belong to the commissioner rather than this surface. Never
call `SAVE_CONFIGURATION` — a known brick hazard.

Full operations: [`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md). RAM cost of
the always-on daemons: [`docs/HANDOFF-runtime-memory.md`](docs/HANDOFF-runtime-memory.md).

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

There are exactly two install profiles, `full` and `streambox`. A fresh Pi
Zero 2 W with no persisted marker resolves to `streambox`; everything else
resolves to `full`. Both use the same repo and the same deploy path. The
older `endpoint`/`satellite` tokens still parse and migrate to `streambox`
on the next deploy. "Endpoint behaviour" is now purely the runtime multiroom
**follower** role — see [`docs/HANDOFF-multiroom.md`](docs/HANDOFF-multiroom.md).

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

Common failure modes are at the bottom of [BRINGUP.md](BRINGUP.md). For
anything subsystem-specific, the relevant doc above almost certainly
addresses the symptom.
