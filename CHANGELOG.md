# Changelog

All notable changes to JTS will be documented in this file.

This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. A maintainer will tag `v0.1.0` at OSS launch; this branch does not create
release tags.

## [Unreleased]

First public release candidate. JTS is a voice-controlled smart speaker that
runs entirely on a single 1 GB Raspberry Pi 5: a swappable real-time voice
assistant, multi-source music playback, CamillaDSP room correction, and a local
web management UI — designed to keep working unattended for months and to
self-heal across crashes, power loss, and missing hardware.

> Draft release notes. A maintainer tags `v0.1.0` at OSS launch; rename this
> section to `[0.1.0] - YYYY-MM-DD` at that point.

### Added

- Real-time voice assistant with a swappable provider backend behind one
  `LiveConnection` interface — Gemini Live, OpenAI Realtime, and xAI Grok —
  selectable from the `/voice` wizard, with per-turn spend accounting and a
  daily spend cap.
- Multi-leg wake-word detection (openWakeWord) OR-gating software AEC3 and
  XVF3800 chip-AEC legs, a `/wake` model + sensitivity wizard, and on-device
  wake-event telemetry for tuning.
- Audio routing core: per-renderer snd-aloop lanes summed by the allocation-free
  `jasper-fanin` Rust mixer, through CamillaDSP, to the final-output owner
  `jasper-outputd`; a hard `volume_limit = 0.0` safety ceiling enforced in the
  YAML contract, the Python clamp, and the Rust gain cap.
- Four music sources with latest-source-wins arbitration (`jasper-mux`):
  Spotify (multi-account Web API control), AirPlay 2 (shairport-sync), Bluetooth
  (BlueALSA), and USB-C audio input (`jasper-usbsink`).
- Room correction v2: synchronized swept-sine measurement, FFT deconvolution,
  and PEQ design over a stdlib HTTP + SSE flow, memory-bounded for the 1 GB Pi.
- Active-crossover / multi-driver speaker DSP with fail-closed driver protection
  and tweeter-blowout safeguards (engineering substrate; not the default output
  path).
- Integrations as self-contained, registry-driven plugins: NYC transit (subway,
  bus, Citi Bike), weather, Home Assistant (conversation API), and Google
  Calendar + Gmail (per-account OAuth).
- Multi-room foundations: a snapcast-based grouping dataplane, member-local
  assistant playback via the shared `jasper-tts-protocol` crate, `/rooms`
  pairing UX, and mDNS peering for wake arbitration (in progress; fail-closed to
  solo).
- Local management UI: socket-activated stdlib HTTP wizards behind a shared
  canonical design system, with CSRF/host guards and XSS-safe rendering enforced
  by conventions tests.
- Resilience ladder: per-unit systemd watchdogs, an OOM-score protection ladder,
  a cross-boot bootloop-guard circuit breaker, a userspace-liveness supervisor,
  declarative AEC / audio-hardware / identity / WiFi reconcilers, and a fail-soft
  `/state` aggregator plus `jasper-doctor`.
- Observability: a three-plane boundary (production health, TTL-bound debug, and
  bounded diagnostics), an in-RAM flight recorder, and structured `event=` logs.
- Single-command laptop→Pi deploy (`scripts/deploy-to-pi.sh`) with identity,
  direction, and downgrade guards, an idempotent modular installer, and SHA-256
  supply-chain provenance enforced in CI.

### Changed

- Voice daemon and control server decomposed along real seams; AEC bridge legs
  routed through a shared emitter with ports derived from the wake registry; TTS
  loudness extracted to a shared engine; the installer split into reviewable
  runtime/systemd modules.
- Transit reworked into self-contained city packs with provider-owned env
  parsing and a `/transit` city-pack wizard.
- A shared `jasper-tts-protocol` Rust crate replaces byte-twin copies across the
  fan-in and outputd daemons.

### Fixed

- Resilience and correctness hardening: outputd parks when a configured DAC
  disappears, shairport dead-unit recovery, the bonded-config reboot-loop chain
  (camilla pipe-guard), bootloop-guard CLI edge cases, the system supervisor
  skipping the sshd probe when sshd is disabled, fan-in TTS duck/restore bounds,
  serialized wake-model env updates, and Gemini turn-slot rollback on a failed
  activity start.
- Contributor front door: macOS installs no longer try to build Linux-only C
  extensions, and the documented `uv` test-install command now installs the
  runtime extras the suite imports (a bare `uv sync` previously failed
  collection on a clean checkout).

### Security

- Documented the LAN-trust security model and enumerated every unauthenticated
  control endpoint in SECURITY.md.
- Guarded wizard GET read paths against cross-site reads; enforced a Bluetooth
  non-pairable-at-rest floor at runtime.
- Privacy logging hardened: voice transcripts log metadata (character counts),
  not content; content-bearing tool payloads are redacted; wake enrollment is
  mic-mute gated. PRIVACY.md documents what leaves the device and what stays
  local.

### Removed

- Deleted the dead legacy single-session voice layer (`VoiceSession` /
  `GeminiLiveSession` / `WakeWordDetector.feed`); the daemon runs on the
  persistent `LiveConnection` / `LiveTurn` path.
- Removed an uncleared third-party dial touch driver and SquareLine gauge assets
  (re-implemented procedurally) to clear firmware for source redistribution.
