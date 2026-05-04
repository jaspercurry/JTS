# Jasper v1 bringup runbook

End-to-end steps from "hardware on desk" to "Hey Jarvis, set volume to 30."
Estimate ~3–4 hours including moOde flash, network setup, and verification.

If anything in here is wrong on first contact with hardware, that's a bug in
this runbook — fix it and update.

---

## What you need on hand

- Raspberry Pi 5 (2GB)
- Official Pi 5 27W USB-C PSU
- Pi 5 active cooler installed
- 32 GB+ A2 microSD card + reader
- Apple USB-C → 3.5mm dongle
- ReSpeaker XVF3800 (USB)
- TPA3255 amp + 32V supply
- Speakers + speaker wire
- 3.5mm → RCA cable (or 3.5mm → bare wire) for amp input
- Ethernet cable (used for first-boot setup)
- Laptop on the same LAN

---

## Phase 0 — Flash moOde (15 min)

1. Download moOde 10.1.2 image: <https://moodeaudio.org/>
2. Use **Raspberry Pi Imager** → "Use custom image" → pick the moOde `.img.xz`.
3. Before writing, click the gear icon and set:
   - Hostname: `jasper`
   - Enable SSH (use password auth) — set username `pi`, password your choice
   - Skip Wi-Fi config (we'll use Ethernet first; add Wi-Fi from moOde later)
4. Write to the SD card.
5. Insert SD into Pi 5. Plug in Ethernet. Power on.

Wait ~60 sec for first boot. The Pi will show up as `jasper.local` on your
LAN.

---

## Phase 1A — moOde basic playback (30 min)

### A1. Initial moOde config

1. Browser → `http://jasper.local`
2. Accept the moOde Terms of Service.
3. **Do NOT plug in the dongle yet.** First confirm the web UI loads.

### A2. Plug in the Apple USB-C dongle

Connect dongle to a Pi 5 USB-A port (use a USB-C → USB-A adapter).
Connect the dongle's 3.5mm out to the amp input. Power the amp.

### A3. Tell moOde to use the dongle

1. moOde web UI → menu (top-left) → **Configure → Audio → MPD options**.
2. **Audio output**: select the entry whose name contains "Headset" (this is
   the Apple dongle — Linux exposes it as card name `Headset`).
3. **Resampling**: leave Off for now.
4. **DSP / CamillaDSP**: leave Off for now (default `alsa_cdsp` mode is
   fine; we'll switch to Custom in Phase 1B).
5. Click **Set**, then **Restart MPD** when prompted.

### A4. Sanity-check streaming

From your phone:

- **AirPlay 2**: open Music or Spotify → tap the AirPlay icon → pick
  `jasper`. Play a track. You should hear it.
- **Spotify Connect**: in Spotify → Devices → pick `Moode jasper`. Play.
- **Bluetooth**: moOde web UI → Configure → Bluetooth → Enable. Pair from
  phone. Play.

If any of these fail, **stop here and fix Phase 1A before moving on**. The
voice daemon depends on this working.

---

## Phase 1B — Always-on CamillaDSP via `_audioout` override (45 min)

### Why this is more involved than picking "Loopback" in moOde

Two moOde 10.x realities make the naive approach ("just pick Loopback as
Output device in moOde's UI, set CamillaDSP to Custom") not work — these
were both painful surprises and the runbook walks around them now:

1. **moOde's "Custom" CamillaDSP mode is NOT externally-managed.** It
   means "you supply the YAML, but moOde still owns spawning + routing."
   In any non-`off` mode (Default OR Custom), moOde rewrites the
   `pcm._audioout` ALSA symbol to point at its own `pcm.camilladsp`
   ioplug — which spawns a fresh CamillaDSP child process per stream
   and kills it when the stream closes. (See `www/inc/audio.php:213-214`
   and `www/snd-config.php:487` in github.com/moode-player/moode.) That
   would fight our long-lived `jasper-camilla` over the DAC. Set
   moOde CamillaDSP to **Off**.

2. **moOde's UI refuses to select Loopback as Output device.** Picking it
   raises "Device is reserved and cannot be selected for output" — moOde
   reserves Loopback for its own ALSA Loopback *toggle* feature
   (a sniff target). We sidestep at the ALSA conf.d layer instead: drop
   `/etc/alsa/conf.d/zz-jts-loopback.conf` redefining `pcm._audioout`
   to route into snd-aloop. moOde's `_audioout.conf` keeps pointing at
   the physical DAC for moOde's own bookkeeping; our `zz-` file loads
   later in alphabetical order and the `pcm.!_audioout` force-redefine
   wins. (Filename matters: digit prefixes load BEFORE underscore — use
   `zz-` to load AFTER `_audioout.conf` in ASCII collation.)

The end-to-end pipeline:

```
phone → AirPlay/Spotify/MPD → pcm._audioout (our override)
     → hw:Loopback,0,0 (snd-aloop kernel)
     → hw:Loopback,1,0 capture (jasper-camilla reads here)
     → master_gain mixer + flat filter (passthrough at 0 dB)
     → jasper_dongle dmix → Apple USB-C dongle → amp → speakers
```

CamillaDSP is the single owner of the dongle. The voice daemon ducks
audio by calling `SetVolume` on the `master_gain` mixer over CamillaDSP's
websocket on port 1234.

### B1. moOde UI — CamillaDSP Off + Volume type Software

1. moOde web UI → Configure → Audio → CamillaDSP section.
2. **Mode: Off.** (Custom mode would re-route `_audioout` through moOde's
   `pcm.camilladsp` ioplug — see "Why" above.)
3. SET, restart MPD when prompted.
4. Back on Configure → Audio:
   - **Volume type: Software** (or PCM/Hardware — anything but
     CamillaDSP, which becomes unavailable when CamillaDSP=Off;
     `jasper-camilla` owns ducking via the master_gain mixer).
   - **Output device: leave as the USB-C dongle** (`2: USB-C to 3.5mm H...`).
     Irrelevant — our ALSA override hijacks `_audioout` regardless of
     what moOde sets here. moOde just wants *some* physical device picked.
   - **Loopback toggle (in ALSA Options): OFF.** That toggle creates a
     `multi` slave that mirrors output to a sniff target — we don't want
     it; it would define its own `pcm.!_audioout` and conflict with ours.
5. SET, restart MPD.

### B2. moOde UI — MPD SoX Resampling at 48 kHz

Critical: snd-aloop locks rate at first opener. AirPlay sources are
44.1 kHz natively; without resampling, the Loopback rate can flip on
track changes and CamillaDSP throws "broken pipe" (CamillaDSP issues
#311 / #315).

1. moOde web UI → Configure → Audio → MPD options.
2. **SoX Resampling: Enabled.**
3. **Sample rate: 48000 Hz**, **Bit depth: 16**, **Channels: 2**.
4. SAVE, restart MPD.

### B3. Get the repo onto the Pi

The simplest path is to rsync from your laptop checkout — works regardless
of whether the GitHub repo is private. Set up passwordless SSH first so
the rest of bringup doesn't prompt every command:

```sh
# from your laptop, one-time:
ssh-copy-id pi@jasper.local
ssh pi@jasper.local sudo apt-get install -y rsync
```

Then push the working tree:

```sh
# from the JTS repo root on your laptop:
rsync -avz --delete \
    --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' \
    ./ pi@jasper.local:/home/pi/jts/
```

Alternatively, if the repo is public on GitHub (or you've configured a
deploy key on the Pi), clone directly:

```sh
ssh pi@jasper.local
sudo apt-get install -y git
git clone https://github.com/jaspercurry/JTS.git /home/pi/jts
cd /home/pi/jts
git checkout claude/camilla-dsp-voice-plan-QRdsE
```

### B4. Run the install script

```sh
sudo bash deploy/install.sh
```

This script will:
- Install Python 3, venv, build deps, libasound2, portaudio
- Download CamillaDSP `v4.1.3` aarch64 to `/opt/camilladsp/`
  (sha256-verified)
- Drop `/etc/camilladsp/v1.yml` (passthrough + master_gain mixer,
  CamillaDSP 4.x schema with `S16_LE`/`S32_LE` formats and
  `channels: [N]` pipeline filters)
- Drop `/etc/modules-load.d/snd-aloop.conf` and `modprobe` it now
- Drop **`/etc/alsa/conf.d/zz-jts-loopback.conf`** — the
  `pcm._audioout` override that hijacks moOde's renderers into
  snd-aloop Loopback (see "Why this is more involved" above)
- Drop `/root/.asoundrc` (defines `pcm.jasper_dongle` dmix device for
  CamillaDSP playback to the dongle)
- Drop **`/etc/systemd/system/shairport-sync.service.d/jts-output.conf`**
  drop-in forcing systemd-launched shairport-sync to write to `_audioout`
  (without this, shairport-sync writes to ALSA `default` and bypasses
  our hijack)
- Create `/opt/jasper/` Python venv and install the daemon
  (`openwakeword` is installed via `--no-deps` because its declared
  `tflite-runtime` dep has no Python 3.13 wheel; we use ONNX models
  exclusively)
- Create `/etc/jasper/jasper.env` from the template
- Drop `/etc/systemd/system/jasper-camilla.service` and
  `jasper-voice.service` and enable them (does NOT auto-start — see B7)
- Restart `shairport-sync.service` so it picks up the drop-in

### B5. Verify ALSA device names match what we assumed

This is the **single most likely thing to be wrong** — confirm before
restarting any services.

```sh
aplay -L | grep -B1 -i 'usb-c to 3.5mm'    # the Apple dongle
arecord -L | grep -B1 -i 'xvf3800\|array'  # the ReSpeaker
aplay -L | grep -i loopback                 # the snd-aloop module
```

The defaults assume (verified against community forum posts, May 2026):

| Component | Card name | Where referenced |
|---|---|---|
| Apple USB-C → 3.5mm dongle | **`A`** (literally the letter A; the device description is "USB-C to 3.5mm Headphone Jack A") | `/root/.asoundrc` slave.pcm `hw:CARD=A,DEV=0` and `ctl.jasper_dongle.card A` |
| ReSpeaker XVF3800 | **`Array`** (literal ALSA card; PortAudio surfaces as "Array: USB Audio (hw:N,0)") | `/etc/jasper/jasper.env` `JASPER_MIC_DEVICE=Array` (PortAudio substring — NOT `plughw:` — see jasper/config.py) |
| MiniDSP UMIK-2 (alt) | **`UMIK2`** ALSA / PortAudio name "UMIK-2: USB Audio (hw:N,0)" | `JASPER_MIC_DEVICE=UMIK-2`, **`JASPER_MIC_CAPTURE_RATE=48000`**, **`JASPER_MIC_CAPTURE_CHANNELS=2`** (no native 16 kHz support — MicCapture downsamples) |
| snd-aloop | **`Loopback`** (kernel-fixed) | `/etc/camilladsp/v1.yml` capture device |

If your `aplay -L` / `arecord -L` shows different names, edit the relevant
file and `systemctl restart jasper-camilla` (and/or `jasper-voice`).

### B6. Verify the `_audioout` override is hijacking to Loopback

```sh
sudo aplay -v -D _audioout /usr/share/sounds/alsa/Front_Center.wav 2>&1 | head
```

Expected output contains:

```
Plug PCM: Hardware PCM card N 'Loopback' device 0 subdevice 0
```

(where `N` is whatever ALSA assigns Loopback — usually 3.) If it shows
the dongle (`'USB-C to 3.5mm Headphone Jack A'`) instead, our override
isn't loading. Sanity-check:

1. `/etc/alsa/conf.d/zz-jts-loopback.conf` exists and has
   `pcm.!_audioout { type plug; slave.pcm "hw:Loopback,0,0" }`
2. `LC_ALL=C ls /etc/alsa/conf.d/` shows `zz-jts-loopback.conf`
   alphabetically AFTER `_audioout.conf` (digit prefixes load BEFORE
   underscore in ASCII collation — that's why `99-` doesn't work)
3. moOde Loopback toggle (Configure → Audio → ALSA Options → Loopback)
   is OFF (toggle ON would create its own `pcm.!_audioout` and conflict)

### B6.1. shairport-sync rate caveat

If AirPlay is your primary streaming source AND you hit "broken pipe"
errors in `journalctl -u jasper-camilla` after Phase 2 starts, force
shairport-sync to 48 kHz:

```sh
sudo nano /etc/shairport-sync.conf
# In the alsa block (uncomment and set):
#     output_format = "S16_LE";
#     output_rate = 48000;
sudo systemctl restart shairport-sync
```

Doesn't always trigger — moOde's `type plug` wrapping of `_audioout`
often masks rate transitions. Document only if hit.

### B7. Start jasper-camilla and verify the chain

```sh
sudo systemctl start jasper-camilla
sudo systemctl status jasper-camilla     # should be "active (running)"
journalctl -u jasper-camilla -n 30 --no-pager
```

Look for these log lines:
- `CamillaDSP version 4.1.3 ...` (startup)
- `Capture device supports rate adjust`
- `PB: Starting playback from Prepared state`

Now from your phone, AirPlay a track to the device named `Jasper`. You
should hear it through the dongle. The signal path is:

```
phone → AirPlay → shairport-sync (-d _audioout)
     → pcm._audioout (zz-jts-loopback.conf override)
     → hw:Loopback,0,0 (snd-aloop kernel)
     → hw:Loopback,1,0 capture
     → jasper-camilla (master_gain + flat passthrough)
     → jasper_dongle dmix → Apple USB-C dongle → amp → speakers
```

Sanity-check the chain is fully wired (each should show `state: RUNNING`):

```sh
cat /proc/asound/Loopback/pcm0p/sub0/status   # shairport writing
cat /proc/asound/Loopback/pcm1c/sub0/status   # jasper-camilla reading
cat /proc/asound/A/pcm0p/sub0/status          # jasper-camilla → dongle
```

### B8. Verify SetVolume works

```sh
/opt/jasper/.venv/bin/python -c "
from camilladsp import CamillaClient
c = CamillaClient('localhost', 1234); c.connect()
print('current:', c.volume.main_volume())
c.volume.set_main_volume(-20); print('ducked')
import time; time.sleep(2)
c.volume.set_main_volume(0); print('restored')
"
```

You should hear the music drop ~20 dB for 2 sec, then come back. **If this
works, Phase 1B is done.**

---

## Phase 2A — Mic + wake word (30 min)

### A1. Plug in the mic

**XVF3800 (intended):** USB-C port on the device; needs USB-C → USB-A
cable (Pi 5's USB-A is for peripherals; Pi 5 USB-C is power-only). Plug
into a USB-A port.

**UMIK-2 (alt; bring-up only — no AEC):** plug the USB-C cable into any
Pi 5 USB-A port. Then in `/etc/jasper/jasper.env`:
```
JASPER_MIC_DEVICE=UMIK-2
JASPER_MIC_CAPTURE_RATE=48000
JASPER_MIC_CAPTURE_CHANNELS=2
```
MicCapture polyphase-downsamples 48 kHz stereo → 16 kHz mono internally.

Confirm capture works (substitute `UMIK2` or `Array` for the card):

```sh
arecord -L | grep -B1 -iE 'umik|xvf3800|array'
arecord -d 5 -f S16_LE -r 16000 -c 1 -D plughw:CARD=Array /tmp/test.wav
sudo aplay -D plug:jasper_dongle /tmp/test.wav   # play via CamillaDSP-shared dongle
```

(Note: `arecord` accepts ALSA `plughw:` strings and resamples — the
daemon goes through sounddevice/PortAudio which doesn't, hence the
CAPTURE_RATE plumbing above.)

### A2. Hardware AEC sanity check (XVF3800 only)

While AirPlaying loud music, repeat the recording. The XVF3800 should
attenuate the music heavily (you should hear yourself clearly above the
nearly-silent music). If the music dominates the recording, AEC isn't
working — check the XVF3800 firmware version, USB cable, USB port.

The UMIK-2 has no AEC — wake-word reliability degrades during loud
music; this is expected, not a bug.

### A3. Wake-word smoke test

PortAudio doesn't accept ALSA `plughw:` strings, and many mics
(including UMIK-2) don't natively support 16 kHz capture. The simplest
smoke test uses arecord (which DOES handle plughw + resampling) to
record, then loads the WAV into openwakeword:

```sh
ssh pi@jasper.local
arecord -d 10 -f S16_LE -r 16000 -c 1 -D plughw:CARD=UMIK2 /tmp/wake.wav
# (substitute Array for XVF3800)
# Say "Hey Jarvis" 2-3 times during the 10 sec recording.
/opt/jasper/.venv/bin/python <<'PY'
import wave, numpy as np
from openwakeword.model import Model
with wave.open('/tmp/wake.wav','rb') as w:
    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
m = Model(wakeword_models=['hey_jarvis'], inference_framework='onnx')
scores = [max(m.predict(audio[i:i+1280]).values()) for i in range(0, len(audio)-1280, 1280)]
print(f'peak: {max(scores):.3f}  ({"DETECTED" if max(scores) >= 0.5 else "NO"})')
PY
```

If peak score is < 0.5 with music quiet, raise the mic gain or move the
XVF3800 closer. If 0.5+ is easy to hit but you also get false positives,
raise `JASPER_WAKE_THRESHOLD` to 0.6 or 0.7 in `/etc/jasper/jasper.env`.

---

## Phase 2B — Voice daemon + Gemini Live (45 min)

### B1. Get a Gemini API key (free tier)

1. Open <https://aistudio.google.com/app/apikey> on a laptop.
2. Sign in with a Google account.
3. Accept Terms of Service → Continue.
4. Click **Create API key** → "Create API key in new project".
5. Copy the key (starts with `AIza...`).

The Gemini 3.1 Flash Live preview model is **free of charge** on the AI
Studio free tier — no billing setup required for personal use. Free-tier
rate limits are project-wide (not per-key), but easily cover a smart
speaker. If you ever exceed free, attach billing in AI Studio → Set up
billing.

### B2. Drop the key into the Pi

```sh
sudo nano /etc/jasper/jasper.env
# Set GEMINI_API_KEY=AIza... (the value you copied)
# Verify the other defaults look right
sudo chmod 0600 /etc/jasper/jasper.env
```

### B3. (Optional) Spotify Web API for voice control

Two different Spotify integrations exist in this build — don't conflate them:

| | Spotify Connect (built into moOde) | Spotify Web API (this section) |
|---|---|---|
| Purpose | Makes the speaker show up as a target in Spotify's app | Lets the voice daemon search for and start tracks via tool calls |
| Setup | None — moOde 10.x bundles librespot 0.8.0; the Pi auto-advertises via zeroconf as "Moode jasper" | Requires a Spotify Developer app + OAuth |
| Account | Premium required for use, but no developer registration | Premium required + your own Developer app |
| What you can do | Play any track from Spotify's app to the speaker | "Hey Jarvis, play Bohemian Rhapsody" |

Skip this section entirely if you don't want voice-driven Spotify search;
moOde's Spotify Connect already works without any setup — open Spotify on
your phone, tap the device icon, pick the moOde unit. Done.

To enable voice-driven Spotify control:

1. Create a Spotify Developer app at
   <https://developer.spotify.com/dashboard>.
2. **Redirect URI**: `http://127.0.0.1:8765/callback` exactly. Spotify
   rejects `localhost` since April 2025 — must be the literal `127.0.0.1`.
3. Copy Client ID + Client Secret into `/etc/jasper/jasper.env`.
4. On the Pi, run:

   ```sh
   sudo -E /opt/jasper/.venv/bin/jasper-spotify-auth
   ```

   It prints an authorize URL. Open it on your phone or laptop. Grant
   access. Your phone redirects to `http://127.0.0.1:8765/callback?code=...`
   which fails to load — that's fine. **Copy the FULL URL from the address
   bar** (must include `?code=...`) and paste it back into the SSH
   terminal. The refresh token is cached at `/var/lib/jasper/.spotify-cache`.

5. The voice daemon's `spotify_play()` tool needs an active Spotify Connect
   target. Open Spotify on your phone once and pick the moOde unit so
   librespot starts; from then on `start_playback` from the daemon will
   find it.

### B4. Start the voice daemon

```sh
sudo systemctl start jasper-voice
sudo systemctl status jasper-voice
journalctl -u jasper-voice -f
```

Watch the logs. The first thing you'll see is "jasper-voice ready". Now
say:

> Hey Jarvis. What time is it?

You should hear the music duck to ~-15 dB, then Gemini responds through
the speakers, then music restores. The log shows token usage and estimated
cost per session.

### B5. Tool-call smoke tests

With music playing:

- "Hey Jarvis, set volume to -20." → music level drops permanently
- "Hey Jarvis, set volume to 0." → restored
- "Hey Jarvis, pause." → music pauses
- "Hey Jarvis, resume." → music resumes
- "Hey Jarvis, skip this song." → next track
- (Spotify only) "Hey Jarvis, play Bohemian Rhapsody by Queen." → Spotify
  starts playback

### B6. Crash-resilience check

```sh
sudo systemctl kill jasper-voice
# wait 5 sec
sudo systemctl status jasper-voice
# should be back to "active (running)" — restarted by systemd
```

### B7. Spend cap check

```sh
sudo nano /etc/jasper/jasper.env
# Set JASPER_DAILY_SPEND_CAP_USD=0.0001 (basically zero)
sudo systemctl restart jasper-voice
# Try saying the wake word — log will show "daily spend cap reached"
# Restore JASPER_DAILY_SPEND_CAP_USD=1.00 when done
```

---

## Reference: file layout on the Pi after install

```
/opt/camilladsp/camilladsp                  # 4.1.3 binary
/etc/camilladsp/v1.yml                      # passthrough + master_gain
                                            #   (CamillaDSP 4.x schema)
/etc/modules-load.d/snd-aloop.conf          # snd-aloop loaded at boot
/etc/alsa/conf.d/zz-jts-loopback.conf       # pcm._audioout override
                                            #   redirecting to Loopback
/root/.asoundrc                             # pcm.jasper_dongle (dmix)
/opt/jasper/                                # Python pkg (managed by install.sh)
  .venv/                                    # virtualenv
  jasper/                                   # source
/etc/jasper/jasper.env                      # API keys + tunables (chmod 600)
/var/lib/jasper/                            # state dir
  usage.db                                  # SQLite spend log
  .spotify-cache                            # Spotify refresh token
/etc/systemd/system/jasper-camilla.service
/etc/systemd/system/jasper-voice.service
/etc/systemd/system/shairport-sync.service.d/jts-output.conf
                                            # drop-in: shairport-sync
                                            #   writes to _audioout
```

---

## Diagnostics

When something's wrong:

```sh
# On the Pi — runs every smoke test from this runbook as code:
sudo -E /opt/jasper/.venv/bin/jasper-doctor
```

When asking Claude Code on your laptop for help:

```sh
# Pulls journals + configs + ALSA state into ./logs/ for inspection:
bash scripts/fetch-pi-logs.sh
# Live tail:
bash scripts/tail-pi-logs.sh
```

Defaults assume `pi@jasper.local`; override via `PI_HOST` / `PI_USER` /
`SINCE` env vars. The fetch script redacts `GEMINI_API_KEY` and
`SPOTIFY_CLIENT_SECRET` server-side before writing.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `journalctl -u jasper-camilla` shows "Cannot open device" | ALSA device names in `/etc/camilladsp/v1.yml` don't match reality | Run `aplay -L` / `arecord -L`, edit `/etc/camilladsp/v1.yml` and `/root/.asoundrc`, `systemctl restart jasper-camilla` |
| Music plays via dongle bypassing CamillaDSP | `pcm._audioout` override not loading, OR moOde CamillaDSP set to a non-`off` mode rerouting `_audioout` to its own ioplug | `sudo aplay -v -D _audioout /usr/share/sounds/alsa/Front_Center.wav` should show `'Loopback'` as the slave; if it shows the dongle, check `/etc/alsa/conf.d/zz-jts-loopback.conf` exists and that moOde CamillaDSP=Off |
| AirPlay music plays through Pi but no sound from speakers | Systemd shairport-sync writing to ALSA `default` instead of `_audioout` | Verify `pgrep -a shairport-sync` shows `-- -d _audioout` on the cmdline; if not, check the systemd drop-in `/etc/systemd/system/shairport-sync.service.d/jts-output.conf` exists and `systemctl daemon-reload && systemctl restart shairport-sync` |
| `journalctl -u jasper-camilla` shows "unknown variant `S16LE`" or "unknown field `channel`" | CamillaDSP 4.x schema mismatch; old YAML has 3.x-style format names or pipeline filter keys | Edit `/etc/camilladsp/v1.yml`: format `S16LE`/`S32LE` → `S16_LE`/`S32_LE`; pipeline filter `channel: N` → `channels: [N]` |
| TTS plays but music doesn't | dmix conflict — both processes opened the dongle at incompatible rates | Confirm `slave.rate 48000` in `/root/.asoundrc`; `aplay -v` shows actual rate |
| Wake word never fires | Mic device wrong, or model file missing, or threshold too high | `arecord -d 5 ... | aplay` to confirm mic; lower `JASPER_WAKE_THRESHOLD` to 0.4 temporarily |
| Wake word fires constantly during music | XVF3800 AEC not working; or threshold too low | Verify XVF3800 firmware; raise threshold to 0.65 |
| "GEMINI_API_KEY not found" on daemon start | Env file not loaded by systemd | Confirm `EnvironmentFile=/etc/jasper/jasper.env` in unit; `systemctl daemon-reload` |
| Spotify tool returns "no active spotify device" | moOde's Spotify Connect endpoint isn't running, or another phone took over | moOde web UI → Configure → Audio → Spotify Connect → Enable. Disconnect any other devices controlling Spotify. |
| Voice ducking doesn't restore | CamillaDSP websocket disconnected mid-session | Check `journalctl -u jasper-voice` for "camilla call failed"; restart `jasper-camilla` |
| Daemon OOM on 1GB Pi 5 | Stack peaks above 750 MB at runtime | Switch to 2GB Pi 5 (the recommended SKU for v1) |

---

## What I am still uncertain about

Live items still to validate on real hardware:

1. **`google-genai` SDK Preview churn.** Live API is still Preview as of
   May 2026. If Google ships a breaking change to `LiveConnectConfig` or
   `send_realtime_input` between when this was written and when you build,
   the pinned `google-genai==1.13.0` should keep things working; later
   upgrade requires reading the changelog. The `VoiceSession` interface
   contains the blast radius to one adapter file.
2. **dmix interaction with CamillaDSP's chunksize.** `chunksize: 1024` in
   `v1.yml` is matched to dmix `period_size 1024`. If you see clicks or
   xruns in `journalctl -u jasper-camilla`, try `chunksize: 2048` and
   `period_size 2048` (change in BOTH places).
3. **moOde's bundled CamillaDSP 3.0.1 vs our 4.1.3.** install.sh stops and
   disables `camilladsp.service` to avoid contention, but if a future
   moOde release renames that unit, our disable becomes a no-op. Watch
   for two camilladsp processes in `ps auxf | grep camilladsp` after a
   moOde upgrade. Bigger risk: a future moOde may regenerate
   `_audioout.conf` in a way that conflicts with our `zz-jts-loopback.conf`
   override (e.g. moves to `99-` numeric prefix to load even later than
   conf.d alphabetics). Re-verify B6 after any moOde upgrade.
4. **shairport-sync `mpd2cdspvolume` volume bridging.** moOde's stock
   setup translates AirPlay/Spotify volume slider events to CamillaDSP
   `SetVolume` websocket calls via `mpd2cdspvolume`. With our setup
   (CamillaDSP=Off in moOde), that bridge isn't running. AirPlay/Spotify
   volume from your phone will adjust the source's own software volume
   only — it won't move CamillaDSP's master_gain. Volume from the moOde
   web UI uses moOde's "Software" volume type which works in MPD
   directly but not for shairport/librespot. If volume control from
   non-MPD sources matters, plumb a bridge later.

### Resolved since first draft

- ALSA card names confirmed: dongle = `A`, ReSpeaker = `Array`.
- CamillaDSP Python client (`camilladsp`, despite the GitHub repo being
  named `pycamilladsp`) is installed from git at `v4.0.0` — it's not on
  PyPI. Matches our CamillaDSP 4.1.3 binary.
- CamillaDSP **4.x schema** uses `S16_LE`/`S32_LE` (was `S16LE`/`S32LE`
  in 3.x) and pipeline filter steps use `channels: [N]` (was
  `channel: N`). Our `v1.yml` is on the 4.x schema.
- openWakeWord stock models don't auto-download — install.sh now calls
  `download_models()` explicitly.
- `openwakeword==0.6.0` hard-pins `tflite-runtime`, which has no Python
  3.13 wheel (PiOS Trixie's default — and Trixie has no python3.12 in
  apt). install.sh installs openwakeword via `--no-deps` plus its
  non-tflite runtime deps (requests/tqdm/scipy/scikit-learn) explicitly;
  we use ONNX models exclusively, so tflite is never imported at runtime.
- **moOde 10.x "Custom" CamillaDSP mode is NOT externally-managed.** It
  still spawns CamillaDSP per-stream via the `pcm.camilladsp` ioplug.
  For long-lived externally-managed CamillaDSP, set moOde CamillaDSP to
  **Off** and route via the ALSA `_audioout` override (B1, B6).
- **moOde won't let you select Loopback as Output device** ("Device is
  reserved" alert). Workaround: ALSA conf.d override redefines
  `pcm._audioout` to point at `hw:Loopback,0,0`, bypassing the moOde
  UI guard. Filename must sort AFTER `_audioout.conf` in ASCII order —
  digit prefixes (e.g. `99-`) sort BEFORE underscore and don't work;
  use `zz-` (lowercase) instead.
- **systemd-launched shairport-sync writes to ALSA `default`, not
  `_audioout`** — moOde's stock `/etc/shairport-sync.conf` has the alsa
  block fully commented out and `DAEMON_ARGS=""`. We drop in
  `/etc/systemd/system/shairport-sync.service.d/jts-output.conf` with
  `ExecStart=/usr/bin/shairport-sync -- -d _audioout` so AirPlay routes
  through our hijack.
- Gemini Live audio shapes confirmed: 16 kHz int16 PCM in,
  24 kHz int16 PCM out, mono.
- `audio_stream_end=True` is a real SDK signal — daemon now sends it on
  end of input.
- Loopback rate-locking is a real risk — moOde Resampling at 48 kHz is
  now a documented requirement, not optional.
- `mpd.service` is the correct systemd unit name in moOde 10.
