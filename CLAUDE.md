@README.md

This file: AI-specific operational guidance for Claude Code
working in this repo. README.md (imported above) has the project
context — architecture, hardware, repo layout, subsystem overview,
deployment, debugging entry points. **Don't restate it here.** If
you find yourself explaining the project, fix README.md and
reference it from this file instead.

What goes here:
- Things easy to get wrong (model gotchas, file ownership lines,
  brick hazards)
- Operational shortcuts (specific scripts, env-var formats)
- AI behavioral rules specific to this codebase

Twin file: `AGENTS.md` mirrors this for non-Claude agents (the
`@README.md` import syntax is Claude-specific). Keep both in sync
when editing.

---

## Speaker hostname — single source of truth

`JASPER_HOSTNAME` (default `jts.local`) is the canonical name other
devices type in to reach the speaker. Set in `/etc/jasper/jasper.env`.

What derives from it (so you only set it once):
- Python: `Config.hostname` plus `JASPER_MANAGEMENT_URL` and
  `JASPER_SPOTIFY_SETUP_URL` defaults (`http://${JASPER_HOSTNAME}` and
  `http://${JASPER_HOSTNAME}/spotify` respectively).
- Bash scripts under `scripts/`: every `PI_HOST` default falls back to
  `${JASPER_HOSTNAME:-jts.local}`. So if you also export
  `JASPER_HOSTNAME` in your laptop shell, `fetch-pi-logs.sh`,
  `tail-pi-logs.sh`, `switch-voice-provider.sh`, etc. all target the
  right host without per-script overrides.

What does NOT derive (intentionally):
- The Pi's actual mDNS hostname (set with `hostnamectl set-hostname`
  + Avahi). Setting `JASPER_HOSTNAME` doesn't change what the Pi
  advertises — that's a separate, OS-level concern. Run hostnamectl
  first; then point `JASPER_HOSTNAME` at it.
- The Spotify OAuth bounce page at
  `https://jaspercurry.github.io/JTS/oauth-callback/` — it's static
  HTML and hard-codes `jts.local` as the bounce target. Forks running
  on a different hostname either fork-and-self-host the page (one
  constant, one re-deploy) or use the manual paste-back OAuth mode
  instead. Documented in `oauth-callback/README.md`.

---

## Renderer architecture — file map

`install.sh` source-builds shairport-sync (AirPlay 2) + nqptp,
drops in librespot (rust, via raspotify .deb) + bluez-alsa +
bt-agent, and owns the full systemd unit per renderer.

`jasper/renderer.py:RendererClient` reads renderer state from each
daemon's own surface:
- librespot → `/run/librespot/state.json` (written by the
  `--onevent` hook `/usr/local/bin/jasper-librespot-event`)
- shairport-sync → MPRIS PlaybackStatus over busctl
- bluez-alsa → `bluealsa-cli list-pcms`

`jasper-mux.service` does latest-source-wins preemption: when a
new source transitions to playing while another is already active,
it pauses the older one.

Spotify volume control goes via the Spotify Web API (the multi-
account `spotify_router`) since librespot has no local control
HTTP — see [`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md).

---

## Voice provider switching — read first

The voice loop runs against any of three real-time speech-to-speech
APIs behind a single env var. Architecture and per-provider
trade-offs are in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md);
this section is the operational summary.

**Two ways to switch.** Either work; pick whichever fits the moment.

**Web UI (preferred, end-user friendly)** — visit
`https://jts.local/voice/` from any device on the LAN. The page
shows one card per provider for pasting API keys, picks model and
voice from curated dropdowns, and has a single radio group at the
top for "use this provider". Saving writes
`/var/lib/jasper/voice_provider.env` at mode 0600 and restarts
`jasper-voice`. Source: [`jasper/web/voice_setup.py`](jasper/web/voice_setup.py).

**Laptop-side script (operator-friendly, scriptable)**:

```sh
bash scripts/switch-voice-provider.sh           # show current
bash scripts/switch-voice-provider.sh gemini    # gemini-3.1-flash-live-preview
bash scripts/switch-voice-provider.sh openai    # gpt-realtime-2 (released 2026-05-07)
bash scripts/switch-voice-provider.sh grok      # grok-voice-think-fast-1.0
```

The script refuses to switch if the destination provider's API key
isn't already in `/etc/jasper/jasper.env` (`GEMINI_API_KEY`,
`OPENAI_API_KEY`, or `XAI_API_KEY`) or in the wizard-written
`/var/lib/jasper/voice_provider.env`. Set the key first via
either path; the script sets the provider and restarts
`jasper-voice` in one shot.

