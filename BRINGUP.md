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

## Phase 1B — Always-on CamillaDSP via ALSA Loopback (45 min)

### B1. Switch moOde to Custom CamillaDSP mode

1. moOde web UI → Configure → Audio → **CamillaDSP**.
2. Mode: select **Custom**. (This stops moOde from auto-managing CamillaDSP
   per-stream; we want one always-on instance we control.)
3. Click **Set** + restart MPD.

### B2. Load the ALSA Loopback kernel module + reroute MPD output

We'll deploy this via the install script (next step), but you should know
what it's doing:

- `snd-aloop` kernel module gets loaded at boot.
- moOde's MPD writes audio to `hw:Loopback,0,0` (capture side: `hw:Loopback,1,0`).
- A new always-on CamillaDSP instance captures from `hw:Loopback,1,0`,
  applies a master_gain mixer (passthrough at 0 dB), and writes to a shared
  dmix device (`jasper_dongle`) wrapping the Apple dongle.

### B3. Get the repo onto the Pi

```sh
ssh pi@jasper.local
sudo apt-get install -y git
git clone http://github.com/jaspercurry/JTS.git /home/pi/jts
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
- Drop `/etc/camilladsp/v1.yml` (passthrough + master_gain mixer)
- Drop `/etc/modules-load.d/snd-aloop.conf` and `modprobe` it now
- Drop `/root/.asoundrc` (defines `pcm.jasper_dongle` dmix device)
- Create `/opt/jasper/` Python venv and install the daemon
- Create `/etc/jasper/jasper.env` from the template
- Drop `/etc/systemd/system/jasper-camilla.service` and
  `jasper-voice.service` and enable them

### B5. Verify ALSA device names match what we assumed

This is the **single most likely thing to be wrong** because `arecord -L`
output depends on what's plugged in.

```sh
aplay -L | grep -A2 -i headset
arecord -L | grep -A2 -i xvf3800
aplay -L | grep -A2 -i loopback
```

The defaults in `/etc/jasper/jasper.env` assume:
- Apple dongle's ALSA card name is `Headset` → used by `pcm.jasper_dongle`
- XVF3800's ALSA card name is `XVF3800` → used as `JASPER_MIC_DEVICE`
- Loopback is `Loopback` → used in `v1.yml`

If `arecord -L` shows the XVF3800 with a different name (e.g. `Array`), edit
`JASPER_MIC_DEVICE` in `/etc/jasper/jasper.env` and restart. If the dongle
card name isn't `Headset`, edit `/etc/camilladsp/v1.yml` (`playback.device:
"jasper_dongle"` is fine because dmix routes via the asoundrc; edit
`/root/.asoundrc` so the `slave.pcm` line matches the actual hw:CARD=...
name).

### B6. Configure moOde to write to Loopback

Critical: now that CamillaDSP is going to capture from Loopback, moOde
needs to stop writing to the dongle and start writing to Loopback.

1. moOde web UI → Configure → Audio → MPD options → **Audio output**:
   select `Loopback (snd-aloop)` from the dropdown. (If the dropdown shows
   it as `Loopback,0` or `Loop:0,0` — pick the one corresponding to
   subdevice 0, the playback side.)
2. **Resampling**: still Off.
3. **Set** + restart MPD.

### B7. Start CamillaDSP and verify

```sh
sudo systemctl start jasper-camilla
sudo systemctl status jasper-camilla     # should be "active (running)"
journalctl -u jasper-camilla -n 30 --no-pager
```

Now from your phone, AirPlay a track. You should hear it through the dongle
again. The signal path is now:

  phone → AirPlay → moOde → Loopback → CamillaDSP → dmix → dongle → amp

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

### A1. Plug in the XVF3800

Plug into a Pi 5 USB-A port. Confirm:

```sh
arecord -L | grep -i xvf
arecord -d 5 -f S16_LE -r 16000 -c 1 -D plughw:CARD=XVF3800 /tmp/test.wav
aplay /tmp/test.wav
```

Speak during the recording. You should hear yourself back.

### A2. Hardware AEC sanity check

While AirPlaying loud music, repeat the recording. The XVF3800 should
attenuate the music heavily (you should hear yourself clearly above the
nearly-silent music). If the music dominates the recording, AEC isn't
working — check the XVF3800 firmware version, USB cable, USB port.

### A3. Wake-word smoke test

```sh
/opt/jasper/.venv/bin/python -c "
import sounddevice as sd
import numpy as np
from openwakeword.model import Model
m = Model(wakeword_models=['hey_jarvis'])
buf = []
def cb(indata, frames, t, status):
    buf.append(indata[:,0].astype(np.int16, copy=True))
print('say \"Hey Jarvis\" within 5 seconds...')
with sd.InputStream(device='plughw:CARD=XVF3800', samplerate=16000, channels=1, dtype='int16', blocksize=1280, callback=cb):
    sd.sleep(5000)
import numpy as np
audio = np.concatenate(buf)
# scan in 80 ms windows
import numpy as np
peak = max(m.predict(audio[i:i+1280]).get('hey_jarvis', 0) for i in range(0, len(audio)-1280, 1280))
print(f'peak score: {peak:.3f}  (>= 0.5 means detected)')
"
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

