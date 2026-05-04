# JTS — Jasper smart speaker

Custom voice daemon on top of moOde 10.x + always-on CamillaDSP, running
on a Pi 5 (2GB). Voice via Gemini 3.1 Flash Live. See `BRINGUP.md` for the
full hardware bringup runbook and `PLAN.md` for the master plan.

## Model constraint — read this first

**This project uses `gemini-3.1-flash-live-preview`. Period.** Do NOT
suggest, fall back to, or test against Gemini 2.5 (`gemini-2.5-flash`,
`gemini-2.5-flash-native-audio-*`, etc.) — 2.5 is older and out of scope.
If 3.1 Live appears broken, debug 3.1 Live: read the official Live API
docs (https://ai.google.dev/gemini-api/docs/live), check status
(https://status.cloud.google.com), check whether the API key's project
needs billing or Vertex-AI access for 3.1 Live. Do not pivot models.

## What this repo is for

The Pi runs the daemon. This repo is developed on a laptop. The deploy
target is `/opt/jasper/` on the Pi via `deploy/install.sh`.

## Debugging Pi behaviour from this repo

When the user reports "it doesn't work" or asks about Pi-side behaviour,
**before guessing**, fetch the actual logs:

```sh
bash scripts/fetch-pi-logs.sh                # last hour, default Pi at jasper.local
SINCE='10 minutes ago' bash scripts/fetch-pi-logs.sh
PI_HOST=192.168.1.42 bash scripts/fetch-pi-logs.sh
```

Output lands in `./logs/`. Read the `*-latest.*` symlinks:

- `logs/jasper-voice-latest.log` — voice daemon (wake events, tool calls,
  Gemini errors, idle timeouts, spend log)
- `logs/jasper-camilla-latest.log` — CamillaDSP (broken pipe, format
  mismatch, websocket connects)
- `logs/mpd-latest.log` — MPD (output device errors, rate negotiations)
- `logs/combined-latest.log` — interleaved timeline of all four units
- `logs/alsa-devices-latest.txt` — `aplay -L` / `arecord -L` output —
  always sanity-check actual ALSA card names against what the configs
  expect (`A` for Apple dongle, `Array` for ReSpeaker, `Loopback` for
  snd-aloop)
- `logs/camilladsp-latest.yml` — current CamillaDSP config on the Pi
- `logs/asoundrc-latest.txt` — current `/root/.asoundrc`
- `logs/jasper.env-latest.txt` — current env (secrets redacted)
- `logs/sessions-latest.txt` — last 20 voice sessions with token counts
  and estimated cost
- `logs/systemctl-latest.txt` — `systemctl status` for all four units

Live tail (interactive, Ctrl-C to stop):

```sh
bash scripts/tail-pi-logs.sh                # all four units
bash scripts/tail-pi-logs.sh jasper-voice   # just one
```

For a one-shot full diagnostic dump (when something's badly wrong),
run on the Pi:

```sh
ssh pi@jasper.local sudo bash /home/pi/jts/scripts/pi-bundle.sh
# prints the path to a tarball under /tmp/, scp it back to ./logs/
```

## Running diagnostics on the Pi itself

`jasper-doctor` runs every smoke test from BRINGUP.md as code (ALSA card
names, CamillaDSP websocket, mic capture, TTS open, openWakeWord model
on disk, env vars, MPD, Spotify auth, RAM, daily spend cap). On the Pi:

```sh
sudo -E /opt/jasper/.venv/bin/jasper-doctor
```

Returns 0 if all critical checks pass. Useful first thing to ask the user
to run when something's broken.

## When debugging, prefer evidence over guesses

Per the user's CLAUDE.md (`github.com/jaspercurry/claude-rules`):

- **Diagnose before solving.** If something's broken, fetch the logs and
  point at the specific line that produced the failure before proposing
  a fix.
- **Check prior art.** Existing helpers — `pycamilladsp`, `python-mpd2`,
  `openwakeword`, `google-genai` — handle most of the integration. Don't
  reinvent.
- **Surgical changes.** moOde owns `/etc/asound.conf`, `/etc/mpd.conf`,
  and `/var/local/www/`. Our files live under `/opt/jasper/`,
  `/etc/camilladsp/`, `/etc/jasper/`, `/root/.asoundrc`, and
  `/etc/systemd/system/jasper-*.service` — touch nothing else.

## Testing

Hardware-free tests (run locally, no SDK auth needed):

```sh
.venv/bin/pytest
```

Anything Pi-specific (audio I/O, websocket, Gemini Live) needs to run on
the actual hardware via `jasper-doctor` or by tailing logs during use.

## Branch and remote

Active branch: `claude/camilla-dsp-voice-plan-QRdsE`. The user's GitHub
remote is `jaspercurry/JTS` — accessible via `mcp__github__*` tools, not
the `gh` CLI.

## Out of scope for v1

Listed in `PLAN.md` "What comes after v1" — defer all of:
room correction web tool, captive portal (Balena WiFi Connect), Snapcast
stereo pair, wireless subwoofer, mesh AP+STA, USB gadget mode, Home
Assistant bridge, custom "Hey Jasper" wake-word training. Don't build
these until v1 actually plays music with voice control end-to-end.