**Per-provider model env var** is independent of the provider switch
— `JASPER_GEMINI_MODEL`, `JASPER_OPENAI_MODEL`, `JASPER_GROK_MODEL`.
The `switch-gemini-model.sh` script (below, "Gemini model switching")
flips the *Gemini* model alias for within-Gemini fallback (3.1 ↔ 2.5)
and is independent of cross-provider switching.

**Pricing trade-off** (early 2026):

| Provider | Cost / minute | Notes |
|---|---|---|
| `gemini` | ~$0.025 | cheapest; 15-min audio cap with 2-h resumption handle |
| `openai` | ~$0.30 | reasoning levels, 128K context, 60-min hard cap, no resumption |
| `grok` | ~$0.05 | flat $3/hour; spend cap under-counts (logs warning) |

**Cue regeneration**: pre-rendered cue WAVs (`cant_connect`,
`spend_cap_reached`, `cant_reach_cloud`) are baked from Gemini TTS
regardless of which voice provider is active for the live loop —
[`jasper/cues/generator.py`](jasper/cues/generator.py)'s
`GeminiTTSGenerator` is the only render backend wired up. If you run
with `JASPER_VOICE_PROVIDER=openai` and no `GEMINI_API_KEY`, cue
regen silently skips — the daemon plays whatever WAVs already exist
on disk. Bake them once with a Gemini key set, then you can run
provider=openai indefinitely.

**Adding a fourth provider**: see the "Adding a fourth provider"
checklist in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md).
The interface is `LiveConnection` + `LiveTurn` at
[`jasper/voice/session.py`](jasper/voice/session.py); shared
supervisor helpers (backoff, fingerprint, escalation cue) live at
[`jasper/voice/_supervisor.py`](jasper/voice/_supervisor.py).

---

## Gemini model switching — read first

**Preferred model: `gemini-3.1-flash-live-preview`** (latest Live
API model). Do NOT use the plain `gemini-2.5-flash` (it's not a
Live model — `Live API: Not supported` per
https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash).

**Acceptable fallback: `gemini-2.5-flash-native-audio-preview-12-2025`**
— Google's docs explicitly position 3.1 Flash Live as the
*successor* of 2.5 native-audio (see "Migrating from Gemini 2.5
Flash Live" section at
https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview).
Same Live API, same `client.aio.live.connect()` SDK path, same
prebuilt voice catalog, same `send_realtime_input(audio=Blob)`
shape. Use it when 3.1 Live Preview is silently failing for the
project (a real Google-side condition we've hit — server accepts
the WebSocket, accepts audio, sends nothing back; not surfaced
as an error in the SDK).

**Switch command** (laptop-side wrapper, SSHs to the Pi):

```sh
bash scripts/switch-gemini-model.sh        # show current model
bash scripts/switch-gemini-model.sh 3.1    # → gemini-3.1-flash-live-preview
bash scripts/switch-gemini-model.sh 2.5    # → gemini-2.5-flash-native-audio-preview-12-2025
```

The script flips `JASPER_GEMINI_MODEL` in `/etc/jasper/jasper.env`
and restarts `jasper-voice`. No code changes needed because the
daemon treats the model as opaque-string config.

**Symptoms that mean "Gemini Live is silently broken, switch to 2.5"**:

- Sessions repeatedly end with `0 input_tokens / 0 output_tokens`
  AND the daemon's `SILENT FAILURE: sent N bytes... received 0
  chunks back` warning is firing.
- Direct probe (text turn via `send_client_content`) returns no
  responses within 15s and no exception.
- Same-key non-Live `client.models.generate_content(...)` works
  (rules out auth/key issue).

When 3.1 Live unsticks, run `switch-gemini-model.sh 3.1` to flip
back.

---

## AEC bridge — opt-in toggle

Software AEC is **built but disabled by default**. README's
"Acoustic echo cancellation" section covers the engine (WebRTC
AEC3 via the `jasper_aec3` pybind11 binding, −15 to −18 dB on
music with the production REF_GAIN/MIC_GAIN tunings) and the
~110 MB RAM cost. The full investigation is in
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md).

**Prerequisite**: the XVF chip must be on the 6-channel firmware
variant (`v2.0.8 6chl`) — the bridge reads raw mic 0 from
channel 2 of the chip's USB capture, which only exists on the
6-ch firmware. If unsure, check with
`cat /proc/asound/Array/stream0 | grep Channels` (expect 6).
DFU flash procedure is in [`BRINGUP.md`](BRINGUP.md) Phase 2A.5.

To enable on the Pi (assumes 6-ch firmware already flashed):

```sh
sudo sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=hw:7,1|' \
    /etc/jasper/jasper.env
