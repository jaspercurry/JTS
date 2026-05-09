# Jasper bringup runbook

End-to-end steps from "hardware on desk" to "Hey Jarvis, set volume
to 30." Estimate ~2–3 hours including OS flash, source builds, and
verification.

If anything in here is wrong on first contact with hardware, that's
a bug in this runbook — fix it and update.

---

## What you need on hand

- Raspberry Pi 5 (2GB recommended; 1GB works)
- Official Pi 5 27W USB-C PSU
- Pi 5 active cooler installed
- 32 GB+ A2 microSD card + reader
- Apple USB-C → 3.5mm dongle (must have analog headphones plugged
  into its 3.5mm jack — otherwise the dongle doesn't enumerate
  USB Audio class)
- ReSpeaker XVF3800 (USB UA variant — the one with USB-C, not the
  Pi-HAT version)
- TPA3255 amp + 32V supply
- Speakers + speaker wire
- 3.5mm → RCA cable (or 3.5mm → bare wire) for amp input
- Ethernet cable (optional — you can do all this over Wi-Fi if you
  pre-configured it in Imager)
- Laptop on the same LAN

Optional (satellite devices — see [docs/satellites.md](docs/satellites.md)):
- ELECROW CrowPanel 1.28" HMI ESP32-S3 rotary dial + USB-C cable
  (the wireless physical knob — volume, transport toggle,
  hold-to-talk; Phase 1–3 working on hardware)
- Waveshare ESP32-S3-Touch-AMOLED-1.8 + USB-C cable (touchscreen +
  mic satellite; Phase 0 firmware shipped, Phase 1 push-to-talk
  in progress)

---

## Phase 0 — Flash Raspberry Pi OS Lite (10 min)

