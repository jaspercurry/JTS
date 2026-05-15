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
  GitHub Pages, which forwards back to `http://${JASPER_HOSTNAME}/spotify/…`
  automatically. Smoothest UX. The bounce page is a separate public
  repo, `jaspercurry/spotify-oauth-callback`, served at
  `https://jaspercurry.github.io/spotify-oauth-callback/`. The wizard
  shows the exact redirect URI (with `?host=` set to your speaker's
  hostname) for you to paste into the Spotify dashboard.
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

`install.sh` runs the doctor at the end of every install, so
nothing should be surprising here — this phase is just a sanity
check that everything's still healthy after the manual steps.

**Mic-side checks worth knowing about** (they pass silently when
fine, surface the exact fix when not):

- **XVF firmware 6-ch** — bridge can't run without 6-channel
  firmware. If it warns, jump to the DFU section below.
- **XVF mixer state** — kernel ALSA mixer can have ch2-5 muted
  even when firmware is 6-ch (a trap on chips flashed 2-ch → 6-ch
  mid-bringup). Reconciler self-heals; doctor flags drift.
- **AEC bridge service** — software AEC is the *desired* state, so:
  - `ok (running)` — bridge active, AEC on
  - `ok (disabled JASPER_AEC_MODE=disabled)` — explicit operator opt-out
  - `warn (off — XVF on 2-channel firmware)` — gentle nudge to DFU-flash
  - `warn (off — Array chip not present)` — XVF needs to be plugged in
  - `fail` — conditions for AEC are met but bridge isn't running (real bug; paste the suggested commands)

If you want to go deeper on any mic issue, the canonical reference
is [docs/HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md) and the
deep-diagnostic tool is `bash scripts/xvf-interrogate.sh --host
<pi>` (run from your laptop, captures everything to `logs/`).

---

## Phase 9 — Trust the speaker's HTTPS cert on each iPhone (one-time, 1 min per device)

This step is **only required** if you want to use the room-correction
wizard at `https://jts.local/correction/`. The Spotify, voice, and dial
settings pages don't need it (they're plain HTTP). If you don't plan
to run room correction yet, skip this section — you can come back any
time.

`getUserMedia` (microphone access in the browser) requires a secure
context, so the correction page is the one route on this speaker that
has to be HTTPS. `install.sh` provisions a private CA on the Pi the
first time it runs and signs a server cert for `${JASPER_HOSTNAME}`
from it; the user-visible step is installing that CA on each iPhone
(or iPad, or Mac) once.

On each iPhone:

1. In Safari, visit `http://jts.local/jts-root-ca.crt`. Safari
   downloads the file silently and prompts: *"This website is trying
   to download a configuration profile. Do you want to allow this?"*
   Tap **Allow**.
2. Open the **Settings** app. There will be a new entry near the top:
   *"Profile Downloaded — JTS Speaker Local CA"*. Tap it.
3. Tap **Install** (top right). Enter your passcode if asked. Tap
   **Install** again on the consent screen, then **Done**.
4. Go to **Settings → General → About → Certificate Trust Settings**.
   Toggle **JTS Speaker Local CA** on. iOS shows a confirmation
   dialog warning that "Enabling this certificate for websites will
   allow third parties to view any private data sent to websites" —
   this is the standard warning Apple shows for any non-public CA
   and is fine for a personal smart speaker on your home network.
   Tap **Continue**.

Verify by visiting `https://jts.local/correction/` in Safari. The
page should load without a "Connection is not private" warning, and
tapping **Start mic capture** should bring up the standard iOS
microphone permission prompt.

If the cert was reissued after a hostname change (`JASPER_HOSTNAME`
edited and `install.sh` re-run), only the leaf cert changes — the CA
on the iPhone keeps working, no re-trust needed. If you ever wipe
`/var/lib/jasper/ca` and run `install.sh` again, the old CA on the
iPhone still appears in Certificate Trust Settings but no longer
matches; remove it (Settings → General → VPN & Device Management →
JTS Speaker Local CA → Remove Profile) and repeat steps 1-4.

To remove the CA from an iPhone (e.g., decommissioning a speaker):
**Settings → General → VPN & Device Management → JTS Speaker Local CA
→ Remove Profile**.

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

