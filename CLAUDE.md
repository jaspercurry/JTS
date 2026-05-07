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

## Renderer backend — moode vs debian

JTS supports two deployment targets, picked at install time:

```sh
sudo bash deploy/install.sh --backend=moode    # default; existing
sudo bash deploy/install.sh --backend=debian   # no moOde
```

**moode** (default, what jasper.local runs): assumes moOde audio
10.1.2+ is already up. Hijacks moOde's `pcm._audioout` ALSA symbol
to redirect renderers into snd-aloop. `jasper/moode.py` polls
moOde's REST + SQLite for renderer state.

**debian** (validated on jts.local 2026-05-07): stock Raspberry Pi OS
Lite, no moOde. Source-builds shairport-sync with AirPlay 2 +
nqptp, drops in librespot (rust, via raspotify .deb) + bluez-alsa
+ bt-agent, owns the full systemd unit per renderer.
`jasper/renderer.py:DebianBackend` reads librespot state from
`/run/librespot/state.json` (written by the `--onevent` hook
`/usr/local/bin/jasper-librespot-event`), shairport-sync MPRIS,
and bluez-alsa directly. `jasper-mux.service` does latest-source-
wins preemption (moOde's worker.php replacement).

Spotify volume control goes via the Spotify Web API (the multi-
account `spotify_router`) since librespot has no local control HTTP
— see [`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md).

Backend selection is via `JASPER_RENDERER_BACKEND=moode|debian`
in `/etc/jasper/jasper.env` (default "moode" for backward compat).
`jasper.renderer.make_backend()` is the single entry point;
voice_daemon and jasper-control both go through it.

**Things that differ between backends:**

| Aspect | moode | debian |
|---|---|---|
| `/root/.asoundrc` | jasper_capture (dsnoop) + jasper_out (dmix) | jasper_out only (no AEC bridge yet) |
| `/etc/alsa/conf.d/zz-jts-loopback.conf` | hijacks `_audioout` | not installed |
| `/etc/modprobe.d/snd-aloop.conf` | `index=0,5` | `index=6,7` (HDMI claims 0,1 on fresh Pi OS Lite) |
| Renderer daemons | provided by moOde | source-built or apt-installed by `install.sh` |
| Renderer state polling | moOde REST + SQLite | each daemon's own surface |
| Source preemption | moOde's worker.php | `jasper-mux` daemon |

**For voice testing on the debian backend** (jts.local), the XVF3800
mic array still needs to be physically present on that Pi. As of
the migration date, the mic was on jasper.local and not yet moved.
Voice end-to-end on the debian stack is the next milestone.

The full debian-stack file map and source-build deps are in
[`deploy/debian-stack/README.md`](deploy/debian-stack/README.md).

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
"Acoustic echo cancellation" section explains the trade-off
(modest attenuation, ~110 MB RAM cost on 1GB Pi 5). The full
investigation is in [`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md).

**Prerequisite**: the XVF chip must be on the 6-channel firmware
variant (`v2.0.8 6chl`) — the bridge reads raw mic 0 from
channel 2 of the chip's USB capture, which only exists on the
6-ch firmware. If unsure, check with
`cat /proc/asound/Array/stream0 | grep Channels` (expect 6).
DFU flash procedure is in [`BRINGUP.md`](BRINGUP.md) Phase 2A.5.

To enable on the Pi (assumes 6-ch firmware already flashed):

```sh
sudo sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=hw:5,1|' \
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

## Rotary dial controller — opt-in hardware

The CrowPanel 1.28" HMI ESP32-S3 rotary dial is a wireless physical
controller that talks to the Pi over WiFi. Phase 1 (volume only) is
the only piece landed; play/pause and hold-to-talk follow.

Pi side: `jasper-control` daemon binds `0.0.0.0:8780`, exposes
`POST /volume/adjust` (and `/volume/set`, `/healthz`). Volume
requests route through `VolumeCoordinator` (see
[`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md)), which dispatches
to the active source's own slider (AirPlay DBus, Spotify HTTP, BT
DBus) — not just CamillaDSP. Persistence is incidental — voice_daemon's debounced poller
catches external main_volume changes and writes them to the same
state file used by voice tools and moOde's slider, so dial-driven
volume survives restarts without the control daemon knowing about
the persistence layer. Service file at
`deploy/systemd/jasper-control.service`. No auth — home LAN only.

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

---

## Debugging — fetch evidence before guessing

When the user reports "it doesn't work" or asks about Pi-side
behaviour, **before guessing**, fetch the actual logs:

```sh
bash scripts/fetch-pi-logs.sh                # last hour, default Pi at jasper.local
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
- `logs/mpd-latest.log` — MPD (output device errors, rate
  negotiations)
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
bash scripts/tail-pi-logs.sh                # all units
bash scripts/tail-pi-logs.sh jasper-voice   # just one
```

For a one-shot full diagnostic dump (when something's badly
wrong), run on the Pi:

```sh
ssh pi@jasper.local sudo bash /home/pi/jts/scripts/pi-bundle.sh
# prints the path to a tarball under /tmp/, scp it back to ./logs/
```

### On the Pi itself

`jasper-doctor` codifies BRINGUP.md's smoke tests:

```sh
sudo -E /opt/jasper/.venv/bin/jasper-doctor
```

Returns 0 if all critical checks pass. First thing to ask the
user to run when something's broken.

---

## Behavioral rules for working in this codebase

Per the user's CLAUDE.md
(`github.com/jaspercurry/claude-rules`) and reinforced for this
specific project:

- **Diagnose before solving.** If something's broken, fetch the
  logs and point at the specific line that produced the failure
  before proposing a fix.
- **Check prior art.** Existing helpers — `pycamilladsp`,
  `python-mpd2`, `openwakeword`, `google-genai` — handle most of
  the integration. Don't reinvent.
- **Surgical changes — file ownership.** moOde owns
  `/etc/asound.conf`, `/etc/mpd.conf`, `/var/local/www/`. Our
  files live under `/opt/jasper/`, `/etc/camilladsp/`,
  `/etc/jasper/`, `/etc/modprobe.d/snd-aloop.conf`,
  `/etc/alsa/conf.d/zz-jts-loopback.conf`, `/root/.asoundrc`,
  and `/etc/systemd/system/jasper-*.service`. **Do not modify
  anything moOde owns.**
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