1. Download **Raspberry Pi Imager** (<https://www.raspberrypi.com/software/>).
2. Insert the microSD card.
3. In Imager:
   - Operating System → "Raspberry Pi OS (other)" → **Raspberry Pi
     OS Lite (64-bit, Trixie)**
   - Storage → your SD card
   - Click the gear icon (OS customisation):
     - Hostname: `jts` (or whatever)
     - Enable SSH → "Use public-key authentication only" → paste
       your laptop's `~/.ssh/id_*.pub`
     - Set username: `pi` + a strong password (used as fallback;
       you'll prefer SSH keys)
     - Wireless LAN: enter your Wi-Fi SSID + password (so first
       boot comes up on Wi-Fi without Ethernet)
     - Locale: your timezone
   - Save → Write.
4. Eject the SD card. Insert into the Pi. Power on (don't connect
   any USB peripherals yet).

**First-boot wait**: ~60 seconds for the Pi to come up on the
network. Find it via mDNS:

```sh
ssh pi@jts.local   # password fallback if SSH key didn't take
```

Once SSH works:

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git rsync vim
sudo reboot
```

Wait for it to come back, re-SSH.

---

## Phase 1 — Plug in audio peripherals (5 min)

**Plug the Apple USB-C dongle into a Pi USB-A port** with analog
headphones connected to its 3.5mm jack. The dongle ONLY exposes its
USB Audio class endpoint when something is plugged into the analog
side — otherwise it appears as a generic USB device with no audio
interface.

**Plug the ReSpeaker XVF3800 into another Pi USB port.**

Verify both enumerate:

```sh
aplay -l
# Expect to see card "A" (the Apple dongle, "USB-C to 3.5mm")
# and card "Array" (the XVF3800 mic)

arecord -l
# Same — both should be there.
```

If the Apple dongle isn't there: the analog jack is empty. Plug in
headphones (or any analog load) and re-check.

If "A" shows up as a different name (e.g. "USB-Audio", "Headset"):
note it for Phase 3 — you'll need to set `JASPER_TTS_DEVICE`
explicitly. The installer's auto-detection looks for "usb-c to
3.5mm" in the device name; if your dongle reports differently,
adjust.

---

## Phase 2 — Clone the repo and run install.sh (~30–60 min)

The slow part is the source build of `shairport-sync` (~10–15 min)
and `nqptp` (~1 min) for AirPlay 2 support.

```sh
ssh pi@jts.local
git clone https://github.com/jaspercurry/JTS.git ~/jts
cd ~/jts
sudo bash deploy/install.sh
```

`install.sh` is idempotent — re-running upgrades the venv and
re-applies configs. Watch the output for warnings about missing
ALSA cards (the dongle and mic should be detected; if either is
missing, fix and re-run).

After it finishes:

```sh
systemctl status jasper-camilla jasper-voice jasper-mux \
    librespot shairport-sync nqptp bt-agent
# All should show active (running)
```

---

## Phase 3 — Configure /etc/jasper/jasper.env (5 min)

```sh
sudo vim /etc/jasper/jasper.env
```

Required: an API key for whichever real-time voice provider you
want active. The voice loop runs against any of three backends —
pick one, paste the matching key. (You can switch later via the
web wizard in Phase 3.5 without re-editing this file.)

- `GEMINI_API_KEY=<your key from Google AI Studio>` —
  default provider, cheapest (~$0.025/min)
- `OPENAI_API_KEY=<your key from platform.openai.com>` — set
  `JASPER_VOICE_PROVIDER=openai` to make it active (~$0.30/min)
- `XAI_API_KEY=<your key from console.x.ai>` — set
  `JASPER_VOICE_PROVIDER=grok` to make it active (~$0.05/min)

`jasper-voice` refuses to start if the active provider's key is
missing or empty. If you set more than one key, the others stay
benign — the wizard uses them when you switch providers.

Optional but recommended:

- `JASPER_DEFAULT_LOCATION=Brooklyn,NY` — the default city for
  "Hey Jasper, what's the weather?"
- `JASPER_SUBWAY_STATION_ID=B12` — your home subway stop (NYC
  MTA GTFS stop ID; see the comment in the file for how to find
  yours)
- `JASPER_SUBWAY_DEFAULT_DIRECTION=uptown`
- `JASPER_SUBWAY_LINES=D` — which lines stop at your station

Spotify (if you want voice search & queue):

- `SPOTIFY_CLIENT_ID=<from your Spotify Developer App>`
- `SPOTIFY_CLIENT_SECRET=<...>`

After editing:

```sh
sudo systemctl restart jasper-voice
```

---

## Phase 3.5 — Pick a voice provider via the wizard (2 min, optional)

This step is optional — the env file you just wrote already
selects a provider. The wizard at `http://jts.local/voice/` is
the friendlier path: paste keys, pick model and voice from
curated dropdowns, flip the active provider with a single radio
group. Saving writes `/var/lib/jasper/voice_provider.env` (mode
0600), which `jasper-voice.service` sources AFTER
`/etc/jasper/jasper.env` so wizard values override the operator
defaults from Phase 3.

Two reasons to use it:

- **Voice picker labels include gender/style hints.** `marin`
  is "feminine, warm", `ash` is "masculine, soft" — easier to
  pick than reading just the catalogue name.
- **Switch provider without SSH.** Useful for A/B comparisons
  or if you want to flip from Gemini's $0.025/min to OpenAI's
  better instruction-following on the fly.

The page is also available scriptably from your laptop:

```sh
bash scripts/switch-voice-provider.sh           # show current
bash scripts/switch-voice-provider.sh openai    # switch
```

The script refuses if the destination provider's key isn't
already in `jasper.env` or the wizard's env file. See
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md)
for the full per-provider trade-off table.

---

## Phase 4 — Initial volume calibration (2 min)

The Apple dongle's `Headphone` control is the **fixed analog
ceiling, pinned at 100% by `jasper-dac-init` at boot** — software
never adjusts it. CamillaDSP's `main_volume` is the canonical
software volume knob (the dial, voice tools, and the HTTP API all
converge on it). For first-boot calibration:

```sh
# Verify the dongle is at 100% (jasper-dac-init enforces this)
amixer -c A sget Headphone | grep '\[on\]'

# Set CamillaDSP main_volume to a quiet starting level
curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"db": -30.0}' http://localhost:8780/volume/set
```

Listen for fan noise + amp idle hum. If silence is suspiciously
quiet, double-check the amp is on and speakers are connected.

AirPlay something to "JTS" (it should appear in your phone /
laptop's AirPlay picker after a few seconds). At main_volume =
−30 dB you should hear barely-audible audio. Now adjust the
**amp's physical gain knob** until that level is your
"barely-audible" comfort floor. After that, raising main_volume
toward 0 dB (the dial's 100%) puts you at your calibrated
comfortable-max listening level. The dongle stays at 100% always.

---

## Phase 5 — Pair Bluetooth (one-time per device, 2 min)

```sh
sudo bluetoothctl
[bluetooth]# scan on
# Wait for your phone to appear, note its MAC
[bluetooth]# pair AA:BB:CC:DD:EE:FF
# Confirm pairing on your phone
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# exit
```

Now connect from your phone's Bluetooth settings; A2DP audio
should route to the speaker.

---

## Phase 6 — Set up Spotify multi-account (one-time per household member, 5 min each)

If you skipped `SPOTIFY_CLIENT_ID` in Phase 3, skip this.

On your phone (or any browser on the same LAN), visit:

```
http://jts.local/spotify
```

The wizard will walk you through creating a Spotify Developer App,
pasting the redirect URI into Spotify's dashboard, and OAuthing each
household member's account. Auth uses PKCE — only the Client ID is
needed, no Client Secret.

Two redirect modes are offered; pick whichever fits:

- **Bounce (default)** — Spotify redirects via a static page on
  GitHub Pages, which forwards back to `http://jts.local/spotify/…`
  automatically. Smoothest UX. The bounce page is the
  `oauth-callback/index.html` file in this repo, hosted free at
  `https://jaspercurry.github.io/JTS/oauth-callback/`.
- **Manual paste** — no external infrastructure. After you approve
  on Spotify, your phone shows "cannot connect to 127.0.0.1" — the
  wizard pre-warns you about this so it doesn't look like a failure.
  Copy the URL from your address bar, paste it back into the
  speaker's setup page, done.

Repeat for each household member who wants their own Spotify
account routed for voice commands.

---

## Phase 7 — Test wake word + voice (2 min)

```
"Hey Jarvis."
[~1s pause for wake detection + the active voice provider to open a turn]
"What time is it?"
```

You should hear a synthetic voice reply. If not:

```sh
sudo journalctl -u jasper-voice -f
# Watch for wake events and provider errors as you say "Hey Jarvis"
```

Other test prompts:

- "Hey Jarvis, what's the weather?"
- "Hey Jarvis, set volume to 30."
- "Hey Jarvis, when's the next D train?"
- "Hey Jarvis, play Sufjan Stevens." (Spotify; requires Phase 6)

---

## Phase 8 — Run doctor (1 min)

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor
```

Returns 0 if all critical checks pass. The codified version of
this runbook's smoke tests. The doctor reads
`/etc/jasper/jasper.env` and (if present)
`/var/lib/jasper/voice_provider.env` itself, so the active
provider's key is checked regardless of which env file you put it
in.

Common warnings (non-fatal):

- "AEC bridge service: disabled" — software AEC is opt-in. See
  CLAUDE.md "Acoustic echo cancellation" if you want to A/B test.

---

## Optional: ESP32 rotary dial

If you have the CrowPanel ESP32-S3 dial:

```sh
# One-time, on any machine with PlatformIO (or the Pi venv):
bash firmware/dial/build.sh

# Plug the dial into a Pi USB-C port, then on the Pi:
sudo /opt/jasper/.venv/bin/jasper-dial-onboard
# → flashes via esptool, reads Pi's WiFi creds, pushes via Improv,
#   waits for dial to appear at jasper-dial.local. ~30 s.

# Unplug from Pi, connect to USB power. Dial reconnects to WiFi
# from NVS flash on every subsequent boot.
```

The dial's WS2812 LED 0 is a status indicator: magenta=boot,
yellow=connecting, dim green=online, red blink=HTTP error, solid
red=WiFi down.

---

## Optional: Software AEC bridge

Disabled by default. To enable on a Pi with the 6-channel XVF
firmware (see DFU procedure below):

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
See [docs/HANDOFF-aec.md](docs/HANDOFF-aec.md) for the full
trade-off analysis.

### XVF firmware: switch to 6-channel variant via DFU

The default XVF firmware exposes 2 channels (conference + ASR);
the 6-channel variant exposes those plus 4 raw mics needed by the
AEC bridge.

```sh
# On the Pi, with the XVF mic plugged in:
sudo apt install -y dfu-util
# Boot the chip into DFU mode (button combo varies by board rev;
# see ReSpeaker docs for your specific revision)
sudo dfu-util -d 20b1:0008 -a 0 \
    -D /path/to/respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin -R
# Re-plug the mic, then verify:
cat /proc/asound/Array/stream0 | grep Channels
# Expect "Channels: 6"
```

The 6-ch firmware's channel 0 is identical to the 2-ch firmware's
channel 0, so it's safe to leave installed even without enabling
AEC.

**Never call XVF `SAVE_CONFIGURATION`** — known brick hazard on
certain firmware versions (respeaker repo issue #8).

---

## Common failure modes

**"Hey Jarvis" doesn't trigger anything.**
- Check `journalctl -u jasper-voice -f` — wake events log there.
  No log = mic isn't being captured. Verify `JASPER_MIC_DEVICE`
  matches what `arecord -l` shows.

**Wake fires but no voice response.**
- The active provider's API key might be missing/invalid. Check
  `/etc/jasper/jasper.env` (or `/var/lib/jasper/voice_provider.env`
  if you used the `/voice/` wizard) for the right env var:
  `GEMINI_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY`.
- The active provider can be confirmed with
  `grep JASPER_VOICE_PROVIDER /etc/jasper/jasper.env
  /var/lib/jasper/voice_provider.env` (later wins).
- Daily spend cap might be hit. Check
  `cat /var/lib/jasper/usage.db` via sqlite3.

**Music plays but voice TTS is silent (or vice versa).**
- Both write to `pcm.jasper_out` (dmix on the dongle). If only one
  works, the dmix isn't summing — usually means the writers are
  using different rates/formats than the dmix's locked
  rate/format. Check `cat /root/.asoundrc`.

**AirPlay senders see "JTS" but won't connect.**
- shairport-sync.conf must use `plughw:Loopback,0,0` (not bare
  `hw:`). `jasper-doctor` catches this. Plain `hw:` fails the
  44.1→48k rate negotiation silently.

**iPhone / Mac volume slider does nothing.**
- The volume coordinator polls each source's slider at 1 Hz.
  Phone sliders should be reflected within ~2 s. If not, check
  `journalctl -u jasper-voice -f` for "VolumeObserver" log lines.

For deeper debugging:

```sh
# From your laptop:
bash scripts/fetch-pi-logs.sh         # pulls journals + configs to ./logs/
bash scripts/tail-pi-logs.sh           # live tail all units
```

Subsystem-specific issues are documented in the relevant
`docs/HANDOFF-*.md` file.
