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
     → hw:Loopback,0,0 (snd-aloop kernel, sub0)
     │
     ▼
   pcm.jasper_capture
   (type plug → type dsnoop on hw:Loopback,1,0,sub0;
    multiple readers OK — currently just jasper-camilla,
    optionally jasper-aec-bridge if enabled)
     │
     ▼
   jasper-camilla (CamillaDSP, port 1234)
   - master_gain mixer + flat filter (passthrough at 0 dB)
     │
     ▼
   pcm.jasper_out (dmix on Apple dongle, 48 kHz S16_LE)
     │
     ▼
   Apple USB-C dongle → amp → speakers
   (the audible path — the only audible path)
```

`pcm.jasper_out` is just a dmix on the dongle. jasper-camilla and
jasper-voice's TTS both write to it; dmix sums them. Volume ducks
via `SetVolume` on the `master_gain` mixer over CamillaDSP's
websocket on port 1234.

**About the XVF3800 chip:** the chip is a 4-mic array with on-board
DSP. We use it as a microphone via its USB UAC2 capture endpoint
(`hw:CARD=Array,DEV=0`) and read its **conference channel**
(channel 0) — which is the chip's processed output: post-beamformer,
post-noise-suppression, post-AGC. Its **on-chip AEC** is NOT
in the audio path on this build because the chip's AEC pipeline
is architecturally tied to the chip's own DAC driving the speaker,
which doesn't match our external-DAC topology. The chip's onboard
codec's analog output is unconnected; the chip happily drives a
disconnected output.

**Software AEC** is built and installed but **disabled by default**
(`jasper-aec-bridge` service). It runs SpeexDSP echo cancellation
between `pcm.jasper_capture` (reference) and the chip's raw mic 0
(channel 2 on the 6-channel firmware variant) and emits an AEC'd
mono signal to a second snd-aloop card (LoopbackAEC at index 5)
that jasper-voice can consume instead of the chip's processed
mic. See `docs/HANDOFF-aec.md` for the full investigation history
and `README.md` § "Acoustic echo cancellation" for the trade-off.

The default this runbook gets you to: chip-side AEC inert
(harmlessly), software AEC disabled, `JASPER_MIC_DEVICE=Array`
reading the chip's processed conference channel. This is the
**stable baseline.** AEC enable/disable is documented in CLAUDE.md.

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
- Install Python 3, venv, build deps, libasound2, portaudio,
  dfu-util (for XVF firmware flashing if needed), and SpeexDSP +
  swig (used by the optional software AEC bridge)
- Download CamillaDSP `v4.1.3` aarch64 to `/opt/camilladsp/`
  (sha256-verified)
- Drop `/etc/camilladsp/v1.yml` (passthrough + master_gain mixer,
  CamillaDSP 4.x schema with `S16_LE`/`S32_LE` formats and
  `channels: [N]` pipeline filters; capture device is
  `pcm.jasper_capture`, the dsnoop tap)
- Drop `/etc/modules-load.d/snd-aloop.conf` (auto-load) AND
  `/etc/modprobe.d/snd-aloop.conf` (two-card config:
  `index=0,5 id=Loopback,LoopbackAEC pcm_substreams=8,1` —
  card 5 is dedicated to the optional AEC bridge output)
- Drop **`/etc/alsa/conf.d/zz-jts-loopback.conf`** — the
  `pcm._audioout` override that hijacks moOde's renderers into
  snd-aloop Loopback (see "Why this is more involved" above)
- Drop `/root/.asoundrc` (defines `pcm.jasper_capture` — a
  `type plug → type dsnoop` over `hw:Loopback,1,0` so multiple
  readers can share — and `pcm.jasper_out`, a simple dmix on
  the Apple dongle)
- Drop **`/etc/systemd/system/shairport-sync.service.d/jts-output.conf`**
  drop-in forcing systemd-launched shairport-sync to write to `_audioout`
  (without this, shairport-sync writes to ALSA `default` and bypasses
  our hijack)
- Create `/opt/jasper/` Python venv and install the daemon
  (`openwakeword` is installed via `--no-deps` because its declared
  `tflite-runtime` dep has no Python 3.13 wheel; we use ONNX models
  exclusively). pyusb + libusb_package + pyalsaaudio are pulled in
  for the AEC tooling. speexdsp-python is fetched from upstream git
  and `__init__.py` patched (the upstream package has a known
  Python 3.13 packaging quirk).
- Create `/etc/jasper/jasper.env` from the template
- Drop systemd units: `jasper-camilla`, `jasper-voice`,
  `jasper-web`, plus `jasper-aec-bridge` and `jasper-aec-init`.
  **Only the first three are enabled.** The two AEC-bridge units
  are installed but disabled by default — toggle on per CLAUDE.md
  "Acoustic echo cancellation" if you want to A/B test software
  AEC. None auto-start (see B7).
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
| Apple USB-C → 3.5mm dongle | **`A`** (literally the letter A; the device description is "USB-C to 3.5mm Headphone Jack A") | `/root/.asoundrc` `pcm.jasper_out` slave |
| ReSpeaker XVF3800 | **`Array`** (literal ALSA card; PortAudio surfaces as "Array: USB Audio (hw:N,0)") | `/etc/jasper/jasper.env` `JASPER_MIC_DEVICE=Array` (PortAudio substring — NOT `plughw:` — see `jasper/config.py`) |
| MiniDSP UMIK-2 (alt) | **`UMIK2`** ALSA / PortAudio name "UMIK-2: USB Audio (hw:N,0)" | `JASPER_MIC_DEVICE=UMIK-2`, **`JASPER_MIC_CAPTURE_RATE=48000`**, **`JASPER_MIC_CAPTURE_CHANNELS=2`** (no native 16 kHz support — MicCapture downsamples) |
| snd-aloop card 0 | **`Loopback`** (kernel-fixed) | `pcm.jasper_capture` dsnoop slave; jasper-camilla captures via this |
| snd-aloop card 1 (index 5) | **`LoopbackAEC`** (per `/etc/modprobe.d/snd-aloop.conf`) | Optional AEC bridge output. `JASPER_MIC_DEVICE=hw:5,1` when bridge is enabled. |

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

(Note: `jasper-aec-bridge` and `jasper-aec-init` are installed but
not enabled by default — software AEC is opt-in. See CLAUDE.md
"Acoustic echo cancellation" if you want to A/B test it. The rest
of this section assumes the default disabled state.)

Now AirPlay a track from your phone. You should hear it through
the dongle. The signal path is:

```
phone → AirPlay → shairport-sync (-d _audioout)
     → pcm._audioout (zz-jts-loopback.conf override)
     → hw:Loopback,0,0,sub0 (snd-aloop)
     → hw:Loopback,1,0,sub0 capture
     → pcm.jasper_capture (dsnoop)
     → jasper-camilla (master_gain + flat passthrough)
     → pcm.jasper_out (dmix on Apple dongle)
     → hw:A,0 → amp → speakers