`install.sh` runs `jasper-aec-reconcile`, which auto-enables AEC on
a Pi running the 6-channel XVF firmware and clears stale UDP mic
config when the Array is absent. To enable manually (e.g. you flashed
6-ch after install and don't want to re-run install.sh):

```sh
printf 'JASPER_AEC_MODE=auto\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

The bridge→voice transport is UDP localhost since May 2026 (was
a second snd-aloop card before that, retired for resilience —
see [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)).

To disable:

```sh
printf 'JASPER_AEC_MODE=disabled\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

Verify with `sudo /opt/jasper/.venv/bin/jasper-doctor` either way.
See [docs/HANDOFF-aec.md](docs/HANDOFF-aec.md) for the full
trade-off analysis.

### XVF firmware: switch to 6-channel variant via DFU

#### Why this step exists

The XVF3800 ships from Seeed on a "2-channel" firmware variant.
That firmware's USB capture endpoint exposes only two channels —
channel 0 is the chip's beamformed + AEC + noise-suppressed
**Conference** output, channel 1 is its speech-recognition-tuned
**ASR** output. Both are post-processed by the chip's on-board DSP
and intended for use as a single conversational microphone.

JTS's software AEC bridge needs the chip's raw, pre-DSP
microphone outputs instead. Those only exist on the **"6-channel"
firmware variant**, which adds raw mic 0–3 on capture channels
2–5. Without that firmware, the AEC bridge can't run, and
wake-word detection runs against the chip's conference channel
only — works in a quiet room, false-wakes heavily when music is
playing.

The 6-channel firmware is a strict superset of the 2-channel
firmware: channel 0 (Conference) and channel 1 (ASR) carry the
same content on either. Switching back is reversible and lossless;
it just removes the raw channels.

#### Which firmware to flash

As of **2026-05-15**, the recommended file is:

```
respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin
```

It's the only 6-channel variant currently in upstream `master`.
Source of truth: [`xmos_firmwares/usb/`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb)
in the Seeed-maintained
[`respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY)
repo.

**Before flashing, check the upstream directory for newer entries.**
If a newer 6-channel variant exists (e.g. PR #13's v2.0.10 attempt
at 48 kHz 6-channel was unmerged as of this writing), read its
changelog/PR description against what JTS depends on:

- channel 0 = Conference (post-DSP beam output)
- channel 1 = ASR (post-DSP, speech-tuned)
- channels 2–5 = raw mic data feeding `jasper-aec-bridge`

If those channels survive the upgrade, the new version should drop
into JTS by bumping three constants in
[`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py):
`FIRMWARE_BLOB_6CH`, `FIRMWARE_KNOWN_GOOD_BLD_REPO_HASH`, and
`FIRMWARE_KNOWN_GOOD_AS_OF`. The fuller variant table is in
[`docs/HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md) §2.

#### How DFU works on this chip (no button combo needed)

The XVF3800 supports **in-system DFU upgrade**. Its USB interface
descriptor advertises a DFU function (Application Specific class
254, alt 1 = Upgrade slot) alongside its normal audio class
interfaces, available continuously while the chip is in runtime
mode. `dfu-util` writes directly to that interface; the chip
briefly enumerates as the XMOS bootloader at `20b1:0008` during
the actual flash, then resets back to the normal audio device at
`2886:001a`.

You may have read elsewhere (the Seeed wiki, older drafts of
this doc, ESPHome examples) about putting the chip into "DFU
mode" via a button combo. **That procedure is for Safe Mode
recovery only** — used when the DataPartition is corrupted, e.g.
after an unsafe `SAVE_CONFIGURATION` call has bricked normal boot.
For a routine 2-ch → 6-ch firmware upgrade, no button combo is
needed.

#### Step 1 — fetch the firmware

```sh
# On the Pi, with the XVF mic plugged in normally:
sudo apt install -y dfu-util curl

# As of 2026-05-15 the latest 6-ch firmware in upstream master is
# v2.0.8. Check the directory listing before flashing in case a
# newer version has shipped:
#   https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb
curl -L -o /tmp/xvf-6ch.bin \
    https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/raw/master/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin
md5sum /tmp/xvf-6ch.bin
# Record this hash — if Seeed re-cuts the same filename with new
# bits in the future, the md5 will change and you'll know to
# re-read the changelog before flashing again.
```

#### Step 2 — confirm the chip exposes DFU

```sh
sudo dfu-util -l
# Expect a line resembling:
#   Found DFU: [2886:001a] devnum=N, cfg=1, intf=4, path="...",
#       alt=1, name="reSpeaker DFU Upgrade", serial="..."
# If alt=1 isn't visible, the chip isn't in normal runtime — re-plug
# and recheck. (alt=0 "Factory" is read-only; don't try to write to it.)
```

#### Step 3 — flash

```sh
sudo dfu-util -R -e -a 1 -D /tmp/xvf-6ch.bin
# ~30-60 seconds. You'll see:
#   - "Invalid DFU suffix signature" — this is NORMAL. Seeed doesn't
#     sign their binaries; dfu-util warns but proceeds.
#   - Progress percentage climbing to 100%
#   - "File downloaded successfully"
#   - "Resetting USB to switch back to runtime mode" (the -R flag)
# The chip disappears from USB momentarily then re-enumerates with
# the new firmware. dmesg shows the re-enumeration.
```

The flag breakdown for future reference: `-a 1` writes to the
Upgrade partition (not the read-only Factory at alt 0). `-R`
resets the chip after flashing so it boots into the new firmware.
`-e` detaches (exits DFU) before download — harmless and required
on some host stacks.

#### Step 4 — verify the new firmware is running

```sh
# Capture-side channel count — pin to the Capture: section because
# /proc/asound/Array/stream0 has Playback (Channels: 2) before
# Capture (Channels: 6), and a naive `grep Channels:` returns the
# wrong one.
awk '/^Capture:/{c=1} c && /Channels:/{print; exit}' /proc/asound/Array/stream0
# Expect: "Channels: 6"

# Chip-side build identification:
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_MSG
# Expect: ['u','a','-','i','o','1','6','-','6','c','h','-','s','q','r']
#         (the chip-reported BLD_MSG = "ua-io16-6ch-sqr")

sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_REPO_HASH
# For v2.0.8 6chl as of 2026-05-15, expect hash:
#   'a1f70651e992d6f0bcff655b26925d33999b9c2d'
# Newer versions will report different hashes — that's fine, the
# value is for change-detection, not validation.
```

#### Step 5 — bring AEC online

The reconciler picks up the new firmware, flips voice's mic source
to the AEC bridge's UDP output, and resets the kernel ALSA mixer
to known-good values for the newly-exposed ch2-5 (which can
otherwise persist a stale mute from before the firmware change —
see "The reconciler step matters" below).

```sh
sudo systemctl start jasper-aec-reconcile

# Confirm everything's healthy:
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E '(AEC bridge|XVF)'
# Expect three "✓" lines:
#   AEC bridge service       running (software AEC enabled)
#   XVF firmware 6-ch        capture is 6-channel
#   XVF mixer state          all 6 capture channels open
```

#### Why the reconciler step matters

When the chip is flashed from 2-channel to 6-channel firmware,
ALSA assigns new mixer slots in the kernel for the newly-exposed
capture channels 2–5. Their defaults are off / 0 dB. `alsactl
restore` then happily persists that silently across reboot —
killing the raw mics in spite of the new firmware, with no
surface that would let an operator notice (chip-side params look
healthy, `/proc/asound/Array/stream0` shows 6 channels, but
`arecord` returns zeros on ch2-5).

The reconciler's `ensure_capture_mixer_open` resets the relevant
controls to all-on / max-volume and runs `alsactl store` so the
state survives reboot. `jasper-doctor`'s "XVF mixer state" check
flags drift if anything sets them back. This is exactly the trap
that consumed half a day on jts2's bringup in May 2026
(`docs/HANDOFF-xvf3800.md` §7 has the full investigation).

If the reconciler is unavailable for any reason and you need to
fix the mixer state manually:

```sh
sudo amixer -c Array cset name='Headset Capture Switch' on,on,on,on,on,on
sudo amixer -c Array cset name='Headset Capture Volume' 60,60,60,60,60,60
sudo alsactl store
```

#### What if it goes wrong

| Symptom | What it means | Where to go |
|---|---|---|
| `dfu-util -l` doesn't see alt=1 | Chip isn't in normal runtime — likely a USB enumeration issue | Re-plug, check `dmesg -T \| grep -i usb` |
| Flash fails mid-write, chip won't boot | Brick — `SAVE_CONFIGURATION` corruption is the documented cause | Safe Mode recovery via `4mb_all_ff.bin`, [HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md) §5.1 |
| Doctor shows XVF firmware 6-ch ✓ but mixer state ✗ | Kernel mixer drifted; reconciler hasn't run | Re-run `sudo systemctl start jasper-aec-reconcile` |
| Doctor shows everything ✓ but wake word still fails | Probably unrelated to firmware; check `journalctl -u jasper-voice -f` and `scripts/xvf-interrogate.sh` | [HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md) diagnostic cookbook |

#### Sources for this section

- **Firmware blobs + DFU protocol semantics**: [upstream `xmos_firmwares/dfu_guide.md`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/xmos_firmwares/dfu_guide.md) and the `xmos_firmwares/usb/` directory listing in the same repo.
- **In-system DFU mechanism**: confirmed empirically via `lsusb -v -d 2886:001a` showing the Application Specific class 254 interface at alt 1 = "reSpeaker DFU Upgrade" while the chip is in normal audio runtime. Same descriptor visible on both jts and jts2 chips on 2026-05-15.
- **Channel layout per firmware variant**: [Seeed wiki — Update Firmware section](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/#update-firmware), cross-verified against the `BLD_MSG` strings the chip itself reports.
- **`SAVE_CONFIGURATION` brick hazard**: [upstream issue #8](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/8) (still open as of this writing — treat the warning as applying to every firmware version we've shipped against).
- **ALSA mixer mute trap after firmware flash**: discovered during the 2026-05-15 jts2 raw-mic-silence investigation; full root cause and resolution log in [HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md) §7.

**Never call XVF `SAVE_CONFIGURATION`** — known brick hazard on
every firmware version we've tested (upstream issue above hasn't
been confirmed fixed in any release notes). The chip's parameter
state is fine to set at runtime via `xvf_host` writes; just don't
persist them to flash via that command.

---

## Common failure modes

**"Hey Jarvis" doesn't trigger anything.**
- Check `journalctl -u jasper-voice -f` — wake events log there.
  No log = mic isn't being captured. Verify `JASPER_MIC_DEVICE`
  matches what `arecord -l` shows.
- For deeper mic debugging (chip identity, USB descriptors,
  ALSA state, XVF firmware, per-channel activity), run
  `bash scripts/xvf-interrogate.sh --host jts.local` from your
  laptop. Output lands in `logs/` tagged by chip iSerial. The
  canonical reference is [docs/HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md).

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