sudo systemctl enable --now jasper-aec-init jasper-aec-bridge
sudo systemctl restart jasper-voice
```

To disable:

```sh
sudo systemctl disable --now jasper-aec-bridge jasper-aec-init
sudo sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=Array|' \
    /etc/jasper/jasper.env
sudo systemctl restart jasper-voice
```

Verify with `sudo /opt/jasper/.venv/bin/jasper-doctor` either way.

These commands are duplicated in [`BRINGUP.md`](BRINGUP.md) Phase
2A.2 — keep both in sync.

The chip control library (`jasper.xvf.xvf_host`) is useful for
diagnostics regardless of bridge state. **Never call
`SAVE_CONFIGURATION`** — brick hazard on certain firmware
versions (respeaker repo issue #8).

---

## Satellite devices — opt-in hardware

The cross-cutting design home for ESP32 satellites (existing rotary
dial, AMOLED touchscreen mic satellite in progress, future devices)
lives in [`docs/satellites.md`](docs/satellites.md). It owns shared
protocols, multi-mic arbitration design, and per-device roadmap. Read
that first when working on satellite firmware or related Pi-side
daemons.

### Rotary dial

The CrowPanel 1.28" HMI ESP32-S3 rotary dial is a wireless physical
controller that talks to the Pi over WiFi. **Currently working
end-to-end on hardware:** volume control via encoder with an
on-screen volume gauge, transport toggle on short-press (play/pause),
hold-to-talk Gemini session on long-press. The other LVGL scenes
(clock face, listening orb, speaking waveform, now-playing card with
album art) have firmware scaffold but aren't yet validated on-device.

Pi side: `jasper-control` daemon binds `0.0.0.0:8780`, exposes
`POST /volume/adjust` (and `/volume/set`, `/healthz`). Volume
requests route through `VolumeCoordinator` (see
[`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md)), which dispatches
to the active source's own slider (AirPlay DBus, Spotify HTTP, BT
DBus) — not just CamillaDSP. Persistence is incidental — voice_daemon's debounced poller
catches external main_volume changes and writes them to the same
state file used by voice tools, so dial-driven volume survives
restarts without the control daemon knowing about the persistence
layer. Service file at `deploy/systemd/jasper-control.service`.
No auth — home LAN only.

Dial side: PlatformIO project at `firmware/dial/`. ESP32-S3, native
USB-CDC, Improv-over-Serial provisioning. WS2812 LED 0 = status
indicator (magenta=boot, yellow=connecting, dim green=online,
red blink=HTTP error, solid red=WiFi down).

To onboard a fresh dial, end-to-end:

```sh
# One-time, on any machine with PlatformIO (or via the Pi venv):
bash firmware/dial/build.sh
# Stages bin to /opt/jasper/firmware/dial/jasper-dial.bin

# Plug the dial into a Pi USB-C port, then on the Pi:
sudo /opt/jasper/.venv/bin/jasper-dial-onboard
# → flashes via esptool, reads Pi's current WiFi creds from
#   NetworkManager (or wpa_supplicant), pushes via Improv,
#   waits for dial to appear at jasper-dial.local. ~30 s.

# Unplug from Pi and connect to USB power. Dial reconnects to
# WiFi from NVS flash on every subsequent boot.
```

To re-provision after a WiFi password change: same command, same
USB plug. The dial accepts `SUBMIT_SETTINGS` over Improv whenever
it's connected to USB.

If the dial is already flashed and you just need to update creds,
pass `--no-flash`. If auto-detection of WiFi creds fails (locked-down
NM secret store, etc.), pass `--ssid` and `--password` explicitly.