```

Sanity-check every leg of the chain (each should show `state: RUNNING`):

```sh
cat /proc/asound/Loopback/pcm0p/sub0/status   # shairport writing
cat /proc/asound/Loopback/pcm1c/sub0/status   # jasper-camilla reading via dsnoop
cat /proc/asound/A/pcm0p/sub0/status          # jasper-camilla → dongle
```

If the chain is healthy, music plays through the speakers and
that's all you need for default operation. The XVF3800 chip's
USB capture endpoint (`/proc/asound/Array/pcm0c/sub0/status`)
will show `RUNNING` once jasper-voice starts in Phase 2; that's
mic capture, not AEC reference.

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
sudo aplay -D plug:jasper_out /tmp/test.wav      # plays via the dongle dmix
```

(Note: `arecord` accepts ALSA `plughw:` strings and resamples — the
daemon goes through sounddevice/PortAudio which doesn't, hence the
CAPTURE_RATE plumbing above.)

### A2. AEC behavior — the default state

**Default: chip-side AEC is inert and software AEC is disabled.**
The chip's processed conference channel (channel 0 of `hw:Array,0`)
still applies beamforming, noise suppression, and AGC — useful
processing — just not echo cancellation. With music playing, the
mic captures the music at typical room SPL.

This is the working state we ship. Whether you need real AEC
depends on how loud the music is during normal use vs. how loudly
the user speaks. Empirically:

- Quiet music + clear voice: openWakeWord triggers reliably.
- Moderate music + close-mic'd voice: also works.
- Loud music + far-field voice: may fail. Mitigate via the
  Gemini session's `NO_INTERRUPTION` flag (already on by default),
  the 5-second wake refractory (also default), and CamillaDSP
  master_gain ducking on wake.

If you find wake-word reliability inadequate during music, the
opt-in software AEC bridge is a tested fallback. It costs ~110 MB
RAM (significant on a 1GB Pi 5; comfortable on 2GB) and delivers
~−2 to −8 dB attenuation — modest, and not measured end-to-end
against wake-word reliability yet. See `docs/HANDOFF-aec.md` for
the full trade-off analysis.

**To enable software AEC:**

