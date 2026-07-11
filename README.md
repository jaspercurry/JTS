# JTS — the Jasper Tech Speaker

JTS is the **J**asper **T**ech **S**peaker — the debut build from the
[Jasper Tech](https://www.youtube.com/@Jasper_Tech) YouTube channel.

A custom voice-controlled smart speaker on a Raspberry Pi 5 running
Raspberry Pi OS Lite Trixie, with
[CamillaDSP](https://github.com/HEnquist/camilladsp) for audio. The
voice loop is provider-agnostic: any of three real-time
speech-to-speech APIs can drive it via a single env-var switch —
[Gemini 3.1 Flash Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
(cheapest), [OpenAI gpt-realtime-2](https://developers.openai.com/api/docs/guides/realtime),
or [xAI Grok Voice Agent](https://docs.x.ai/docs/guides/voice/agent).
This is a personal hobby project; not a product.

The pitch: a music streamer that's also a voice assistant, built
from open hardware and open audio software, with the LLM costing
roughly $1–3/month at light use on the cheapest provider.

Privacy details: [PRIVACY.md](PRIVACY.md) explains cloud egress, local retention, and mic mute scope.

**Want to set one up?**
- **Using Claude Code?** Just open this repo and say *"I want to set up
  a JTS speaker"* (or *"set up a Pi"*, *"I just got a new Pi"*,
  whatever feels natural). Claude reads the
  [`/onboard-pi`](.claude/commands/onboard-pi.md) skill and walks you
  through every step — Raspberry Pi Imager's nested OS picker,
  password-based SSH setup, SD card flash, first boot, network
  discovery (including multi-speaker collision detection), and the
  install. ~30 minutes total.
- **Prefer to read the steps yourself?** [QUICKSTART.md](QUICKSTART.md)
  is the same flow as a human-readable walkthrough.
- **Doing the full long-form bringup** (hardware calibration, XVF
  firmware flashing, satellite devices)? See [BRINGUP.md](BRINGUP.md).

The setup docs default to the hostname `jts`, which becomes
`jts.local` on your home network. If you choose another hostname in
Raspberry Pi Imager, such as `jts3`, use `jts3.local` everywhere
later. Keep the Pi and your computer on the same Wi-Fi during setup.

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
  hw:Loopback,1,0..4  ──►  jasper-fanin ◄── /run/jasper-fanin/tts.sock
                              │ sums active renderer/test lanes + TTS
                              │ applies program duck before TTS mix
                              ▼
                       hw:Loopback,0,7
                              │
                              ▼ (loop)
                       pcm.jasper_capture / pcm.jasper_ref
                              │
                              ▼
                    jasper-camilla (CamillaDSP, port 1234)
                    - main_volume (listening level / source volume)
                    - crossover / correction / protection profile
                              │
                              ▼
                    outputd_content_playback
                              │
                              ▼ (loop)
                    outputd_content_capture
                              │
                              ▼
                    jasper-outputd (final output owner)
                    - writes post-DSP content to the selected sink
                    - publishes runtime health / xrun counters
                              │
                              ▼
                    outputd_dac (selected final-output DAC)
                              │
                              ▼
                    Apple USB-C dongle or DAC8x → amp → speakers


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
        │                            /run/jasper-fanin/tts.sock
        │                                     │
        │                                     ▼
        └──── airborne echo back to mic ◄── speakers
```

`jasper-outputd` is the only normal writer to the physical DAC.
`jasper-camilla` writes post-DSP content to a private loopback lane,
and `jasper-voice` sends assistant PCM over fan-in's
outputd-compatible local TTS socket. Wake/speech ducking happens in
`jasper-fanin` before TTS is mixed, so CamillaDSP can apply the same
crossover, correction, and protection path to music and assistant
audio. CamillaDSP `main_volume` remains the steady-state listening
level/source-volume knob.

> ### Important: one final output owner
>
> Music and TTS both go **through** CamillaDSP. TTS enters
> `jasper-fanin`, which measures content before ducking, applies the
> provider/profile/peak-capped assistant loudness matcher, mixes TTS
> after the program duck, then hands one protected stream to CamillaDSP.
> `jasper-outputd` owns the final DAC timing loop and nothing else
> normally writes to the physical sink. To test the chain at a
> controlled volume, play to `correction_substream`; the legacy
> `jasper_out` dmix remains only as the pre-outputd rollback path. How
> assistant loudness matching works:
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

There's also a reconciler-managed AEC bridge (`jasper-aec-bridge`).
In the software fallback profile it consumes outputd's final-speaker
UDP monitor, runs WebRTC AEC3 against the XVF mic, and emits the
cleaned-up mono signal over UDP localhost for jasper-voice. In the
chip-AEC profile, the same bridge process bypasses WebRTC AEC3 and
forwards the selected hardware-AEC chip beam over that carrier. It
runs automatically only when the configured AEC mic is present with
6-channel firmware — see § below.

---

## Current status

`v1` (per [PLAN.md](PLAN.md)) is mostly landed:

- ✅ Music streaming (AirPlay 2, Spotify Connect, Bluetooth A2DP) via
  source-built shairport-sync + nqptp, librespot (rust, via raspotify
  .deb) with log volume curve, and bluez-alsa
- ✅ `jasper-mux` daemon for latest-source-wins preemption plus manual
  landing-page source selection with guarded volume handoff
- ✅ Always-on CamillaDSP with a passthrough `master_gain` mixer
- ✅ Outputd mainline topology: `jasper-outputd` owns final DAC playback
  and sink health; assistant audio enters fan-in before CamillaDSP so it is
  crossed over like other content; `/state.outputd` reports output health
  while the pre-outputd Camilla statefile remains intact for rollback
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
  refresh provider model lists for experimental trials, manage pricing
  and spend cap settings, save. Writes provider selection to
  `/var/lib/jasper/voice_provider.env`, writes API keys to
  `/var/lib/jasper-secrets/voice_keys.env`, and restarts `jasper-voice`
- ✅ Tools: volume, transport (play/pause/skip/now-playing), Spotify
  search & queue, weather (now including daily sunrise/sunset),
  NYC subway times, NYC MTA bus arrivals, NYC Citi Bike availability
  (split between classic bikes and e-bikes, with open-dock counts;
  all configured via the `/transit/` wizard), Google Routes travel-time
  and directions from the speaker's saved location, current wall-clock time
- ✅ Tool catalog wizard at `http://jts.local/tools/` — browse
  first-party voice capabilities as top-level packs grouped by category
  (with singleton packs for standalone tools), open a generated pack
  detail page, inspect the individual tools inside, and toggle either a
  whole pack or a specific child tool. Detail pages expose the full
  model-facing prompt, input schema, and metadata; advanced users can
  override a prompt at their own risk and reset it to the built-in
  default. Each page links a tool-authoring guide at
  `http://jts.local/tools/guide/` (the house style for first-party and
  trusted-PR capability packs). Reads the catalog `jasper-voice` writes to
  `/run/jasper/tools.json`; disabled tools/packs persist to
  `/var/lib/jasper/tool_state.env` and prompt overrides persist to
  `/var/lib/jasper/tool_prompt_overrides.json` (fail-safe: missing or
  malformed state = defaults). Toggles and prompt edits stage
  instantly; Apply restarts `jasper-voice` once, which re-filters the
  registry and re-writes the catalog
- ✅ Multi-user Spotify routing (each household member's account,
  routed by AirPlay title-match)
- ✅ Transit setup wizard at `http://jts.local/transit/` — type your
  address, the page geocodes via OSM Nominatim, shows nearest subway,
  bus stops, Citi Bike stations, and Google Routes travel-time settings,
  lets you pick. The Travel Time card stores the billable Routes API key
  in `/var/lib/jasper-secrets/google_routes.env` and lets the household
  choose a default travel mode (`transit`, `drive`, `walk`, or `bicycle`);
  voice requests like "drive to..." still override that per question. Multi-stop bus
  support — save both the eastbound and westbound stops at your
  corner and "next bus" unions arrivals across them. Subway "next
  train" returns every line at the station including service-change
  reroutes (an N rerouted onto D tracks at a D station appears in the
  same answer). Citi Bike multi-station picker with a household-wide
  "only e-bikes" toggle; voice answers always split classic from
  e-bike counts unless the toggle is on. Modular over
  `jasper.transit.REGISTRY`: discovery (bbox + stop lookup) for a new
  city/mode (Berlin BVG, Capital Bikeshare DC, …) starts with one new
  provider module under `jasper/transit/providers/`. The canonical
  checklist in `jasper/transit/__init__.py` is seven logical edit
  points: provider module, CityPack entry, wizard card dispatch,
  bespoke card renderer, voice-tool factory, runtime client, and the
  install migration for provider env keys. `REGISTRY` and the daemon
  wiring derive automatically — no `voice_daemon.py` or `config.py`
  edit. NYC subway and Citi Bike are keyless; NYC bus requires a free
  MTA BusTime API key — that card is locked until the user pastes one.
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
  clearing room correction. A collapsed Speaker setup card hosts the
  active-crossover commissioning flow — choose the speaker layout, enter
  driver/crossover values, confirm the DAC outputs, then run the guarded
  per-driver audible test and apply the active speaker profile (passive /
  full-range speakers skip the driver test). See
  [docs/HANDOFF-sound-preferences.md](docs/HANDOFF-sound-preferences.md)
  for the composition contract, profile semantics, and observability
  hooks.
- ✅ Speaker-name wizard at `http://jts.local/speaker/` — one display
  name for AirPlay, Spotify Connect, Bluetooth, and USB Audio. Defaults
  to `JTS`; the URL remains the hostname chosen in Imager
  (`jts.local`, `jts3.local`, etc.).
- ✅ **USB Audio Input** (`jasper-usbsink`) — fourth music source.
  Plug a computer into the Pi's USB data/OTG port through a compatible
  power/data splitter or hub and the host sees the configured speaker
  name as a USB audio output device while this speaker is solo or a pair
  leader. A bonded follower parks the USB gadget even if saved intent is
  on, so it does not advertise itself as an independent input. Off by default; toggle at
  `http://jts.local/sources/` enables it. The host's volume slider
  drives JTS's canonical `listening_level` (feels like spinning the
  dial). Joins the existing mux arbitration for latest-source-wins
  preemption. Zero RAM cost when off; the Rust audio bridge is low
  single-digit MB when on, plus the non-real-time host-volume helper. See
  [docs/HANDOFF-usbsink.md](docs/HANDOFF-usbsink.md) for the full
  design.
- ✅ **USB management network** — the same USB-C port always carries a
  USB NCM network link (`ncm.usb0`, on by default, independent of the
  USB Audio Input toggle above): plug a laptop in and
  `http://<JASPER_HOSTNAME>/` (or the documented fallback
  `http://10.12.194.1/`) works even with the Pi's Wi-Fi off. No IP
  forwarding/NAT — the plugged-in laptop keeps its own default route.
  Kill switch: `JASPER_USB_NETWORK=disabled`. See
  [docs/HANDOFF-usb-gadget.md](docs/HANDOFF-usb-gadget.md) for the
  composite-gadget design (both USB functions share one ConfigFS
  descriptor).
- ✅ Wi-Fi network wizard at `http://jts.local/wifi/` — current
  network at top, scan + tap-to-connect for nearby networks,
  manual join-by-name fallback for hidden or scan-suppressed networks,
  saved networks in a collapse section with Forget. Backed by
  `nmcli`. On Pi 5 brcmfmac scan suppression, `/wifi/scan` attempts a
  bounded non-disruptive self-heal before falling back to manual join.
  Connect rolls back to the previous network on failure
  (`nmcli --wait 30 dev wifi connect` + explicit `connection up
  <previous>` on non-zero exit). Saved profiles are hardened to keep
  retrying after router/ISP flaps, and a no-resident-RAM recovery timer
  nudges scan suppression even when NetworkManager still reports an
  active profile; it calls the guardian activation path only when
  Wi-Fi is actually down. WPA-Enterprise deferred — home-network case only.
- ✅ Persistent live session with sustained-speech VAD
- ✅ Hardware AEC investigation: the 2026-05-29 Option D lab pass has
  been promoted into the recommended XVF3800 input profile. Fresh
  installs seed `JASPER_AUDIO_INPUT_PROFILE=auto`: on 6-channel XVF3800
  hardware plus a supported/calibrated output DAC profile this resolves
  to chip-AEC with USB-IN reference + direct source fanout; otherwise it
  falls back to software AEC3/direct mic as hardware allows. Current
  findings live at
  [`docs/CHIP-AEC-EXPERIMENT.md`](docs/CHIP-AEC-EXPERIMENT.md)
- ✅ AEC bridge reconciles automatically on 6-channel XVF firmware
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

Current AEC behavior is profile-driven rather than a separate
"marginal items" list: `JASPER_AUDIO_INPUT_PROFILE=auto` uses the
chip-AEC profile on 6-channel XVF3800 hardware with a
supported/calibrated output DAC profile, falls back to software AEC3/direct
mic when needed, and exposes custom raw/DTLN/chip-leg switches from `/wake/`
for corpus or nonstandard hardware. Resource
costs are in the table below, and the current wake refractory lives as
`WAKE_REFRACTORY_SEC` in `jasper/voice_daemon.py`.

---

## Repository layout

```
jasper/                         Python daemon source
  voice_daemon.py               Main: wake → real-time LLM → tools → TTS
  audio_io.py                   MicCapture, TtsPlayout, TTS IPC transport
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
  xvf/                          JTS-owned XVF3800 USB control helper
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
                                rotary dial (volume, play/pause,
                                hold-to-talk; display scenes scaffolded)
  satellite-amoled/             PlatformIO project for the Waveshare
                                ESP32-S3 touchscreen/mic satellite
  ...                           optional accessory firmware projects

deploy/
  install.sh                    Idempotent installer (run as root on Pi)
  alsa/                         /etc/asound.conf template
  camilladsp/                   legacy v1.yml + outputd-cutover.yml baselines
  systemd/                      jasper-{camilla,voice,control,mux,outputd,aec-bridge,aec-init,...}
                                + librespot, shairport-sync, nqptp, Bluetooth no-code agent
  modules-load.d/               snd-aloop autoload
  modprobe.d/                   snd-aloop single-card config
  bin/                          jasper-librespot-event (--onevent hook)
  configure-bluez.sh            Speaker-mode pairing config
  shairport-sync.conf           AirPlay 2 receiver config
  index.html                    Static landing page
  assets/fonts/                 Local web fonts for static management UI
  correction-preflight.html     HTTP warning before HTTPS correction measurements
  nginx-jasper.conf             Main nginx site: HTTP wizards + HTTPS correction hub

docs/                           Subsystem deep-dives ("HANDOFF" docs)
  HANDOFF-wake-training-experiment.md  Primary active workstream: custom wake-model training
  HANDOFF-custom-wakeword-training.md  Off-Pi custom wake model training/deploy workflow
  HANDOFF-wake-corpus-quality.md  Methodology for wake-corpus audio QA / artifact review
  HANDOFF-usb-mic-wake.md   Parked cheap-USB mic wake/AEC follow-up plan
  HANDOFF-mic-quality-v2.md     Empirical history: AEC sweeps, BEST_A, triple-stream architecture
  HANDOFF-mic-fusion-architecture.md  Design/plan (draft): pluggable-mic boundary + N-leg wake fusion
  HANDOFF-vad-experiments.md    Active workstream: VAD/mic-stream A/B matrix, why Cell 0 wins, raw+AGC followup
  HANDOFF-aec.md                Acoustic echo cancellation engine
  HANDOFF-hotplug-resilience.md  Runtime mic/DAC/satellite attach-detach convergence (no crash-loop)
  HANDOFF-speaker-output-reference.md  Chosen output-owner / true speaker-reference direction
  HANDOFF-chip-aec-portability.md  DAC-portable chip-AEC: clock-recovery design + roadmap
  HANDOFF-wake-telemetry.md     Triple-stream wake + per-event SQLite + funnel
  HANDOFF-xvf3800.md            Canonical reference for the XVF3800 mic
  HANDOFF-airplay.md       AirPlay glitch troubleshooting guide
  HANDOFF-apple-music.md   Apple Music integration research + plan (no code yet)
  HANDOFF-dlna.md          DLNA/UPnP media-input design (no code yet)
  HANDOFF-peering.md            Multi-Pi wake arbitration (off by default)
  HANDOFF-persistent-live-session.md
  HANDOFF-voice-music-control.md
  HANDOFF-volume.md             Source-aware volume coordinator
  multi-user-spotify.md
  audit-pending-followups.md    Open Tier 2/3 follow-ups
  ...                           additional HANDOFF, proposal, research,
                                and archived/history docs are mapped below

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
                                rolling back to a pre-outputd tree
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
   `deploy-to-pi.sh`, validates with `jasper-doctor`. It treats
   `PI_HOST` as the SSH target and `JASPER_HOSTNAME` as the speaker
   identity, so IP-based adoption doesn't leak into cert/URL state.
   Emits
   structured `event=onboard.<phase> status=<s>` lines parallel to
   the Pi-side daemon logging convention.
3. **`scripts/_lib.sh`** — shared header. Sources `.env.local`,
   exports `PI_HOST`/`PI_USER`, keeps the legacy
   `JASPER_HOSTNAME` → `PI_HOST` SSH fallback for older helpers, and
   records optional `JASPER_HOSTNAME` for speaker identity. New scripts
   should set/read `PI_HOST` for SSH transport and reserve
   `JASPER_HOSTNAME` for identity/cert URLs. Exposes a
   `write_laptop_state` helper so the onboarder and the `use` switcher
   stay in template-sync.
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
| [PRIVACY.md](PRIVACY.md) | Operators / OSS reviewers | What leaves the device, what stays local, retention defaults, and mic mute scope |
| [CHANGELOG.md](CHANGELOG.md) | Maintainers / release followers | Keep-a-Changelog release notes; release tags are maintainer-cut (`v0.1.0` marks OSS launch) |
| [LICENSE](LICENSE) | Anyone redistributing | Apache 2.0 |
| [NOTICE](NOTICE) | Anyone redistributing | Project notice plus pointer to third-party attribution inventory |
| [LICENSE-third-party.md](LICENSE-third-party.md) | Redistributors / maintainers | First-pass third-party software, asset, model, and data attribution inventory |
| [QUICKSTART.md](QUICKSTART.md) | First-time speaker builder | Raspberry Pi Imager password-SSH flow → boot → `scripts/onboard.sh --adopt` → working speaker in ~30 min. Carries the chosen hostname through every step. |
| [BRINGUP.md](BRINGUP.md) | Operator flashing a fresh Pi | Step-by-step from blank SD card to working speaker — OS flash, XVF firmware, dial, satellites, calibration |
| [PLAN.md](PLAN.md) | Project planning | v1 phased build, future roadmap |
| [docs/extensibility.md](docs/extensibility.md) | Maintainers / AI / extension contributors | **Start here before adding a modular subsystem.** The cross-cutting extensibility doctrine: the one invariant (host-mediated indirection), the five extension contracts (tools, sources, model providers, hardware profiles, features), the *what-kind → which-pattern* decision tree, and the build-now-vs-defer trust gradient. Frames the per-contract docs that follow. |
| [docs/tool-platform-plan.md](docs/tool-platform-plan.md) | Maintainers / AI | Vision, research, findings, rationale, and phased plan for turning JTS integrations into an extensible tool platform (trust gradient: first-party → trusted PRs → eventual marketplace). Records the shipped Phase-1.5 pieces: the `labels` facet, pack-first catalog, singleton packs for standalone tools, generated pack detail pages, full prompt override/reset, and the built-in `/tools/` on/off catalog wizard |
| [docs/research-tool-plan.md](docs/research-tool-plan.md) | Maintainers / AI | Vision, design, and phased roadmap for the async "research this and tell me later" tool: a fast `research(query)` tool that hands the question to a pluggable text LLM (OpenAI v1, Anthropic v2) running in a bounded background job, then reads a <=30 s summary back through the existing timer-fire announcement path. Records the shipped defaults (background+poll, no webhook, short spoken answers, spend accounting, etiquette hardening, and privacy-safe status/doctor observability) and the deferred future (Anthropic, full barge-in, richer interaction history). |
| [docs/conversation-history-plan.md](docs/conversation-history-plan.md) | Maintainers / AI | Execution plan for the first JTS **Feature** (per the extensibility doctrine): a household-visible `/chat` log of perceived-command-in / response-back with local capture controls. Native-first transcript capture, a dedicated `ConversationStore`, static ES-module page, `/state.chat`, doctor coverage, and opt-in / mic-mute-gated / retention-capped privacy are implemented; Gemini transcript capture and richer filtering remain deferred. |
| [docs/examples/tool_pack_starter.py](docs/examples/tool_pack_starter.py) | Trusted tool-pack contributors | Non-production postcard example of a copyable capability pack: `CapabilityPack`, `CatalogPack`, explicit `ToolDefinition`, `PythonExecutor`, labels, timeout, risk flags, and deps/build shape. Tests import it so the example cannot drift from the real boundary. |
| [docs/install-update-resilience-plan.md](docs/install-update-resilience-plan.md) | Maintainers / AI | **Planning brief (not operational truth).** Problems + open questions for hardening the install/update flow across Pi hardware tiers (512 MB–16 GB), fresh-vs-in-service-update, large version jumps, and runtime hot-plug/unplug. Origin: a 2026-06-21 jts2 update that OOM-killed the build (and nginx/voice) mid-install. Carries four ready-to-paste workstream prompts (memory-safe builds, atomic/recoverable updates, hot-plug resilience, tier-aware install + testing). |
| [docs/HANDOFF-runtime-memory.md](docs/HANDOFF-runtime-memory.md) | Maintainers / AI | **Operational.** Current always-on runtime memory decisions: chip-AEC defaults to one wake detector, optional chip beams are explicit custom opt-ins, `/system/` Home Assistant status runs through a child-process cache, the dashboard shows root cgroup memory buckets, and the remaining high-leverage RAM options are tracked without turning them into scattered TODOs. |
| [docs/phone-mic-relay-plan.md](docs/phone-mic-relay-plan.md) | Maintainers / AI | **Design + build plan (`/correction/` + `/sync/` relay + USB-C-mic-on-phone BUILT, gated default-off; on-device validation pending).** How to capture the phone mic in a browser for room/balance/sync/crossover measurement on iOS + Android with **no trusted cert on the Pi and no per-device cloud infra**: a static capture page on a trusted origin (jasper.tech) + a stateless, end-to-end-encrypted **dead-drop relay** the Pi pulls from (O(1) for the whole fleet, vs the rejected O(N) per-Pi-cert path). A Pi-owned **opaque `capture_spec`** (kind/duration/constraints/stimulus/UI) keeps one page + one relay agnostic across measurement types; **server-driven UI as data, not code** (the security boundary); the relay carries the guided room setup and host-synchronized "phone records, Pi plays, Pi reports sweep complete" handshake. WebRTC LAN-direct passthrough validated but deferred. |
| [docs/HANDOFF-install-update-transaction.md](docs/HANDOFF-install-update-transaction.md) | Maintainers / AI | **Operational** (Workstream B, landed). How a JTS update is a transaction: the build manifest is the verified-install marker (written last, gated by `set -e`, so a failed update never advertises a SHA it isn't running), deploy verification covers voice/AEC/renderers via `jasper-doctor` (broken-vs-idle), and collateral OOM kills are surfaced/gated. Includes the rollback/A-B analysis (cheapest "never worse than before"; full A-B deferred) and the Workstream-C seam. |
| [docs/install-hardware-tier-and-staleness.md](docs/install-hardware-tier-and-staleness.md) | Maintainers / AI | **Design note + recommendation (Workstream D output).** Findings on making the installer hardware-tier-aware (RAM/CPU/arch detected up front, orthogonal to the full/streambox *profile*) and the version-skew risk question. Bottom line: migrations are convergent so being far behind is not a migration-pile-up risk; it amplifies risk via cold build caches (the OOM-prone WebRTC/Cargo rebuilds) — so stepwise updates are rejected in favor of safe builds (A) + atomic updates (B). Includes the cross-SKU test strategy and the scoped tier-detection/arch-guard PR. |
| [docs/OSS-READINESS-TOP-FIVE.md](docs/OSS-READINESS-TOP-FIVE.md) | Maintainers / OSS reviewers | Contributor "files to know" register + the original top-five framing (priority list superseded by LAUNCH-READINESS.md) |
| [docs/REVIEW-google-oss-readiness.md](docs/REVIEW-google-oss-readiness.md) | Maintainers / OSS reviewers | Historical point-in-time OSS-readiness review; not current operational truth |
| [docs/REVIEW-2026-06-04-deep-dive.md](docs/REVIEW-2026-06-04-deep-dive.md) | Maintainers / OSS reviewers | **Superseded.** 23-agent parallel code-review snapshot of `main` @ `b4417b1`; do not drive work from this doc — see [LAUNCH-READINESS.md](docs/historical/LAUNCH-READINESS.md) |
| [docs/REVIEW-2026-06-04-big-rocks.md](docs/REVIEW-2026-06-04-big-rocks.md) | Maintainers / OSS reviewers | **Superseded.** Companion to the 2026-06-04 deep-dive: larger structural/architecture items from the same review pass |
| [docs/REVIEW-2026-06-04-small-wins.md](docs/REVIEW-2026-06-04-small-wins.md) | Maintainers / OSS reviewers | **Superseded.** Companion to the 2026-06-04 deep-dive: contained bug/hygiene/doc-staleness items from the same review pass |
| [docs/REVIEW-2026-06-12-oss-due-diligence.md](docs/REVIEW-2026-06-12-oss-due-diligence.md) | Maintainers / OSS reviewers | **Superseded.** Staff-engineer-style OSS due-diligence pass against `main` @ `6772b81a`; companion to the 2026-06-04 review series |
| [docs/REVIEW-deep-audit-2026-07-11.md](docs/REVIEW-deep-audit-2026-07-11.md) | Maintainers | Point-in-time whole-codebase deep-audit report (677 verified findings, grades, fix waves); session-artifact, not current operational truth |
| [docs/REVIEW-deep-audit-ledger.md](docs/REVIEW-deep-audit-ledger.md) | Maintainers | Live findings tracker joined to the deep-audit report by DA-NNNN id — per-finding status/disposition/PR, consolidation triage, owner decisions, validation owed |
| [docs/audio-paths.md](docs/audio-paths.md) | Operator + AI | Reference: the two ALSA paths to the dongle, which volume knob attenuates which path, how end-of-turn timing anchors on TTS drain, and the canonical checklist for adding a new music source |
| [docs/HANDOFF-speaker-output-reference.md](docs/HANDOFF-speaker-output-reference.md) | Audio / voice architects | Chosen direction for a JTS-native output owner, true speaker-output reference, TTS playout ledger, and robust assistant-speech barge-in |
| [docs/HANDOFF-audio-latency-foundation.md](docs/HANDOFF-audio-latency-foundation.md) | Audio architects | Local-audio-latency work: the lean File-capture lane (Stage 4, default-OFF, soak-gated), USB-input bridge latency, the snapcast bond buffer, the CamillaDSP v4 resampler object schema, chip/software AEC optionality, and the hard rules against re-architecting the topology |
| [docs/HANDOFF-usb-low-latency.md](docs/HANDOFF-usb-low-latency.md) | Audio architects | **Operational + evidence gate.** Current `usb_low_latency_48k` route: Rust UAC2 bridge, fan-in USB input resampler, CamillaDSP protection/correction, outputd final-reference ownership, Apple DAC tuned floor, rejected lower settings, `jasper-route-latency-artifact`, and the doctor route-latency artifact gate. The older lean-FIFO bypass is preserved there only as historical/deferred context. |
| [docs/HANDOFF-usb-latency-measurement.md](docs/HANDOFF-usb-latency-measurement.md) | Audio architects / maintainers | **Operational + reference.** Measurement reference for USB-input latency: the hardware-measured results (~55.5 ms full chain, electrical `:9891` + analog Scarlett-loopback methods that compose exactly), the per-stage breakdown, the productized-settings table (every value is the shipped code default or auto-pass-armed — the fresh-install reference), and the host/bench reproduction setup (Mac output pinning, gadget recovery, click-WAV spec, descend-to-floor). |
| [docs/HANDOFF-audio-graph-consolidation.md](docs/HANDOFF-audio-graph-consolidation.md) | Audio architects | **Campaign plan.** Consolidating the audio graph onto SHM rings + the `jts_ring` ioplug and deleting every duplicate/legacy path (snd-aloop, Python usbsink pump, lean lane, transport_pipe, rate_match): the file-level no-dupes audit, sequenced phase map with per-phase gates/rollbacks, renderer ring-ingress design, risk register, and campaign done criteria |
| [docs/RESEARCH-pipewire-low-latency.md](docs/RESEARCH-pipewire-low-latency.md) | Audio architects | Research artifact: how PipeWire's *actual* source achieves low latency + clock resilience (the `spa_dll` delay-locked loop, driver/follower double-buffered quantum, timer/headroom ALSA model, xrun recovery, zero-copy), a JTS verdict per technique, and a principle-aligned adoption plan centered on lifting one shared DLL primitive. We do NOT use PipeWire — this mines its algorithms, not its architecture |
| [docs/AEC-DIAG-*.md](docs/AEC-DIAG-06-xvf-format-level-profile.md) | Audio diagnostics | Dated AEC diagnostic notes and active probe runbooks for the outputd/chip-ref/XVF timing investigation. Current entry point: `AEC-DIAG-06-xvf-format-level-profile.md` |
| [docs/satellites.md](docs/satellites.md) | Anyone working on a satellite device | Cross-cutting design + roadmap for ESP32 satellites (dial, AMOLED mic, etc.) |
| [docs/dumb-endpoint-bringup.md](docs/dumb-endpoint-bringup.md) | Operator bringing up a Zero 2 W streambox | Lab runbook for cheap Zero-class JTS: the streambox install profile (local renderers, DSP, shared capability-gated UI) plus the planned `active_crossover` output topology. "Endpoint behaviour" is now the runtime multiroom follower role, not a separate install tier |
| [docs/HANDOFF-supply-chain.md](docs/HANDOFF-supply-chain.md) | Maintainers / release engineers | Canonical provenance policy for deploy/build-time third-party inputs, checksum expectations, and accepted gaps |
| [docs/HANDOFF-build-sandbox.md](docs/HANDOFF-build-sandbox.md) | Maintainers / install-path | How `install.sh` runs heavy compiles (webrtc AEC3, jasper_aec3, Rust daemons, shairport-sync/nqptp) RAM-bounded + cgroup-contained so an OOM during an in-service update kills only the build, never a live daemon. Workstream A of the install-update resilience brief; the inverse-of-audio-daemon build policy |
| [docs/testing-tooling.md](docs/testing-tooling.md) | Anyone writing a test/measurement script | Index of every capture / wake-word-scoring / forensic / diagnostic tool in the repo. **Read before writing a new one** — many parallel tools have been built before this index existed. |
| [docs/DEEP-AUDIT-PLAYBOOK.md](docs/DEEP-AUDIT-PLAYBOOK.md) | Maintainers / AI agents (pre-launch) | The heavyweight whole-codebase audit method: many sub-agents comb the tree close to line by line for dead code, drift, duplication, unjustified complexity, and the **unknown unknowns** (orphans, dead flags, off-map corners). Capital-T-truth bar (a clean result is *suspect*), coverage ledger, adversarial verification, honest grades with confidence. Driven by the `/deep-audit` command. NOT a per-diff review — that's `/code-review ultra`. |
| [docs/HANDOFF-observability.md](docs/HANDOFF-observability.md) | Operator + AI | Logging/observability model (heartbeat-vs-forensic split, the three steady-state verbosity hotspots, persistent-journald rationale) + the approved per-subsystem debug-mode toggle, flight-recorder, and download-diagnostics design |
| [docs/HANDOFF-privilege-separation.md](docs/HANDOFF-privilege-separation.md) | Maintainers / security | Threat model + ADR for hardening and de-rooting the daemons: Phase 1 hardened-root stanza (landed, measured), the restart-broker + invisible-token, the Tier-A user drop with its recovery-validation matrix, and the tracked Tier-B follow-up |
| [docs/HANDOFF-control-plane-auth.md](docs/HANDOFF-control-plane-auth.md) | Maintainers / security | Device-to-device / household control-plane auth: why the per-device CSRF control token cannot authenticate cross-speaker grouping, the prior-art research, the household-credential design, shipped Phase A-C status, and the Phase D scope decision. Also folds in the landing-page mic-mute token-delivery fix |
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
- [`HANDOFF-pricing-editor.md`](docs/HANDOFF-pricing-editor.md) —
  Per-model voice pricing: model-ID-keyed rates with dated defaults in
  `jasper/data/model_pricing.json`, the `/voice` "Pricing rates" editor
  writing per-model overrides, the `/voice` spend cap status/settings,
  unknown-model handling, and a chatbot-research prompt + JSON import for
  refreshing rates. Why provider APIs can't supply voice prices
- [`HANDOFF-prompting.md`](docs/HANDOFF-prompting.md) — The voice
  prompting playbook. Cross-provider principles (conditional over
  absolute, positive framing for tool calls, brevity vs. structure),
  provider deltas (OpenAI gpt-realtime-2 / Gemini 3.1 Flash Live /
  Grok think-fast-1.0), a section-by-section walk-through of the
  current `SYSTEM_INSTRUCTION`, a tool-prompt cookbook including
  the `build_tool()` first-paragraph truncation, a pitfalls
  catalog with symptoms, and a "Recommended edits to current code"
  punch list. **Start here for any edit to `SYSTEM_INSTRUCTION` in
  `jasper/voice/prompt.py` or any tool description in
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
  `http://jts.local/rooms/`. P2P via mDNS-SD + multicast UDP, no
  hub, no SPOF. **Start here for `jasper/peering/`, the wake-handler
  restructure, or anything related to the `/rooms/` wake-response card.**
- [`HANDOFF-multiroom.md`](docs/HANDOFF-multiroom.md) — **In-progress
  grouped playback.** Stereo-pair control/observability, the music
  dataplane, and member-local TTS are built and off by default; the
  current handoff names the remaining calibration, wider bond UI, and
  validation gates. Covers synchronized grouped playback across
  speakers: stereo pair, 2.1 with a wireless sub, and multi-room,
  using Snapcast plus the JTS peering/identity substrate. **Start here
  for any multi-room / stereo-pair / wireless-sub work.**
- [`dumb-endpoint-bringup.md`](docs/dumb-endpoint-bringup.md) —
  Raspberry Pi Zero 2 W streambox: today's lab runbook (OS Lite +
  `snapclient` + the multi-room spike) and the decided product path —
  one JTS package with two install profiles (`full` / `streambox`),
  not a parallel codebase. The streambox profile installs local
  renderers + outputd/CamillaDSP + the shared JTS landing page filtered
  by capabilities. "Endpoint behaviour" — a box that just plays a bonded
  channel — is now the runtime multiroom **follower** role, not a
  separate install tier. Planned work adds an `active_crossover` topology
  capability with local `/crossover`.
- [`HANDOFF-aec.md`](docs/HANDOFF-aec.md) — AEC architecture +
  investigation (engine choices, chip-AEC profile, software fallback)
- [`CHIP-AEC-EXPERIMENT.md`](docs/CHIP-AEC-EXPERIMENT.md) —
  2026-05-29 chip-AEC lab findings and next-productionization plan.
  Option D is now a positive lab result, not a closed negative:
  direct source fanout to the DAC + XVF3800 USB-IN reference made the
  split-DAC topology clock-stable, and ASR fixed gated `150°/210°`
  beams were the best tested output. The production path now ships
  behind the profile selector and is used by `auto` on 6-channel
  XVF3800 hardware; the checked-in
  `scripts/chip-aec-*.sh` scripts +
  `jasper/chip_aec_experiment.py` are lab infrastructure, and
  `chip-aec-teardown.sh` fully reverts. **Read the doc before running.**
- [`HANDOFF-chip-aec-portability.md`](docs/HANDOFF-chip-aec-portability.md) —
  **Design-of-record (living draft).** Making chip-AEC work across any DAC:
  the clock-domain decision (digital SRO clock-recovery, *not* a per-period
  `snd_pcm_delay` delay line), the JTS/JTS3/JTS5 hardware test matrix, and
  the YAGNI-gated layered roadmap (Layer 0 observe → classify → compensate).
  Supersedes an earlier (unlanded) production-design draft's
  per-period delay-line mechanism.
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
- [`HANDOFF-audio-capability-platform.md`](docs/HANDOFF-audio-capability-platform.md) —
  Cross-cutting plan for turning the current mic/AEC/DAC work into a
  hardware-capability platform: detected mic/DAC facts, profile
  selection, validation artifacts, fallback behavior, dashboard truth,
  and future onboarding. Read before generalizing chip-AEC to new DACs,
  adding a second mic family, or moving corpus/onboarding modes into
  productized hardware setup.
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
- [`HANDOFF-custom-wakeword-training.md`](docs/HANDOFF-custom-wakeword-training.md) —
  Productization plan for converting JTS wake-corpus recordings into
  custom wake-word models trained off-Pi with LiveKit/openWakeWord-
  compatible tooling, then evaluated, thresholded, and deployed back
  into the existing JTS multi-leg fusion runtime. Read this before
  building corpus export, feature extraction, cloud training, model
  registry, shadow-mode, or one-click training UX.
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
  Live implementation plan & current-code gap analysis for robust
  assistant-speech barge-in (provider-agnostic spine + per-provider
  packs). The contract itself lives in
  [`HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md) and
  [`HANDOFF-speaker-output-reference.md`](docs/HANDOFF-speaker-output-reference.md);
  the 2026-05-23 option-costing record is a tagged historical appendix.
- [`barge-in-build-prompts.md`](docs/barge-in-build-prompts.md) —
  Execution artifact (session handoff): the step sequencing + copy-paste
  per-window agent prompts for building barge-in against the plan in
  `HANDOFF-barge-in.md`. Retire once barge-in has shipped.
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
- [`HANDOFF-identity.md`](docs/HANDOFF-identity.md) — Speaker
  identity: the three loosely-coupled names (OS hostname, Avahi's
  effective post-collision name, `JASPER_HOSTNAME`), the identity
  reconciler + `/var/lib/jasper/identity.env`, how the management
  allowlist survives an mDNS collision rename without locking the
  household out, the doctor/`/state` surfaces, and the supported
  rename flow (`scripts/rename-speaker.sh`). Read before touching
  `jasper/http_security.py`, hostname handling, or multi-speaker
  setups.
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
- [`HANDOFF-hotplug-resilience.md`](docs/HANDOFF-hotplug-resilience.md) —
  Runtime hardware attach/detach convergence ("treat it like a
  computer"): mic/XVF3800, output DAC/dongle, satellites can be
  plugged/unplugged while running and the speaker converges both
  directions with no redeploy, restart, or crash-loop. The mic
  presence-gate (`jasper-voice` `ConditionPathExists` on a reconciler-
  written marker + a clean `66` exit), why the output owner and
  satellites already converge, and the plug/unplug hardware-pass
  checklist. Read before touching the no-mic/no-DAC park paths in
  `deploy/bin/jasper-aec-reconcile`, `jasper-voice.service`, or the
  `ConditionPathExists`/`ExecCondition` device gates.
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
- [`HANDOFF-dlna.md`](docs/HANDOFF-dlna.md) — **Research / design
  only, not yet implemented.** Design for DLNA/UPnP media input as
  a network-only music source via gmrender-resurrect: why DLNA
  rather than Google Cast (hardware-fused Cast auth no OSS project
  has solved), the gmrender vs upmpdcli renderer analysis, the
  Python state/preemption sidecar, and the decision records (GENA
  eventing vs polling, Pause+disarm preemption, sidecar-owned
  preempt proxy). The audio-path section is written against the
  current per-source fan-in lane / `jasper-outputd` topology —
  DLNA adds one private snd-aloop lane (allocation is full, so it
  must reuse one). **Start here before any `jasper/dlna/` work.**
- [`HANDOFF-remote-updates.md`](docs/HANDOFF-remote-updates.md) —
  Research only, no implementation yet. Design space for an OTA
  "Check for updates" button on the management dashboard: option
  survey (`git pull` → GitHub Releases + poll → RAUC A/B
  partition swap), recommended staged build-out (CI first,
  auto-release, then the button), and the open questions before
  specing. Referenced from PLAN.md.
- [`HANDOFF-persistent-live-session.md`](docs/HANDOFF-persistent-live-session.md)
  — **Historical** (per AGENTS.md "Historical handoffs are tagged at
  the top"). Frozen-in-time
  session-pickup brief from 2026-05-05 when the persistent-single
  Gemini Live rework was scoped. Preserved for primary-source
  archaeology; do NOT read for current state. Current operational
  truth: [HANDOFF-voice-providers.md](docs/HANDOFF-voice-providers.md).
- [`HANDOFF-voice-music-control.md`](docs/HANDOFF-voice-music-control.md)
  — Source-aware transport (AirPlay/Spotify Connect) + volume
- [`HANDOFF-volume.md`](docs/HANDOFF-volume.md) — Source-aware
  volume coordinator (one canonical `listening_level`, dispatched to
  whichever source is active, observed inbound at 1 Hz)
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
  install-time source archive pins, firmware dependency pins,
  hash-checked model downloads, and accepted gaps for apt, Python,
  and PlatformIO transitive resolution.
- [`HANDOFF-usb-gadget.md`](docs/HANDOFF-usb-gadget.md) — **Canonical**
  for the composite USB gadget: the always-on USB management network
  (`ncm.usb0`, NetworkManager keyfile, scoped dnsmasq, no IP
  forwarding/NAT), the function truth table shared with USB audio,
  OS-support verification (Windows/macOS NCM, dwc2 endpoint capacity),
  the relationship to Raspberry Pi OS's own `rpi-usb-gadget` rescue
  feature, and the hardware-validation checklist.
- [`HANDOFF-usbsink.md`](docs/HANDOFF-usbsink.md) — Optional USB
  audio-input gadget: host-control preemption, source wizard behavior,
  and how the USB-in lane feeds fan-in. Gadget/ConfigFS ownership and
  the management network now live in HANDOFF-usb-gadget.md above.
- [`HANDOFF-audible-feedback.md`](docs/HANDOFF-audible-feedback.md) —
  Pre-rendered audio cue subsystem: registry, cache lifecycle, CLI,
  how to add a new reactive or proactive cue. Start here when a
  failure path needs to "say something" rather than fall silent.
- [`HANDOFF-audio-measurement-core.md`](docs/HANDOFF-audio-measurement-core.md)
  — **Living architecture + product plan** (2026-06-19) for the shared
  audio measurement/calibration core that room correction, active-crossover
  calibration, and pair/leader-follower balance all build on: the layered
  calibration product (L0 fail-closed crossover / L1 phone-mic level match /
  L2 calibrated-mic FR-phase), the multi-volume verdict, and a strangler-fig
  refactor roadmap (kernel extraction + single GraphValidator). The
  output/measurement-side sibling of `HANDOFF-audio-capability-platform.md`.
- [`HANDOFF-correction-revision-plan.md`](docs/HANDOFF-correction-revision-plan.md) —
  execution plan of record for the layered correction/tuning revision
  (speaker → room → preference pipeline, shared measurement kernel,
  level-match ramp, verify-acceptance loop, tuning LLM; hardware-free
  vs hardware-gated roadmap).
- [`HANDOFF-correction.md`](docs/HANDOFF-correction.md) — HTTPS
  correction measurement hub at `/correction/`: room correction,
  active-crossover mic measurement, and bass/subwoofer tuning surfaces;
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
- [`HANDOFF-dsp-graph-carrier.md`](docs/HANDOFF-dsp-graph-carrier.md) —
  Design-of-record for composing preference EQ + room correction on top of
  ANY output topology (flat / active 1/2/3-way + sub / distributed
  leader-follower): the graph-carrier dispatcher that re-emits the loaded
  CamillaDSP graph in its own shape — or fails closed with a typed reason —
  the program/driver split-mixer seam, the shared stereo-domain prefix, and
  the deferred distributed-active boundary.
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
- [`HANDOFF-distributed-active.md`](docs/HANDOFF-distributed-active.md) —
  **Design-of-record (proposed 2026-06-20)** for running an active
  speaker's driver-domain crossover (Layer A) as a wireless **follower**,
  while the leader owns the program domain (room correction + preference
  EQ) and streams the corrected stereo program. Owns the
  distributed-active boundary the graph-carrier doc defers: the
  CamillaDSP-re-entry follower engine, the role/capture contract, the
  follower-409 narrowing, the local-vs-wireless subwoofer split, the
  fail-closed/clock-domain safety story, and the safest-first slice plan.
- [`active-crossover-information-design.md`](docs/active-crossover-information-design.md)
  — **Product and architecture design of record** for the active crossover
  builder: first-class manual control, calibrated-microphone automatic tuning,
  one shared crossover model, explicit overwrite/apply/rollback semantics,
  fixed-axis driver measurement, observability, ownership boundaries, and the
  simple delivery path from level matching through full crossover design.
- [`dual-apple-dac-lab.md`](docs/dual-apple-dac-lab.md) —
  Lab-only runbook for validating two Apple USB-C to 3.5 mm adapters
  as one stereo DAC per speaker. Keeps the experiment outside the
  product output path, requires serial-pinned direct hardware PCMs,
  and starts with no-speaker silence/identity/drift evidence.
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
- [`docs/research/2026-06-19-active-crossover-calibration/`](docs/research/2026-06-19-active-crossover-calibration/README.md)
  — 2026-06-19 multi-agent research/design snapshot: mic-driven active
  crossover level matching, the "is it just level matching?" + calibrated
  vs uncalibrated-iPhone-mic adjudication, a live JTS3 "crossover not
  applied" diagnosis, codebase reuse/gap analysis, and the shared
  audio-measurement-core + layered-product vision. Source material; the
  shipped design lives in the canonical calibration handoffs.
- [`docs/research/balance-sync-calibration.md`](docs/research/balance-sync-calibration.md)
  — 2026-06-13 prior-art synthesis for multi-speaker balance versus
  sync calibration, including the Snapcast sync loop, Snapcast
  per-client latency, and leader-side CamillaDSP delay ownership split.
  Treat as source material; operational guidance lives in
  `HANDOFF-multiroom.md` and `dumb-endpoint-bringup.md`.
- [`HANDOFF-management-ui.md`](docs/HANDOFF-management-ui.md) —
  Proposal (created 2026-05-22, not yet implemented) for
  restructuring the `jts.local` management surface into a tighter
  layout with a first-run setup wizard.
- [`PROPOSAL-dac-profile-registry.md`](docs/PROPOSAL-dac-profile-registry.md)
  — **Proposal / implementation handoff** (updated 2026-06-11) —
  scoped design for the data-driven DAC profile registry now scaffolded
  in `jasper/audio_hardware/dac.py`, covering Apple USB-C, HiFiBerry
  DAC8x-family, and dual-Apple composite output profiles.
- [`HANDOFF-canonical-ui-migration.md`](docs/HANDOFF-canonical-ui-migration.md)
  — **Historical** (snapshot 2026-05-31) — the handoff for the
  now-completed canonical design-system migration of all wizards.
  Primary-source archaeology only; current management-UI direction
  lives in [`HANDOFF-management-ui.md`](docs/HANDOFF-management-ui.md).
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

Fresh installs default to `JASPER_AUDIO_INPUT_PROFILE=auto`. On the
recommended 6-channel XVF3800 hardware plus a supported/calibrated output
DAC profile, `auto` resolves to the chip-AEC profile: `jasper-outputd`
fans out the final speaker buffer to the XVF3800 USB-IN reference, the
chip emits fixed 150°/210° AEC beams, and the bridge forwards the selected
chip beam to `jasper-voice` while WebRTC AEC3 is bypassed. If that hardware
path is unavailable or the active output DAC still needs calibration, `auto`
falls back to software AEC3 or a direct mic path rather than stacking
incompatible processing.

The chip is still useful — its **beamforming, noise suppression,
and AGC** all run in the XVF processing pipeline. The key rule is not
to double-process: chip-AEC profiles do not also arm software raw/DTLN
wake legs; software AEC3 is the fallback for hardware that cannot use
chip-AEC.

**Wake/input configuration is profile-first.** The `/wake/` page exposes
the canonical choices (`auto`, `xvf_chip_aec`,
`xvf_chip_aec_testing`, `xvf_software_aec3`, `direct_mic`) and keeps
individual AEC/raw/DTLN/chip-leg toggles as advanced custom controls
for corpus tests and nonstandard hardware.
Changing a profile or custom layer runs `jasper-aec-reconcile`, which
restarts the affected bridge/voice services and updates `/state`,
doctor, and the dashboard.

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
procedure is in
[`BRINGUP.md` "XVF firmware: switch to 6-channel variant via DFU"](BRINGUP.md#xvf-firmware-switch-to-6-channel-variant-via-dfu);
the known-good version constants are tracked in
[`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py).
On the 2-channel firmware the bridge stays disabled and voice
reads the chip's processed conference channel directly. `install.sh` runs
`jasper-aec-reconcile`, which auto-detects + auto-enables when the
hardware is ready and clears stale UDP mic config when the Array is
missing.

### What's installed and at what cost

Numbers are **Pss** (proportional set size — shared libs deduplicated;
the honest "private cost" measure) on a Pi 5, after the lazy-import,
openwakeword stub diet, and jasper-input httpx removal landed.

| Component | Default | RAM (Pss) | CPU |
|---|---|---|---|
| `jasper-voice` (wake + LLM + tools) | Active | ~140-150 MB | ~12% of one core during a session |
| `jasper-aec-bridge` (software AEC) | **Active** on 6-ch firmware, **disabled** on 2-ch | +85 MB | +3% of one core |
| `jasper-aec-init` (boot-time chip init) | follows aec-bridge | one-shot, ~0 | ~0 |
| `jasper-wifi-guardian` (NM keyfile/profile self-heal) | Active (oneshot) | one-shot, ~0 | ~3-5 ms |
| `jasper-wifi-recover` (Wi-Fi periodic recovery nudge) | Active timer | ~0 resident; one-shot only | healthy tick is one NM read + narrow kernel-log check every ~3 min; repair path only for brcmfmac scan suppression or Wi-Fi down |
| `jasper-camilla` (always-on CamillaDSP, ducking) | Active | ~12 MB | <1% |
| `jasper-control` (HTTP API + dial routing) | Active | ~35 MB | ~0.1% idle |
| `jasper-input` (HID accessory bridge) | Active | ~16 MB | ~0% idle |
| `jasper-accessory-reconcile` (optional accessory mic profile gate) | Active oneshot | ~0 resident | boot/deploy and Bluetooth pair/connect/forget only |
| `jasper-wiim-remote-mic` (WiiM Remote 2 BLE mic adapter) | Profile-gated; active only when paired WiiM Remote 2 is present | 0 MB off; ~15 MB on, bounded by MemoryMax=100M | ~0% idle; decode only while the remote mic streams |
| `jasper-mux` (renderer arbitration) | Active | ~13 MB | ~0% idle |
| `jasper-usbsink` (USB audio source) | **Disabled by default**; Rust data plane when on | 0 MB off; low single-digit MB for the bridge, plus host-volume helper | low; ALSA-period Rust bridge while host streams |
| `jasper-usbgadget` (composite ConfigFS gadget: always-on USB network + optional audio) | **Active by default** (network function); audio function follows the usbsink toggle above | one-shot, ~0 own footprint; ~1 MB kernel modules once composed — see [docs/HANDOFF-usb-gadget.md](docs/HANDOFF-usb-gadget.md) "RAM contract" | ~0 |
| `jasper-usbnet-dhcp` (scoped dnsmasq for the USB management network) | **Device-activated** — active only while `usb0` exists | 0 MB when `usb0` absent; bounded ≤16 MB when active | ~0% idle |
| `jasper-web` (Spotify / voice / Google / AirPlay / Sources / Wake / Wi-Fi / Transit / Home Assistant / Weather / Sound / Wake-Corpus / Speaker / Rooms / Tools wizards) | **Socket-activated** | ~0 idle, ~22 MB when open | n/a idle |
| `jasper-bluetooth-web` (BT pair UI) | **Socket-activated** | ~0 idle, ~17 MB when open | n/a idle |
| `jasper-correction-web` (HTTPS correction measurement hub) | **Socket-activated** | ~0 idle, ~15 MB when open | n/a idle |
| `jasper-dial-web` (dial onboarding UI) | **Socket-activated** | ~0 idle, ~9 MB when open | n/a idle |
| `jasper-system-web` (system dashboard at `/system/`) | **Socket-activated** | ~0 idle, ~12 MB when open | n/a idle |
| `jasper-chat-web` (conversation-history dashboard at `/chat/`) | **Socket-activated** | ~0 idle; not yet measured when open (same stdlib-server shape as `jasper-system-web`, bounded by `MemoryMax=90M`) | n/a idle |
| Single-card snd-aloop (Loopback) | Loaded at boot | ~0 | ~0 |
| dsnoop tap on music chain | Always present | ~0 | ~0 |

The six web-wizard daemons are socket-activated — systemd holds
their ports open and only spawns the daemon when a tab opens any of
its pages. `jasper-web` alone hosts fifteen URL surfaces (Spotify,
voice, Google, AirPlay, Sources, Wake, Wi-Fi, Transit, Home
Assistant, Weather, Sound, Wake-Corpus, Speaker, Rooms, Tools) on
fifteen loopback ports; the other five daemons each host one. Four of
the six (`jasper-web`, `jasper-bluetooth-web`, `jasper-correction-web`,
`jasper-dial-web`) exit after 10 min of no requests; `jasper-system-web`
and `jasper-chat-web` use a longer 30-min idle timeout since users may
leave those read-only dashboards open in a tab. Either way the
resident cost is zero between admin sessions. First request after
idle takes ~500-800 ms (Python startup); invisible during the OAuth
round-trip or BT pair flow.

**Total Pss baseline with AEC on**: ~318 MB jasper-* daemons +
~80 MB system/OS plumbing + page cache → typically ~770 MB used
out of 2 GB. On a 1 GB Pi, ~200 MB headroom with AEC on; ~280 MB
with AEC off. (The ~318 MB also covers two always-on Rust audio
daemons not broken out as rows above — `jasper-fanin` (renderer
fan-in summing) and `jasper-outputd` (final-output owner); both are
small native binaries.)

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

`jasper/xvf/xvf_host.py` is a JTS-owned USB control helper for the
XVF3800 command subset JTS uses. It is useful as a diagnostic tool
independent of AEC:

```sh
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host --list  # JTS-supported params
```

Read AEC convergence, inspect supported routing/profile values, change
beam parameters, etc. The JTS helper intentionally does not expose
filter-coefficient dumps; add and hardware-validate a narrow command
from XMOS documentation before relying on that workflow. Don't call
`SAVE_CONFIGURATION` — known brick hazard on certain firmware versions
(respeaker repo issue #8).

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

If you have a fresh Pi and want to set up a speaker, start with
[QUICKSTART.md](QUICKSTART.md). It follows the beginner path from
Raspberry Pi Imager through `scripts/onboard.sh --adopt` and the
first setup pages in ~30 minutes.

For the long-form operator runbook — hardware calibration, XVF
firmware, dial, satellites, room correction — use
[BRINGUP.md](BRINGUP.md).

If the repo is already deployed and you're just pushing changes:

```sh
# from your laptop:
bash scripts/deploy-to-pi.sh
# or with a non-default SSH target:
PI_HOST=192.168.1.42 JASPER_HOSTNAME=jts.local bash scripts/deploy-to-pi.sh
# or for a Zero 2 W streambox; fresh Zeros auto-resolve this way:
PI_HOST=jts4.local bash scripts/deploy-to-pi.sh
# or make the streambox intent explicit in the deploy log:
PI_HOST=jts4.local JASPER_INSTALL_PROFILE=streambox bash scripts/deploy-to-pi.sh
```

This is a thin wrapper that captures the current git SHA + branch
(via `git rev-parse`), preflights sudo before upload, rsyncs to the
remote user's `${HOME}/jts/`, then runs install.sh under sudo with
`JASPER_DEPLOY_SHA` / `JASPER_DEPLOY_BRANCH` env vars set. Passwordless
sudo is required for unattended deploys; an interactive terminal can
prompt through `ssh -tt` without storing the password. install.sh
writes the deploy metadata into `/var/lib/jasper/build.txt` so the
/system dashboard's "Software" card shows the real deployed version
instead of "unknown" (`.git/` is excluded from the rsync for speed).

The install script is idempotent.

There are exactly two install profiles: `full` and `streambox`. On a fresh
Raspberry Pi Zero 2 W with no persisted marker and no explicit
`JASPER_INSTALL_PROFILE`, the installer resolves to `streambox` so a tiny
board does not accidentally run the full brain profile; everything else
resolves to `full`. The former third tier (`endpoint` / `satellite`) has
been removed — those tokens are still accepted and map to `streambox`, so a
field box with a persisted `endpoint` marker auto-migrates to streambox on
its next deploy (a single `event=install_profile.migrate` log line records
it). `streambox` is the normal Zero capability set: local AirPlay / Spotify
Connect / Bluetooth / USB Audio Input, outputd/CamillaDSP, `/spotify`,
`/sources`, `/sound`, `/system`, `/rooms`, and correction/balance/sync
surfaces, but no local voice, wake word, mic/AEC, assistant providers, or
CamillaGUI. It reuses the shared landing UI with profile capabilities but
installs a scoped `jasper-web` service/socket template, so it does not bind
full-brain wizard ports. Both profiles use the same repo and deploy path;
deploy verifies the relevant nginx surface plus `jasper-control`'s
always-on `:8780/healthz`.

"Endpoint behaviour" is now purely the multiroom **follower** grouping role
at runtime — a runtime role, not a second frontend or a surprise package
rewrite. A full or streambox box that joins a pair as a follower parks its
local source resource groups, including advertise-side resources such as
the USB Audio Input gadget (and, on a full speaker, its voice/AEC brain),
through the grouping reconciler and exposes the stripped paired-follower
UI; when unpaired, those surfaces come back. Active-crossover driver-DSP
remains a separate topology capability. See
[docs/HANDOFF-multiroom.md](docs/HANDOFF-multiroom.md).

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