The control daemon is always installed and enabled by `install.sh`,
even if there's no dial — it costs <10 MB RAM idle and the volume
endpoints are useful for any LAN client (Home Assistant, shortcuts,
etc.).

### AMOLED satellite (Phases 0, 1.1, 1.2 done; 1.3+ in progress)

Waveshare ESP32-S3-Touch-AMOLED-1.8 — touchscreen + mic satellite.
Project at `firmware/satellite-amoled/`. Both ESP32 firmware projects
(dial + satellite) on **Arduino-ESP32 v3.x via pioarduino** — see
`docs/satellites.md` "Toolchain — Arduino-ESP32 v3.x via pioarduino"
for the rationale and v2.x→v3.x deltas.

Shipped:
- Phase 0 (2026-05-08) — ES8311 mic capture, 16 kHz mono PCM over
  USB-CDC. Validated against music playback. See
  `docs/satellites.md` "Audio init footguns" for the non-obvious
  ES8311 init quirks (I²S stereo + demux for slot alignment;
  REG02 pre_multi=3 for SCLK-derived MCLK).
- Phase 1.1 (2026-05-08) — WiFi join from NVS-stored creds,
  Improv-over-Serial provisioning, mDNS-SD discovery of
  `_jasper-control._tcp`, dlog over USB-CDC + UDP `:5514`.
- Phase 1.2 (2026-05-09) — on-screen connection-status indicator
  on the 368×448 SH8601 AMOLED via Arduino_GFX. Direct draws (no
  LVGL yet); colored circle + label keyed off the `Status` enum;
  `setStatus()` helper redraws inline so PROVISION→ONLINE
  transitions show up immediately. See "Display init footguns"
  in `docs/satellites.md` for the SH8601 + TCA9554 reset
  sequence and Arduino_GFX subclass gotchas.

Next milestone: Phase 1.3+ — capacitive touch (FT3168), LVGL "Tap
to Talk" surface, control-plane HTTP, I²S mic capture gated on
press, UDP audio stream to a new Pi-side `MicSource` endpoint.

**Onboarding flow:** plug the satellite into a Pi USB-C port, then
`sudo /opt/jasper/.venv/bin/jasper-satellite-onboard`. Mirrors
`jasper-dial-onboard`: USB CDC discovery → optional flash from
`/opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin`
(populated by `bash firmware/satellite-amoled/build.sh`) → push
WiFi creds via Improv → wait for `jasper-satellite-amoled.local`.
The flash itself wipes NVS (factory.bin pads 0x0–0x10000 with
0xFF, including the 0x9000–0xe000 NVS region) but the cred-push
that follows refills it — no manual provisioning step.

**Local PIO setup** for the v3.x toolchain (laptop-side):
pioarduino requires Python ≥ 3.10 — the JTS project venv is
3.9 — so build inside a separate Python 3.11 venv with
`brew install python@3.11 && python3.11 -m venv /tmp/jts-pio-venv
&& /tmp/jts-pio-venv/bin/pip install platformio`. Prefix `pio`
invocations with `PATH="/opt/homebrew/bin:$PATH"` so PIO's
subprocess can find git for the Improv-WiFi library install.
The Pi already has Python 3.13 + PIO and builds cleanly without
the dance.

To capture audio for testing or SNR comparisons:

```sh
bash scripts/capture-satellite-amoled.sh 10        # 10 s → captures/<ts>.wav
bash scripts/capture-chip-mic.sh 10                # same shape, from XVF3800
```

Capture scripts assume the satellite is plugged into the Pi via
USB-C and the Pi is at `jts.local`. WAVs land in `captures/` (which
is gitignored — large binaries, regenerate as needed).

---

## Debugging — fetch evidence before guessing

When the user reports "it doesn't work" or asks about Pi-side
behaviour, **before guessing**, fetch the actual logs:

```sh
bash scripts/fetch-pi-logs.sh                # last hour, default Pi at jts.local
SINCE='10 minutes ago' bash scripts/fetch-pi-logs.sh
PI_HOST=192.168.1.42 bash scripts/fetch-pi-logs.sh
```

Output lands in `./logs/`. Read the `*-latest.*` symlinks:

- `logs/jasper-voice-latest.log` — voice daemon (wake events,
  tool calls, Gemini errors, idle timeouts, spend log)
- `logs/jasper-camilla-latest.log` — CamillaDSP (broken pipe,
  format mismatch, websocket connects)