```sh
# 1. Make sure the XVF chip is on 6-channel firmware (required —
#    the bridge reads raw mic 0 from channel 2 of the chip's USB
#    capture). One-shot DFU flash, fully reversible — see Phase
#    2A.5 below.

# 2. Switch jasper-voice's mic source to the bridge's output.
sudo sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=hw:5,1|' \
    /etc/jasper/jasper.env

# 3. Enable + start the bridge (and chip-init dependency).
sudo systemctl enable --now jasper-aec-init jasper-aec-bridge

# 4. Restart jasper-voice so it picks up the new mic device.
sudo systemctl restart jasper-voice

# 5. Verify everything is alive.
sudo /opt/jasper/.venv/bin/jasper-doctor
```

`hw:5,1` is the LoopbackAEC card (snd-aloop card index 5 per
`/etc/modprobe.d/snd-aloop.conf`) — PortAudio names all snd-aloop
instances identically as "Loopback: PCM (hw:N,M)" so we use the
unique `hw:N,M` substring to address it. The bridge writes AEC'd
mono to `hw:5,0` and jasper-voice reads from `hw:5,1`.

**To disable software AEC** (revert to default):

```sh
sudo systemctl disable --now jasper-aec-bridge jasper-aec-init
sudo sed -i 's|^JASPER_MIC_DEVICE=.*|JASPER_MIC_DEVICE=Array|' \
    /etc/jasper/jasper.env
sudo systemctl restart jasper-voice
```

Frees ~110 MB RAM. jasper-voice goes back to reading the chip's
processed conference channel directly.

The CLAUDE.md "Acoustic echo cancellation" section duplicates
these toggle commands for AI-session reference. Keep both files
in sync if anything changes.

The **chip's on-chip AEC is structurally not usable** in our
external-DAC topology — see `docs/HANDOFF-aec.md` for the full
investigation. Don't attempt to enable it via `xvf_host` calls;
the chip's AEC pipeline assumes the chip drives the speaker via
its own codec, which we don't do.

### A2.5. (Removed) `jasper-aec-tune` and chip-side AEC tuning

The original chip-side AEC tuning workflow (calibrating
`AUDIO_MGR_SYS_DELAY` via `jasper-aec-tune`) is no longer relevant
to default operation. The script still exists in the repo as a
diagnostic tool, but the chip's AEC isn't in the audio path on
this build, so the value it produces doesn't affect anything
audible. See `docs/HANDOFF-aec.md` for the investigation that led
to abandoning chip-side AEC.

If you ever want to flash the 6-channel XVF firmware (required
for the optional software AEC bridge — exposes raw mics on USB
capture channels 2-5):

```sh
# On the Pi:
sudo systemctl stop jasper-voice jasper-aec-bridge 2>/dev/null
git clone --depth 1 \
    https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git /tmp/xvf
sudo dfu-util -R -e -a 1 \
    -D /tmp/xvf/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin
# Wait ~30 sec for chip to re-enumerate, then:
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
# expect: VERSION: [2, 0, 8]
cat /proc/asound/Array/stream0 | grep Channels
# expect: Channels: 6
```

Reversible (flash a different firmware variant via the same
procedure). **Never call `SAVE_CONFIGURATION`** — brick hazard
on certain firmware versions per respeaker repo issue #8. We
re-apply chip config at boot via `jasper-aec-init` instead, but
that service is also disabled by default.

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
| Setup | None — moOde 10.x bundles librespot 0.8.0; the Pi auto-advertises via zeroconf as "Moode jasper" | Requires a Spotify Developer app + per-household-member OAuth |
| Account | Premium required for use, but no developer registration | Premium required + your own Developer app |
| What you can do | Play any track from Spotify's app to the speaker | "Hey Jarvis, play Bohemian Rhapsody" |

Skip this section entirely if you don't want voice-driven Spotify search;
moOde's Spotify Connect already works without any setup — open Spotify on
your phone, tap the device icon, pick the moOde unit. Done.

The voice daemon supports **multi-user Spotify** — each household member
links their own account once, and the router cross-references the
currently-AirPlay-streamed track title with each account's
`current_playback` to figure out whose voice command should hit which
account. End-to-end architecture, gotchas, and verification commands
live in [docs/multi-user-spotify.md](docs/multi-user-spotify.md); this
section is just the bringup steps.

**1. Create a Spotify Developer app** (one-time, by the speaker owner)
at <https://developer.spotify.com/dashboard>.

- **Name:** anything ("Jasper Smart Speaker")
- **Redirect URI:** `https://jasper.local/spotify/callback` —
  must be HTTPS (Spotify rejects HTTP for non-loopback hosts as of late
  2024). The Pi serves HTTPS via a self-signed cert that
  `deploy/install.sh` generates with the right SANs.