### B3. (Optional) Spotify OAuth

Skip this if you only want AirPlay/Bluetooth; it's only required for
voice-driven Spotify search ("Hey Jarvis, play Bohemian Rhapsody").

1. Create a Spotify Developer app at
   <https://developer.spotify.com/dashboard>.
2. **Redirect URI**: `http://127.0.0.1:8765/callback` exactly. Spotify
   rejects `localhost` — must be the literal `127.0.0.1`.
3. Copy Client ID + Client Secret into `/etc/jasper/jasper.env`.
4. On the Pi, run:

   ```sh
   sudo -E /opt/jasper/.venv/bin/jasper-spotify-auth
   ```

   It prints an authorize URL. Open it on your phone. Grant access. Your
   phone redirects to `http://127.0.0.1:8765/callback?code=...` which
   fails to load — that's fine. **Copy the FULL URL from the address bar**
   (must include `?code=...`) and paste it back into the SSH terminal.
   The refresh token is cached at `/var/lib/jasper/.spotify-cache`.

   Spotify control requires Spotify Premium. The Pi must be a Spotify
   Connect target (which moOde provides) for `start_playback` to find a
   device.

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
/etc/modules-load.d/snd-aloop.conf          # snd-aloop loaded at boot
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
```

---

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `journalctl -u jasper-camilla` shows "Cannot open device" | ALSA device names in `/etc/camilladsp/v1.yml` don't match reality | Run `aplay -L` / `arecord -L`, edit `/etc/camilladsp/v1.yml` and `/root/.asoundrc`, `systemctl restart jasper-camilla` |
| Music plays via dongle bypassing CamillaDSP | moOde audio output still set to `Headset` instead of `Loopback` | moOde web UI → Configure → Audio → MPD output → switch to Loopback |
| TTS plays but music doesn't | dmix conflict — both processes opened the dongle at incompatible rates | Confirm `slave.rate 48000` in `/root/.asoundrc`; `aplay -v` shows actual rate |
| Wake word never fires | Mic device wrong, or model file missing, or threshold too high | `arecord -d 5 ... | aplay` to confirm mic; lower `JASPER_WAKE_THRESHOLD` to 0.4 temporarily |
| Wake word fires constantly during music | XVF3800 AEC not working; or threshold too low | Verify XVF3800 firmware; raise threshold to 0.65 |
| "GEMINI_API_KEY not found" on daemon start | Env file not loaded by systemd | Confirm `EnvironmentFile=/etc/jasper/jasper.env` in unit; `systemctl daemon-reload` |
| Spotify tool returns "no active spotify device" | moOde's Spotify Connect endpoint isn't running, or another phone took over | moOde web UI → Configure → Audio → Spotify Connect → Enable. Disconnect any other devices controlling Spotify. |
| Voice ducking doesn't restore | CamillaDSP websocket disconnected mid-session | Check `journalctl -u jasper-voice` for "camilla call failed"; restart `jasper-camilla` |
| Daemon OOM on 1GB Pi 5 | Stack peaks above 750 MB at runtime | Switch to 2GB Pi 5 (the recommended SKU for v1) |

---

## What I am genuinely uncertain about

Honest tally — these are the things to verify with real hardware first:

1. **moOde's audio-output dropdown labels for Loopback subdevs.** moOde's
   UI labelling has changed across versions; the exact dropdown entry name
   may not be "Loopback (snd-aloop)" verbatim. Pick the entry whose device
   string looks like `hw:CARD=Loopback,DEV=0`.
2. **XVF3800 ALSA card name.** Probably `XVF3800` or `XMOS-XVF3800`. Verify
   with `arecord -L` and adjust `JASPER_MIC_DEVICE` accordingly.
3. **Apple dongle ALSA card name.** Almost certainly `Headset` on Pi 5
   Bookworm/Trixie, but confirm via `aplay -L`. If different, edit
   `/root/.asoundrc` `slave.pcm` line.
4. **`google-genai` SDK Preview churn.** Live API is still Preview as of
   May 2026. If Google ships a breaking change to `LiveConnectConfig` or
   `send_realtime_input` between when this was written and when you build,
   pin `google-genai==1.13.0` in `pyproject.toml` and the existing code
   should still work; later upgrade requires reading the changelog.
5. **`turn_complete` semantics under interruption.** I treat any
   `server_content.turn_complete = True` as "model finished a turn." If
   Google later changes this so partial turns also flip the flag, the
   idle watchdog might close the session prematurely. Guard with logging:
   `journalctl -u jasper-voice` will show "idle timeout" lines and the
   token usage will tell you if sessions are too short.
6. **dmix interaction with CamillaDSP's chunksize.** `chunksize: 1024` in
   `v1.yml` is matched to dmix `period_size 1024`. If you see clicks or
   xruns in `journalctl -u jasper-camilla`, try `chunksize: 2048` and
   `period_size 2048` (must be changed in BOTH places).