- `logs/jasper-aec-bridge-latest.log` — software AEC bridge
  (only when enabled)
- `logs/combined-latest.log` — interleaved timeline
- `logs/alsa-devices-latest.txt` — `aplay -L` / `arecord -L`
  output. Always sanity-check actual ALSA card names against
  what the configs expect (`A` for Apple dongle, `Array` for
  ReSpeaker, `Loopback` for snd-aloop, `LoopbackAEC` for the
  AEC bridge's output card)
- `logs/camilladsp-latest.yml` — current CamillaDSP config on
  the Pi
- `logs/asoundrc-latest.txt` — current `/root/.asoundrc`
- `logs/jasper.env-latest.txt` — current env (secrets redacted)
- `logs/sessions-latest.txt` — last 20 voice sessions with token
  counts and estimated cost
- `logs/systemctl-latest.txt` — `systemctl status` for all units

Live tail (interactive, Ctrl-C to stop):

```sh
bash scripts/tail-pi-logs.sh                # all jasper-* units + renderers
bash scripts/tail-pi-logs.sh jasper-voice   # just one
```

For just the cross-daemon "events" — duck transitions, source
preempts, dial volume routing, wake/turn boundaries — the
`jasper-trace.sh` wrapper filters the live tail down to the
high-signal lines:

```sh
bash scripts/jasper-trace.sh                # default: last 5 min, follow
SINCE='1 hour ago' bash scripts/jasper-trace.sh
```

For a single JSON snapshot of cross-daemon state (voice provider /
session / spend, main_volume_db / listening_level, renderer states,
dial heartbeat), hit jasper-control's `/state` aggregator:

```sh
curl -s http://jts.local:8780/state | jq
```

Each `/state` section fails soft — if a daemon is unreachable, that
section is null instead of the whole call erroring out.

For a one-shot full diagnostic dump (when something's badly
wrong), run on the Pi:

```sh
ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh
# prints the path to a tarball under /tmp/, scp it back to ./logs/
```

### On the Pi itself

`jasper-doctor` codifies BRINGUP.md's smoke tests:

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor
```

Returns 0 if all critical checks pass. First thing to ask the
user to run when something's broken. The doctor reads
`/etc/jasper/jasper.env` and (if present)
`/var/lib/jasper/voice_provider.env` itself — no need to source
them into the calling shell.

---

## Behavioral rules for working in this codebase

Per the user's CLAUDE.md
(`github.com/jaspercurry/claude-rules`) and reinforced for this
specific project:

- **Diagnose before solving.** If something's broken, fetch the
  logs and point at the specific line that produced the failure
  before proposing a fix.
- **Check prior art.** Existing helpers — `pycamilladsp`,
  `openwakeword`, `google-genai`, `spotipy` — handle most of
  the integration. Don't reinvent.
- **Surgical changes — file ownership.** Our files live under
  `/opt/jasper/`, `/etc/camilladsp/`, `/etc/jasper/`,
  `/etc/modprobe.d/snd-aloop.conf`, `/root/.asoundrc`,
  `/etc/shairport-sync.conf`, `/etc/nginx/sites-enabled/jasper.conf`,
  and `/etc/systemd/system/{jasper-*,librespot,shairport-sync,nqptp,bt-agent}.service`.
  Touch only what you must when modifying these.
- **No silent failure paths.** Any new code path that would
  prevent the speaker from responding to a wake event MUST also
  trigger an audio cue (so the user hears why nothing happened).
  Add cues by appending a `CueDef` to
  [`jasper/cues/registry.py`](jasper/cues/registry.py) and calling
  `cues.play("<slug>")` from the failure handler — see
  [docs/HANDOFF-audible-feedback.md](docs/HANDOFF-audible-feedback.md)
  for the full pattern. Cue text must stay provider-agnostic
  (no "Google" / "Gemini" — voice backend is replaceable).

---

## Testing

Hardware-free tests (run locally, no SDK auth needed):

```sh
.venv/bin/pytest
```

Anything Pi-specific (audio I/O, websocket, Gemini Live) needs
to run on the actual hardware via `jasper-doctor` or by tailing
logs during use.

---

## Branch and remote

Active branch: `main`. The user's GitHub remote is
`jaspercurry/JTS` — accessible via `mcp__github__*` tools, not
the `gh` CLI.