- **APIs:** Web API
- **User Management:** add each household member's Spotify-account
  email. Development Mode allows up to 25 named users.

**2. Drop credentials into `/etc/jasper/jasper.env`:**

```sh
sudo nano /etc/jasper/jasper.env
# Set:
#   SPOTIFY_CLIENT_ID=...
#   SPOTIFY_CLIENT_SECRET=...
sudo chmod 0600 /etc/jasper/jasper.env
sudo systemctl restart jasper-web
```

**3. Each household member, on their own phone, visits**
<https://jasper.local/spotify>:

1. Click through the cert warning ("connection not private" → "visit
   anyway"). One-time-per-device — the browser remembers.
2. Pick a label name (lowercase, no spaces — internal identifier the
   speaker uses to route commands).
3. Click **Continue with Spotify** → log in → **Agree** → bounced back
   to the speaker page. Account now appears in the list.

**4. Restart `jasper-voice` once after the first account is added** so
the router builds clients for the registered accounts:

```sh
sudo systemctl restart jasper-voice
```

After that, voice commands like "Hey Jarvis, play Sufjan Stevens" or
"play my workout playlist" route to the appropriate household member's
Spotify account. The router and ranking logic are documented in
[docs/multi-user-spotify.md](docs/multi-user-spotify.md).

**Legacy single-account fallback.** A `jasper-spotify-auth` CLI still
exists in the repo for headless single-account OAuth (one cache file at
`/var/lib/jasper/.spotify-cache`, no web UI). It's no longer the
canonical path — use the multi-user web flow above. If you have an
existing single-account install, the daemon migrates the legacy cache
into the registry on first start (see `maybe_migrate_legacy()`).

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

## Bringing up a second speaker (additional Pi)

Most steps are identical to the first Pi — Phases 0 → 2B run unchanged.
What follows is just what differs when a unit isn't the first one in the
household.

### Pick a different hostname at flash time

Both Pis can't be `jasper.local`. At Phase 0's Pi Imager step, set
hostname to e.g. `jasper-kitchen` or `jasper-bedroom`. Everything that
references the hostname (Spotify redirect URI, nginx self-signed cert
SAN, fetch-pi-logs scripts) flows from this. The setup web URL becomes
`https://jasper-kitchen.local/spotify`.

### Reuse the Spotify Developer app — just add a redirect URI

You don't create a second Developer app. Open the existing one at
<https://developer.spotify.com/dashboard>, edit settings, and **add an
additional Redirect URI** for the new hostname:

```
https://jasper-kitchen.local/spotify/callback
```

Spotify allows multiple redirect URIs on a single app. Same Client ID
and Client Secret go into the new Pi's `/etc/jasper/jasper.env` —
no developer-side change beyond the redirect URI addition.

### Each household member re-OAuths on the new Pi

OAuth tokens are scoped per-Pi (cache files live at
`/var/lib/jasper/spotify/caches/<name>.json`). Each person who wants
their account on the new speaker visits
`https://jasper-kitchen.local/spotify` once, clicks through the cert
warning, picks the same label name they used on the first Pi (or a
different one — labels are per-Pi), and OAuths. ~10 seconds per person.

### TLS cert auto-generates with the right SANs

`deploy/install.sh` generates the self-signed cert at install time
using the Pi's actual hostname. No manual cert work needed. Each phone
clicks through "not private" once on the new device.

### AEC is a per-room judgment call

Default ships with no real AEC (the chip's processed channel still
does beamforming + NS + AGC). If the second speaker lives somewhere
loud-music-while-speaking is common, plan to flash 6-channel firmware
and enable the software AEC bridge for that unit (see Phase 2A.5 and
A2). Most rooms don't need it.

### Gemini API key — reuse or split

The same key works on both Pis. Split into separate keys per Pi if
you want per-unit spend caps via `JASPER_DAILY_SPEND_CAP_USD`.

### Run `jasper-doctor` to verify

After install, on the new Pi:

```sh
sudo -E /opt/jasper/.venv/bin/jasper-doctor
```

Should turn up green for all critical checks — including the
`Spotify Connect device` and `AirPlay renderer` checks that catch
the most common per-Pi misconfigurations (broadcast-name pattern
mismatch, AirPlay disabled in moOde, mDNS not advertising). If
anything fails, the message is the fix.

### Time estimate

~1.5 hours for a second Pi if you skip the optional software AEC
bridge, ~2 hours with it. Faster than the first Pi because you
already know the moOde web UI clicks and have the Spotify Developer
app set up — most of the savings is in Phases 1A and 2B.

---

## Reference: file layout on the Pi after install

```
/opt/camilladsp/camilladsp                  # 4.1.3 binary
/etc/camilladsp/v1.yml                      # main DSP: passthrough +
                                            #   master_gain ducking;
                                            #   captures from
                                            #   pcm.jasper_capture
/etc/modules-load.d/snd-aloop.conf          # snd-aloop loaded at boot
/etc/modprobe.d/snd-aloop.conf              # two-card config (Loopback +
                                            #   LoopbackAEC at index 5);
                                            #   second card is reserved
                                            #   for the optional bridge
/etc/alsa/conf.d/zz-jts-loopback.conf       # pcm._audioout override
                                            #   redirecting to Loopback
/root/.asoundrc                             # pcm.jasper_capture (dsnoop
                                            #   on Loopback,1,0,sub0)
                                            #   + pcm.jasper_out (dmix
                                            #   on Apple dongle)
/opt/jasper/                                # Python pkg (managed by install.sh)
  .venv/                                    # virtualenv
  jasper/                                   # source (incl. cli/aec_init,
                                            #   cli/aec_tune, cli/aec_bridge,
                                            #   cli/aec_matrix, xvf/xvf_host)
/etc/jasper/jasper.env                      # API keys + tunables (chmod 600)
/var/lib/jasper/                            # state dir
  usage.db                                  # SQLite spend log
  .spotify-cache                            # Spotify refresh token
  aec_delay.txt                             # vestigial (chip-side AEC
                                            #   tune output; not used in
                                            #   default operation)
  aec-matrix-*.{json,md}                    # produced by jasper-aec-matrix
                                            #   when run (not in default
                                            #   bringup)
/etc/systemd/system/jasper-camilla.service       # ENABLED
/etc/systemd/system/jasper-voice.service         # ENABLED
/etc/systemd/system/jasper-web.service           # ENABLED (Spotify OAuth)
/etc/systemd/system/jasper-aec-bridge.service    # installed, NOT enabled
/etc/systemd/system/jasper-aec-init.service      # installed, NOT enabled
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
| Wake word fires constantly during music | No AEC in default config; mic is hearing the speaker. Expected behavior. | Reduce music volume during voice interactions (the daemon already ducks ~15 dB on wake), or enable software AEC bridge per CLAUDE.md "Acoustic echo cancellation". Last resort: raise `JASPER_WAKE_THRESHOLD` from 0.5 to 0.65 in `/etc/jasper/jasper.env`. |
| (Bridge enabled) `journalctl -u jasper-aec-bridge` shows "Cannot open device" or import errors | Required deps not installed, or 6-ch firmware not flashed | Re-run `bash deploy/install.sh`. Confirm 6-ch firmware: `cat /proc/asound/Array/stream0 \| grep Channels` should say 6. If not, see Phase 2A.5 for DFU flash. |
| (Bridge enabled) `cat /proc/asound/LoopbackAEC/...` doesn't exist | snd-aloop two-card config not loaded | `cat /etc/modprobe.d/snd-aloop.conf` should exist and say `index=0,5`. Then `sudo rmmod snd_aloop && sudo modprobe snd-aloop` (best done after stopping all loopback users). |
| "GEMINI_API_KEY not found" on daemon start | Env file not loaded by systemd | Confirm `EnvironmentFile=/etc/jasper/jasper.env` in unit; `systemctl daemon-reload` |
| Spotify tool returns "no active spotify device" | moOde's Spotify Connect endpoint isn't running, or another phone took over | moOde web UI → Configure → Audio → Spotify Connect → Enable. Disconnect any other devices controlling Spotify. |
| Voice ducking doesn't restore | CamillaDSP websocket disconnected mid-session | Check `journalctl -u jasper-voice` for "camilla call failed"; restart `jasper-camilla` |
| Daemon OOM on 1GB Pi 5 | Stack peaks above 750 MB at runtime | Switch to 2GB Pi 5 (the recommended SKU for v1) |
| Wake fires, ducks, but no model response (sessions ending with `SILENT FAILURE: sent N bytes... received 0 chunks`) | `gemini-3.1-flash-live-preview` is silently degraded for your project (server accepts WS, accepts audio, sends nothing back — not a 409, not a quota error in the SDK). Confirmed by direct text-turn probe returning 0 responses while `gemini-2.5-flash-native-audio-preview-12-2025` works on the same key | Run `bash scripts/switch-gemini-model.sh 2.5` from the laptop. Same-class Live API model (Google explicitly published it as 3.1's predecessor); same code path, same voices, same SDK. Run `switch-gemini-model.sh 3.1` to flip back when 3.1 unsticks. |

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
