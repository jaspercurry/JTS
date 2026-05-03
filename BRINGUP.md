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
| ReSpeaker XVF3800 | **`Array`** (literally; description "reSpeaker XVF3800 4-Mic Array") | `/etc/jasper/jasper.env` `JASPER_MIC_DEVICE=plughw:CARD=Array` |
| snd-aloop | **`Loopback`** (kernel-fixed) | `/etc/camilladsp/v1.yml` capture device |

If your `aplay -L` / `arecord -L` shows different names, edit the relevant
file and `systemctl restart jasper-camilla` (and/or `jasper-voice`).

### B6. Configure moOde to write to Loopback at 48 kHz

Critical: CamillaDSP captures from Loopback at 48 kHz. snd-aloop **does
not resample** — whatever rate the writer uses locks the capture rate.
AirPlay sources are 44.1 kHz natively, so without resampling on moOde's
side, the rate flips between 48 kHz and 44.1 kHz across track changes
and CamillaDSP throws "broken pipe" (CamillaDSP issues #311 / #315).
Fix: tell moOde to resample everything to 48 kHz before writing to
Loopback.

1. moOde web UI → **Configure → Audio → MPD options**.
2. **Audio output**: pick the entry whose device path looks like
   `hw:CARD=Loopback,DEV=0`. moOde 10.x's exact label for this entry isn't
   publicly documented and may vary — look for "Loopback", "snd-aloop", or
   "Loop:0,0" in the dropdown; pick the playback side (subdevice 0).
3. **Resampling**: enable **SoX resampling**. Output rate **48000 Hz**,
   bit depth **16**, channels **2**.
4. Click **Set** + restart MPD when prompted.

If your moOde version doesn't expose Loopback in the Audio output dropdown,
the manual fallback is `/etc/mpd.conf`:

```conf
audio_output {
    type           "alsa"
    name           "JasperLoopback"
    device         "plughw:CARD=Loopback,DEV=0"
    format         "48000:16:2"
    auto_resample  "no"
    auto_format    "no"
    auto_channels  "no"
}
```

Then `sudo systemctl restart mpd` and verify the new output is selected
in moOde's UI.

### B6.1. AirPlay rate caveat

AirPlay (shairport-sync) is a separate renderer that bypasses MPD. Its
output rate is configured in `/etc/shairport-sync.conf`. If AirPlay is
your primary streaming source AND you hit "broken pipe" issues on
CamillaDSP after Phase 2 starts, the fix is:

```sh
sudo nano /etc/shairport-sync.conf
# In the `general` block, set:
#     output_format = "S16_LE";
#     output_rate = 48000;
# Save, then:
sudo systemctl restart shairport-sync
```

For v1, document this only if you actually hit the issue — it doesn't
always trigger because moOde's plughw layer often masks rate transitions.

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

The XVF3800 has a USB-C port on the device side; you'll need a USB-C → USB-A
cable (Pi 5's USB-A ports are best for peripherals; the Pi 5 USB-C is
power-only). Plug it into a USB-A port. Confirm:

```sh
arecord -L | grep -B1 -i 'xvf3800\|array'
arecord -d 5 -f S16_LE -r 16000 -c 1 -D plughw:CARD=Array /tmp/test.wav
aplay /tmp/test.wav
```

Speak during the recording. You should hear yourself back. If the card
name isn't `Array`, edit `JASPER_MIC_DEVICE` in `/etc/jasper/jasper.env`
and swap the `-D` flag above.

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
with sd.InputStream(device='plughw:CARD=Array', samplerate=16000, channels=1, dtype='int16', blocksize=1280, callback=cb):
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

## What I am still uncertain about

A second cold-review pass closed several earlier uncertainties. What's
left to confirm on real hardware:

1. **moOde 10.x Audio Output dropdown label for the Loopback entry.**
   moOde's docs don't publicly specify the exact UI string. Pick the entry
   whose underlying device path is `hw:CARD=Loopback,DEV=0`. If the
   dropdown doesn't include Loopback at all, use the `/etc/mpd.conf`
   manual fallback in B6.
2. **AirPlay (shairport-sync) rate handling under Custom CamillaDSP mode.**
   moOde's audio infrastructure uses `plughw:Loopback` on the writer side,
   which usually masks rate transitions. But shairport-sync configures its
   output independently of moOde's MPD resampler. If you hit "broken pipe"
   in `journalctl -u jasper-camilla` immediately after starting AirPlay,
   apply the shairport-sync fix in B6.1.
3. **`google-genai` SDK Preview churn.** Live API is still Preview as of
   May 2026. If Google ships a breaking change to `LiveConnectConfig` or
   `send_realtime_input` between when this was written and when you build,
   the pinned `google-genai==1.13.0` should keep things working; later
   upgrade requires reading the changelog. The `VoiceSession` interface
   contains the blast radius to one adapter file.
4. **dmix interaction with CamillaDSP's chunksize.** `chunksize: 1024` in
   `v1.yml` is matched to dmix `period_size 1024`. If you see clicks or
   xruns in `journalctl -u jasper-camilla`, try `chunksize: 2048` and
   `period_size 2048` (change in BOTH places).
5. **moOde's bundled CamillaDSP 3.0.1 vs our 4.1.3.** install.sh stops and
   disables `camilladsp.service` to avoid contention, but if a future
   moOde release renames that unit, our disable becomes a no-op. Watch
   for two camilladsp processes in `ps auxf | grep camilladsp` after a
   moOde upgrade.

### Resolved since first draft

- ALSA card names confirmed: dongle = `A`, ReSpeaker = `Array`.
- pycamilladsp pinned at `4.0.0` (matches our CamillaDSP 4.1.3 binary;
  3.0.0 would have been ABI-mismatched).
- openWakeWord stock models don't auto-download — install.sh now calls
  `download_models()` explicitly.
- Gemini Live audio shapes confirmed: 16 kHz int16 PCM in,
  24 kHz int16 PCM out, mono.
- `audio_stream_end=True` is a real SDK signal — daemon now sends it on
  end of input.
- Loopback rate-locking is a real risk — moOde Resampling at 48 kHz is
  now a documented requirement, not optional.
- `mpd.service` is the correct systemd unit name in moOde 10.
