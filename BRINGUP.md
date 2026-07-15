# Jasper bringup runbook

End-to-end steps from "hardware on desk" to "Hey Jarvis, set volume
to 30." Estimate ~2–3 hours including OS flash, source builds, and
verification.

This is the long-form **advanced/operator** runbook. It includes
manual SSH, Pi-local package installs, hardware checks, firmware
flashing, and calibration steps. If this is your first JTS speaker
and you want the guided consumer setup, start with
[QUICKSTART.md](QUICKSTART.md) instead. The normal beginner install
path runs from your computer:

```sh
bash scripts/onboard.sh <hostname>.local --adopt
```

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
   - Device → **Raspberry Pi 5**
   - Operating System →
     **Raspberry Pi OS → Raspberry Pi OS (Other) → Raspberry Pi
     OS Lite (64-bit)**
   - Storage → your SD card
   - Customisation:
     - Hostname: `jts` for the first speaker, or another simple
       name. If you choose `jts3`, use `jts3.local` in later
       commands and browser URLs.
     - Localisation: choose your capital city, then confirm the time
       zone and keyboard layout.
     - User: username `pi` plus a password.
     - Wireless LAN: enter the same Wi-Fi network your laptop is on
       so first boot comes up without Ethernet.
     - Remote Access / SSH: enable SSH with **password
       authentication**. Public-key auth is fine for advanced
       imaging, but the beginner path is password SSH plus
       `scripts/onboard.sh --adopt`. (Right after onboarding you'll
       also enable **passwordless sudo** — see Phase 2.5; it's what
       lets `deploy-to-pi.sh` and AI-agent sessions deploy unattended.)
     - Raspberry Pi Connect: leave off; JTS does not use it.
     - Interfaces & Features: leave defaults unless a later phase
       explicitly tells you otherwise.
   - Save → Write.
4. Imager usually auto-ejects the SD card after writing. Physically
   remove it from the computer and insert it into the Pi. If you need
   to inspect `bootfs`, physically reinsert the card into the
   computer first.
5. Power on (don't connect any USB peripherals yet).

**First-boot wait**: ~60 seconds for the Pi to come up on the
network. The examples below assume the hostname `jts`; if you chose
another hostname, substitute its `.local` address:

```sh
ssh pi@jts.local
```

If you are following the beginner path, stop here and run the
laptop-side onboarder from the repo checkout on your computer:

```sh
bash scripts/onboard.sh <hostname>.local --adopt
```

The remaining phases are advanced/operator detail. They include
hardware verification and manual service checks, but the supported
install path still runs from the laptop unless a section explicitly
labels a Pi-local developer alternative.

For the manual path, once SSH works:

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y rsync vim
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

## Phase 2 — Run the laptop-side onboarder (~30–60 min)

The slow part is the source build of `shairport-sync` (~10–15 min)
and `nqptp` (~1 min) for AirPlay 2 support.

From your laptop, in a local JTS checkout:

```sh
bash scripts/onboard.sh <hostname>.local --adopt
```

The onboarder rsyncs this checkout to `$HOME/jts`, captures the
laptop-side git SHA before `.git/` is excluded, passes that build
metadata into the remote sudo install, and runs `deploy/install.sh`.
The Pi consumes the staged source tree and pinned/hash-checked source
archives; it does not need `git` for the normal public install path.

`install.sh` is idempotent — re-running `bash scripts/onboard.sh
<hostname>.local --adopt` or `bash scripts/deploy-to-pi.sh` upgrades
the venv and re-applies configs. Watch the output for warnings about
missing ALSA cards (the dongle and mic should be detected; if either
is missing, fix and re-run).

<details>
<summary>Advanced/developer Pi-local checkout path</summary>

Use this only when intentionally developing directly on the Pi or when
you cannot rsync from a laptop. It makes the Pi a checkout host and
therefore requires `git`; it is not the normal public install path.

```sh
ssh pi@jts.local
sudo apt install -y git
git clone https://github.com/jaspercurry/JTS.git ~/jts
cd ~/jts
sudo JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh
```

Substitute the Pi's actual speaker hostname. A direct Pi-local
`install.sh` run reads `JASPER_HOSTNAME` from the process environment;
it does not source an existing `/etc/jasper/jasper.env` first. The
normal laptop-side `scripts/deploy-to-pi.sh` path forwards the hostname
for you.

</details>

After it finishes:

```sh
systemctl status jasper-camilla jasper-voice jasper-mux \
    librespot shairport-sync nqptp bt-agent.service
# All should show active (running)
```

---

## Phase 2.5 — Enable passwordless sudo (recommended; do this for every Pi)

**Why this matters — read even if you're in a hurry.** The supported
deploy path (`bash scripts/deploy-to-pi.sh`) and every AI-agent or
scripted session run *unattended*: they SSH in and run `sudo bash
install.sh` with nobody at the keyboard. They preflight `sudo -n true`
(non-interactive sudo) and **refuse to proceed without it** — the project
will not store or hand-roll your sudo password. Two things make
unattended deploys work; set up both early so you never hit a wall
mid-session:

1. **Pubkey SSH** — so SSH needs no password. `scripts/onboard.sh
   <hostname>.local --adopt` already did this for you (it runs
   `ssh-copy-id`). Your *first* onboard runs interactively from your
   terminal, so sudo could prompt for a password then — that's why Phase
   2 worked without this step.
2. **Passwordless sudo** — so `sudo -n` works for every deploy *after*
   the first. `--adopt` deliberately does **not** set this up (the
   installer never adds broad sudoers rules for you — it's your explicit
   choice). This phase is that choice.

**Your two options:**

| Option | What every deploy looks like | Pick this if |
|---|---|---|
| **Passwordless sudo** (recommended) | `deploy-to-pi.sh` runs fully unattended; AI agents can deploy across sessions with you out of the loop. | A home-LAN appliance you own — the normal case. |
| **Keep a sudo password** | You must run *every* deploy yourself from an interactive terminal so it can prompt. Unattended and agent-driven deploys are impossible. | You specifically want sudo to require a password. |

For the JTS appliance, **passwordless sudo is the right posture.** It is a
trusted-LAN device you own, its deploys already require root to install and
update system services (and the boot/recovery reconcilers still run as root),
and the threat model ([SECURITY.md](SECURITY.md)) already assumes a trusted
household network. The alternative — re-typing a password on every deploy
and blocking every agent session — buys you almost nothing here.

**Enable it** (one-time per Pi). SSH in, run these (the first `sudo`
prompts for the Pi password once), then exit:

```sh
ssh pi@<hostname>.local
# then, on the Pi:
echo "pi ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010_pi-nopasswd >/dev/null
sudo chmod 440 /etc/sudoers.d/010_pi-nopasswd
sudo visudo -cf /etc/sudoers.d/010_pi-nopasswd   # must print: parsed OK
exit
```

Verify from your laptop:

```sh
ssh pi@<hostname>.local 'sudo -n true && echo PASSWORDLESS_SUDO_OK'
```

**Revoke later** with `sudo rm /etc/sudoers.d/010_pi-nopasswd`.

> **Do this on EVERY speaker you bring up — each Pi has its own sudo
> config.** The classic failure mode (a real one): your main speaker has
> passwordless sudo and deploys fine, but a second unit (e.g. a lab Pi)
> silently doesn't, so deploys and agent sessions to it fail at the
> `sudo -n` preflight until you run this phase there too.

---

## Phase 3 — Configure /etc/jasper/jasper.env (5 min)

```sh
sudo vim /etc/jasper/jasper.env
```

Required: an API key for whichever real-time voice provider you
want active. The voice loop runs against any of three backends —
paste the matching key (or all three if you plan to A/B). You
pick the active provider via the wizard in Phase 3.5, **not** in
this file — `JASPER_VOICE_PROVIDER` is wizard-owned per PR #166
and lives only in `/var/lib/jasper/voice_provider.env`.

- `GEMINI_API_KEY=<your key from Google AI Studio>` — Gemini
  Live (~$0.025/min)
- `OPENAI_API_KEY=<your key from platform.openai.com>` — OpenAI
  Realtime (~$0.30/min)
- `XAI_API_KEY=<your key from console.x.ai>` — xAI Grok (~$0.05/min)

Fresh installs have no provider selected; `jasper-voice` refuses
to start with a clear error until the wizard writes one. If you
set more than one key, the others stay benign — the wizard uses
them when you switch providers.

Optional but recommended:

- `JASPER_DEFAULT_LOCATION=Brooklyn,NY` — the default city for
  "Hey Jasper, what's the weather?"

NYC subway, bus, and Citi Bike are wizard-managed at
`http://jts.local/transit/` — **do not set the `JASPER_SUBWAY_*` /
`JASPER_BUS_*` / `JASPER_MTA_BUSTIME_KEY` / `JASPER_CITIBIKE_*`
variables in `jasper.env`**. Type your home address; the wizard
geocodes via OSM Nominatim and shows nearby stops + stations.
Subway is keyless; bus needs a free MTA BusTime API key (linked
from the wizard, ~30 min approval); Citi Bike is keyless (GBFS is
public). If those vars are already in `jasper.env` from an older
install, `install.sh` migrates them into
`/var/lib/jasper/transit.env` automatically on the next deploy.

Spotify (if you want voice search & queue):

- `SPOTIFY_CLIENT_ID=<from your Spotify Developer App>` — that's
  all. PKCE flow is used; no Client Secret is required (the wizard
  at `http://jts.local/spotify` is the preferred path and writes
  this for you).

After editing:

```sh
sudo systemctl restart jasper-voice
```

---

## Phase 3.5 — Pick a voice provider via the wizard (2 min, REQUIRED)

`JASPER_VOICE_PROVIDER` is wizard-owned (PR #166) — `jasper-voice`
refuses to start until you've picked one. Visit
`http://jts.local/voice/`: paste keys, pick model and voice from
curated dropdowns, flip the active provider with a single radio
group. Saving writes `/var/lib/jasper/voice_provider.env` (mode
0640 group `jasper`, provider selection only) and
`/var/lib/jasper-secrets/voice_keys.env` (mode 0640 group
`jasper-secrets`, API keys), which `jasper-voice.service` sources via
`EnvironmentFile=`.
`install.sh` actively migrates any stale `JASPER_VOICE_PROVIDER`
out of `/etc/jasper/jasper.env` on each run, so the wizard file is
the only place it lives.

Bonus reasons to use the wizard (beyond it being required):

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

## Phase 3.6 — Configure transit (one-time, 2 min, optional)

Skip this phase if you're not in NYC (or Jersey City / Hoboken — Citi
Bike covers those). The voice tools work without it; queries about
"next train" / "next bus" / "Citi Bike situation" without
configuration get a polite "transit isn't set up — visit
`jts.local/transit` to configure it."

From any browser on the LAN:

```
http://jts.local/transit/
```

The wizard:

1. **Geocodes your home address** via OSM Nominatim (no API key, no
   account). Only the resulting coordinates (rounded to ~110 m) are
   saved on the speaker — the address itself never lands on disk.
2. Renders one card per transit provider whose coverage area
   includes your coordinates. As of now:
   - **NYC Subway** (keyless) — pick a station + default direction
     ("uptown" / "downtown" / "both"). "Next train" returns every
     line stopping at the station, including service-change reroutes.
   - **NYC MTA Bus** — needs a free MTA BusTime API key. The card
     is locked until you paste one; register at the link inside the
     wizard (~30 min approval window). Multi-stop support: save both
     directions at your corner and voice answers union them.
   - **NYC Citi Bike** (keyless, GBFS-backed) — multi-station picker
     with a household-wide "Only mention e-bikes" toggle for if your
     household only rides e-bikes. Each station's voice answer
     splits classic-bike vs. e-bike counts and reports open docks.
     Covers NYC + Jersey City + Hoboken.

Save and the daemon restarts in ~5 seconds, picking up the new
config. Everything lives in `/var/lib/jasper/transit.env` (wizard-
owned; the systemd unit's `EnvironmentFile=` sources it).

> **Do not** put `JASPER_SUBWAY_*`, `JASPER_BUS_*`,
> `JASPER_MTA_BUSTIME_KEY`, or `JASPER_CITIBIKE_*` in
> `/etc/jasper/jasper.env`. If they're there from an older install,
> `install.sh` migrates them into the wizard file on the next deploy.

---

## Phase 3.7 — Connect Home Assistant (one-time, 2 min, optional)

Skip if you don't run Home Assistant. Smart-home requests without
configuration get a polite "smart-home isn't set up yet — visit
`jts.local/ha` to enable it" with no model misroute.

From any browser on the LAN:

```
http://jts.local/ha/
```

The wizard walks three states:

1. **Find Home Assistant.** Click the discovery button to mDNS-browse
   the LAN for `_home-assistant._tcp` — usually finds your HA in
   ~4 seconds. Cross-subnet setups (HA on a different VLAN) get a
   manual URL field; paste whatever address you use to open HA in
   your browser (e.g. `http://homeassistant.local:8123` or
   `http://192.168.1.42:8123`).
2. **Paste a Long-Lived Access Token.** In Home Assistant, open
   the link the wizard provides (`<HA URL>/profile/security`),
   scroll to the bottom, click **Create Token**, name it
   "JTS Speaker", and paste the token into the wizard. The wizard
   validates against `GET /api/` before saving — invalid tokens
   bounce back to this step with the error.
   - **HTTPS with self-signed cert?** The wizard renders an "Accept
     a self-signed certificate" checkbox for `https://` URLs. Check
     it only for your own LAN-internal HA installs (Nabu Casa /
     Let's Encrypt / any publicly-trusted cert: leave unchecked).
3. **Connected.** Status card shows your HA's name + version. The
   advanced disclosure lets you override which HA conversation
   agent JTS routes to — leave empty to use whatever HA's default
   is (lowest friction).

The household's existing setup carries over with zero extra work:
sentence triggers (`bedroom medium` → custom automation), exposed
scripts, scenes, areas, aliases, and any LLM-backed conversation
agent the user has configured inside HA. JTS is a relay through
HA's own conversation pipeline.

Configuration lives in `/var/lib/jasper-intsecrets/home_assistant.env`
(wizard-owned, mode 0640 group `jasper-intsecrets` — token is a secret).
The systemd unit's `EnvironmentFile=` sources it. Headless installs
(CI / imaging pipelines) can write the file directly:

```sh
sudo install -D -m 0640 -g jasper-intsecrets /dev/stdin /var/lib/jasper-intsecrets/home_assistant.env <<EOF
JASPER_HA_URL=http://homeassistant.local:8123
JASPER_HA_TOKEN=eyJ0eXAi...
EOF
sudo systemctl restart jasper-voice
```

See [docs/HANDOFF-homeassistant.md](docs/HANDOFF-homeassistant.md)
for the architecture (why HA's conversation API rather than its MCP
server), the failure-mode taxonomy, and the v1.1+ upgrade path.

---

## Phase 3.8 — Enable USB audio input (one-time, 10 min, optional)

Plug a computer (Mac, Windows, Linux) into the Pi's USB-C port and
JTS appears as a USB audio output device on the host. Music plays
through the speakers, the Mac volume slider drives JTS's volume, the
mux preempts/restores it just like AirPlay or Spotify. Disabled by
default; low single-digit MB RAM when on, on top of the gadget's own
baseline (see the note below).

Skip this phase if you only ever stream from phones (AirPlay/Spotify
Connect/Bluetooth cover that). Come back when you want to use JTS as
the audio output for a laptop.

Full design + RAM analysis + failure-mode matrix:
[docs/HANDOFF-usbsink.md](docs/HANDOFF-usbsink.md).

**Same USB-C port, hardware-conditional management network.** On a Pi 5,
the USB-C port carries an NCM network link (`ncm.usb0`) after install — plugging
a laptop in gets you `http://<JASPER_HOSTNAME>/` (or the documented fallback
`http://10.12.194.1/`) even with Wi-Fi off, whether or not USB Audio Input is
enabled. On a Zero-class speaker with a USB output DAC, its one OTG data port is
reserved for that DAC, so both the management link and USB Audio Input are
unavailable. A registered I²S DAC leaves the Zero port free. The Sources page
shows the resolved availability; see
[docs/HANDOFF-usb-gadget.md](docs/HANDOFF-usb-gadget.md) for the policy.

### Hardware prerequisite

The Pi 5's USB-C port serves both power and data. To use it as a
USB-gadget data port to a host computer while keeping the Pi powered,
you need a **USB-C/PWR splitter**: it provides the data leg to the
host and a separate power leg to the wall PSU.

- **8086 Consultancy USB-C/PWR Splitter** (~$30,
  [8086.net/products/usb-c-pwr-splitter](https://www.8086.net/products/usb-c-pwr-splitter))
  — the only tested option. Sidesteps the Pi 5
  USB-C-to-USB-C kernel issue
  [raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289)
  because the host-side leg terminates in USB-A.
- USB-A-to-USB-C cable (~$10) for the data leg.
- Your existing Pi 5 USB-C PSU stays in use — connect it to the
  splitter's power leg instead of directly to the Pi.

Physical topology:

```
Wall outlet
   │
   ▼
27W USB-C PSU ───► 8086 Splitter ◄─── USB-A cable ◄─── Host computer
                       │
                       ▼ (combined power + data over USB-C)
                  Pi 5 USB-C port
```

When no host is connected, JTS still powers up normally — nothing
about standalone behavior changes.

### Side effect to be aware of

After this phase, the Pi 5's USB-C port is **no longer usable for
plugging USB host devices** (flash drives, etc.) because it's
permanently in peripheral mode. The four USB-A ports remain in host
mode unchanged.

### Steps

#### 1. Set the dtoverlay (one-time, requires reboot)

`install.sh`'s `reconcile_usb_data_role` step ran during Phase 2 and selected
peripheral mode for the Pi 5. A role change takes effect on the next reboot.
Verify and reboot only when the Sources page or doctor reports one is needed:

```sh
ssh pi@jts.local 'grep dwc2,dr_mode=peripheral /boot/firmware/config.txt'
# Expect: dtoverlay=dwc2,dr_mode=peripheral

# Reboot. Pi will boot with the BCM2712's DWC2 controller in peripheral mode.
# The hardware-gated composite gadget may load immediately for the default-on
# NCM management link; the Sources toggle controls only its UAC2 audio function.
ssh pi@jts.local 'sudo reboot'

# Wait ~30 s, then verify:
ssh pi@jts.local 'lsmod | grep dwc2'
# Expect: dwc2 ... (loaded)
ssh pi@jts.local 'lsmod | grep libcomposite'
# Expect: loaded when the USB management network is enabled (the default), or
# empty only when both the network kill switch and USB Audio Input are Off.
```

#### 2. Wire up the splitter

```
Pi USB-C port ◄─── splitter ◄─── PSU (power leg)
                       ◄─── USB-A-to-C ─── (no host connected yet)
```

The Pi should power up normally. SSH still works (Wi-Fi unchanged).

#### 3. Enable the USB toggle in `/sources/`

Open `http://jts.local/sources/` on any device on the LAN. Find the
**USB Audio Input** row, flip the toggle on. Within ~3 s:
- `jasper-usbgadget.service` restarts and recomposes the ConfigFS
  descriptor to add the `uac2.usb0` audio function (on this gadget-capable
  Pi 5, the default-on `ncm.usb0` network function was already there)
- `jasper-fanin.service` opens the gadget capture endpoint; the process-free
  `jasper-usbsink.service` readiness marker becomes active (exited)

Verify on the Pi:

```sh
ssh pi@jts.local 'ls /proc/asound/UAC2Gadget/'
# Expect: a directory (the gadget's ALSA card)

ssh pi@jts.local 'systemctl is-active jasper-usbsink jasper-usbgadget'
# Expect: active / active
```

#### 4. Plug a host computer in

Plug your Mac/Windows/Linux laptop into the splitter's USB-A leg.

- **macOS**: Open System Settings → Sound → Output. The device appears
  under your **Speaker Name** (e.g. **JTS**; truncated to 15 chars for
  this label). Select it. (If it instead shows **"Playback Inactive"**,
  the name patch didn't apply — see the failure-modes table below and
  `jasper-doctor`'s `usbsink name` check. Audio still works either way.)
- **Windows**: Open Sound settings, choose JTS USB Audio as output
  (or the renamed USB Audio device).
- **Linux**: Should auto-route via PulseAudio/PipeWire, or `pactl
  set-default-sink alsa_output.usb-Linux_Foundation_*`.

#### 5. Play music from the host

Open any media player on the host. Audio should come out of the JTS
speakers within a few hundred milliseconds.

- **Volume**: move the host's volume slider. JTS volume follows
  within ~250 ms (the same slider experience as AirPlay sender
  volume).
- **Source switching**: start AirPlay from your phone. The mux
  preempts USB; phone audio takes over. Stop AirPlay, hit play on
  the host again — USB takes back over.

#### 6. Run the doctor

```sh
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor' | grep -i usbsink
# Expect OK lines including:
#   USB data role: ok
#   usbsink state: ok playing=... host_connected=...
#   usbsink card: ok UAC2Gadget present
#   usbsink name: ok device name patched to track Speaker Name '...'
```

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Toggle unavailable with a reboot note | The installer resolved a different role than the one active in this boot | Re-run `bash scripts/deploy-to-pi.sh` if needed, then reboot |
| Toggle unavailable because the output DAC owns the port | This is a Zero-class speaker using its shared OTG port as a USB host | Use the USB DAC normally, or configure a supported I²S DAC if USB gadget input is required |
| Host doesn't see the speaker in its audio device picker | Splitter not wired (forgot the USB-A cable to host), or `jasper-usbgadget` didn't recompose with the audio function | `journalctl -u jasper-usbgadget` for ConfigFS errors |
| Mac says "Playback Inactive" instead of the Speaker Name | Name patch didn't apply (kernel renamed the string, or override stale). Cosmetic — audio still plays | `journalctl -u jasper-usbgadget \| grep event=usbsink_name`; `sudo systemctl restart jasper-usbgadget`; check `jasper-doctor` `usbsink name` |
| Volume slider on Mac doesn't move JTS | `amixer -c UAC2Gadget controls` should show `PCM Capture Volume`; if missing, gadget descriptor wasn't built with `c_volume_present=1` | `journalctl -u jasper-usbsink-volume \| grep volume_bridge` |
| Toggle off but `lsmod \| grep libcomposite` shows it loaded | RAM-drift from a previous bad stop — jasper-doctor will warn | `sudo rmmod u_audio libcomposite` or reboot |

### Disable later

Flip the toggle off in `/sources/`. The daemon stops, and
`jasper-usbgadget.service` restarts and recomposes **without**
`uac2.usb0` — the host loses JTS from its audio device list within
~3 s, but the `ncm.usb0` management network function stays
up (plugging a laptop in still gets you `http://<JASPER_HOSTNAME>/`).
See [docs/HANDOFF-usb-gadget.md](docs/HANDOFF-usb-gadget.md) for the
full function truth table. The hardware-resolved role stays in place either way
(harmless on its own — DWC2 in peripheral mode with no gadget
descriptor at all is a no-op from the host's perspective).

To turn off the USB management network as well (kill switch, not a
dtoverlay rollback):

```sh
ssh pi@jts.local "echo JASPER_USB_NETWORK=disabled | sudo tee -a /etc/jasper/jasper.env"
ssh pi@jts.local 'sudo systemctl restart jasper-usbgadget'
```

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

AirPlay something to "JTS" (or the display name configured at
`http://jts.local/speaker/`; it should appear in your phone /
laptop's AirPlay picker after a few seconds). At main_volume =
−30 dB you should hear barely-audible audio. Now adjust the
**amp's physical gain knob** until that level is your
"barely-audible" comfort floor. After that, raising main_volume
toward 0 dB (the dial's 100%) puts you at your calibrated
comfortable-max listening level. The dongle stays at 100% always.

---

## Phase 5 — Pair Bluetooth (one-time per device, 2 min)

Open `http://jts.local/bluetooth/`, turn **Pairing mode** on, then
choose the speaker from your phone's Bluetooth settings. Pairing mode
opens a five-minute no-code window: the speaker is visible and accepts
new Just Works pairings, then automatically closes. No PIN, passkey,
or numeric-comparison prompt should appear.

After pairing, connect from your phone's Bluetooth settings; A2DP audio
should route to the speaker. Already-paired devices can reconnect later
without leaving pairing mode on.

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

The default wake phrase as of 2026-05-16 is **"Jarvis"** (the
fwartner community model, trained on the phrase set `"jarvis"` /
`"hey jarvis"` / `"jarvis!"` / `"jarvis?"` — so either form
triggers it). Try the shorter form first:

```
"Jarvis."
[~1s pause for wake detection + the active voice provider to open a turn]
"What time is it?"
```

You should hear a synthetic voice reply. "Hey Jarvis" works too.
To pick a different wake phrase — Hey Jarvis, Alexa, Hey Mycroft —
visit `http://jts.local/wake/` from any LAN device, or run
`bash scripts/switch-wake-word.sh <key>` from your laptop. See
[AGENTS.md "Wake-word switching"](AGENTS.md#wake-word-switching--read-first)
for the registry and how to add a new model.

If wake isn't firing:

```sh
sudo journalctl -u jasper-voice -f
# Watch for wake events and provider errors as you say "Jarvis"
```

Other test prompts:

- "Jarvis, what's the weather?"
- "Jarvis, set volume to 30."
- "Jarvis, when's the next D train?"
- "Jarvis, play Sufjan Stevens." (Spotify; requires Phase 6)

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

The doctor also re-checks presence and hashes for JTS-staged opaque
runtime model files that otherwise fail later with cryptic
ONNX/runtime errors: required openWakeWord package assets, the active
wake model when it is registry-pinned, and the configured DTLN-aec ONNX
stages when DTLN is enabled.

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
- **Audio profile** — read-only intent-vs-runtime truth from the same
  classifier as `/aec` and `/state.aec`: requested profile, active
  profile, session source, wake legs, and any pending/unavailable warning.
- **AEC bridge service** — the bridge should be active whenever a mic
  profile needs its UDP outputs. In software profiles it runs WebRTC
  AEC3; in chip-AEC profiles it forwards the selected chip beam and
  bypasses WebRTC AEC3.
  - `ok (running (software AEC3 enabled))` — software profile active
  - `ok (running (chip-AEC beam forwarding; WebRTC AEC3 bypassed; ...))`
    — chip-AEC profile active
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

If the cert was reissued after a hostname change, only the leaf cert
changes — the CA on the iPhone keeps working, no re-trust needed. For
Pi-local reruns, pass the hostname explicitly:
`sudo JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh`. The
normal laptop-side `scripts/deploy-to-pi.sh` path forwards it
automatically. If you ever wipe `/var/lib/jasper/ca` and run
`install.sh` again, the old CA on the iPhone still appears in
Certificate Trust Settings but no longer matches; remove it (Settings
→ General → VPN & Device Management → JTS Speaker Local CA → Remove
Profile) and repeat steps 1-4.

To remove the CA from an iPhone (e.g., decommissioning a speaker):
**Settings → General → VPN & Device Management → JTS Speaker Local CA
→ Remove Profile**.

---

## Optional: ESP32 rotary dial

If you have the CrowPanel ESP32-S3 dial:

```sh
# One-time, explicit accessory firmware build (not part of base install):
bash /opt/jasper/firmware/dial/build.sh

# Plug the dial into a Pi USB-C port, then on the Pi:
sudo /opt/jasper/.venv/bin/jasper-dial-onboard
# → flashes via esptool, reads Pi's WiFi creds, pushes via Improv,
#   waits for dial to appear at jasper-dial.local. ~30 s.

# Unplug from Pi, connect to USB power. Dial reconnects to WiFi
# from NVS flash on every subsequent boot.
```

The web wizard at `http://jts.local/dial/` shows whether
`/opt/jasper/firmware/dial/jasper-dial.bin` is missing, current, or
stale relative to the staged source and prints the same build command
when a dial owner needs it.

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

> **Mic doesn't show up as a USB device at all?** If `lsusb` /
> `arecord -l` never list the XVF3800 — you see only an Espressif
> `303a:1001` "USB JTAG/serial debug unit", or nothing — even though
> the board powers up (PWR LED lit) and the cable carries data, you
> have a **Flex / XIAO-ESP32S3 variant that shipped in I2S mode.** It
> is *not* a USB device until you flash USB firmware, so the in-system
> DFU below won't see it. You must first enter **Safe Mode via the
> BOOT button** — hold BOOT while powering on the **XMOS USB-C port**
> (the one next to the 3.5 mm jack, *not* the XIAO/Seeed port; *not*
> the Mute-button procedure). Full variant identification + the
> validated recovery steps are in
> [docs/HANDOFF-xvf3800.md](docs/HANDOFF-xvf3800.md) §2.6. Once it
> enumerates as `2886:001a` / card `Array`, return here.

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

Pick the firmware by **physical microphone geometry**, not by the
fact that the chip is an XVF3800:

| Board | Firmware | Expected runtime identity |
|---|---|---|
| Legacy square/circular XVF3800 USB 4-Mic Array | `respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin` from [`respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb) | `BLD_MSG=ua-io16-6ch-sqr`, USB `2886:001a`, ALSA `Array` |
| ReSpeaker Flex XVF3800 **LINEAR-4** | `respeaker_flex_usb_l16k6ch_v1.0.1.bin` from [`respeaker/reSpeaker_Flex`](https://github.com/respeaker/reSpeaker_Flex/tree/main/xmos_firmwares/usb) | `BLD_MSG=ua-io16-6ch-lin`, USB `2886:0022`, ALSA `L16K6Ch` |

The old `ua-io16-6ch-sqr` blob will enumerate and expose raw mics on
a linear board, but its chip processed beams and DoA assume square
geometry. Use it only for raw-channel-only diagnostics. For JTS wake
and AEC tuning on the Flex LINEAR-4, flash the linear Flex blob and
start retuning from `xvf_software_aec3` / raw-mic corpus legs.

**Before flashing, check the upstream directory for newer entries.**
If a newer 6-channel variant exists, read its changelog/PR
description against what JTS depends on:

- channel 0 = Conference (post-DSP beam output)
- channel 1 = ASR (post-DSP, speech-tuned)
- channels 2–5 = raw mic data feeding `jasper-aec-bridge`

If those channels survive the upgrade, the new version should drop
into JTS by bumping three constants in
[`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py):
`FIRMWARE_BLOB_6CH` / Flex equivalents, build hash constants, and
`*_KNOWN_GOOD_AS_OF`. The fuller variant table is in
[`docs/HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md) §2.

#### How DFU works on this chip (no button combo needed)

The XVF3800 supports **in-system DFU upgrade**. Its USB interface
descriptor advertises a DFU function (Application Specific class
254, alt 1 = Upgrade slot) alongside its normal audio class
interfaces, available continuously while the chip is in runtime
mode. `dfu-util` writes directly to that interface; the chip
briefly enumerates as the XMOS bootloader at `20b1:0008` during
the actual flash, then resets back to the normal audio device:
`2886:001a` for the legacy square/circular firmware, `2886:0022`
for Flex firmware.

You may have read elsewhere (the Seeed wiki, older drafts of
this doc, ESPHome examples) about putting the chip into "DFU
mode" via a button combo. **That procedure is for Safe Mode
recovery only** — used when the DataPartition is corrupted, e.g.
after an unsafe `SAVE_CONFIGURATION` call has bricked normal boot.
For a routine 2-ch → 6-ch firmware upgrade, no button combo is
needed. **One exception:** a Flex / XIAO board that shipped in I2S
mode was never a USB device, so its *first* USB flash does require
Safe Mode entry (the BOOT button on those boards) — see the callout
at the top of this section and HANDOFF-xvf3800.md §2.6.

#### Step 1 — fetch the firmware

```sh
# On the Pi, with the XVF mic plugged in normally:
sudo apt install -y dfu-util curl

# Legacy square/circular board, known-good as of 2026-05-15:
#   https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb
curl -L -o /tmp/xvf-6ch.bin \
    https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/raw/master/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin

# Flex LINEAR-4 board, JTS hash-pinned updater target as of 2026-06-29
# (prior jts5 hardware verification used v1.0.0 on 2026-06-19):
#   https://github.com/respeaker/reSpeaker_Flex/tree/main/xmos_firmwares/usb
curl -L -o /tmp/xvf-flex-linear-6ch.bin \
    https://github.com/respeaker/reSpeaker_Flex/raw/main/xmos_firmwares/usb/respeaker_flex_usb_l16k6ch_v1.0.1.bin
sha256sum /tmp/xvf-flex-linear-6ch.bin
# Expected SHA256:
#   85743239b4c4b069fb153b4a23f29dde9c29f34768b47601fa92daaaf09f2a99

md5sum /tmp/xvf-6ch.bin
# Record this hash — if Seeed re-cuts the same filename with new
# bits in the future, the md5/SHA256 will change and you'll know to
# re-read the changelog before flashing again.
```

#### Step 2 — confirm the chip exposes DFU

```sh
sudo dfu-util -l
# Expect a line resembling:
#   Found DFU: [2886:001a] devnum=N, cfg=1, intf=4, path="...",
#       alt=1, name="reSpeaker DFU Upgrade", serial="..."
# Flex firmware in runtime mode reports [2886:0022] instead.
# If alt=1 isn't visible, the chip isn't in normal runtime — re-plug
# and recheck. (alt=0 "Factory" is read-only; don't try to write to it.)
```

#### Step 3 — flash

```sh
sudo dfu-util -R -e -a 1 -D /tmp/xvf-6ch.bin
# or, for the Flex LINEAR-4:
sudo dfu-util -R -e -a 1 -D /tmp/xvf-flex-linear-6ch.bin
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
# Flex LINEAR-4:
awk '/^Capture:/{c=1} c && /Channels:/{print; exit}' /proc/asound/L16K6Ch/stream0
# Expect: "Channels: 6"

# Chip-side build identification:
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_MSG
# Expect: ['u','a','-','i','o','1','6','-','6','c','h','-','s','q','r']
#         (the chip-reported BLD_MSG = "ua-io16-6ch-sqr")
# Flex LINEAR-4 expects: ['ua-io16-6ch-lin']

sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_REPO_HASH
# For v2.0.8 6chl as of 2026-05-15, expect hash:
#   'a1f70651e992d6f0bcff655b26925d33999b9c2d'
# For Flex LINEAR-4 v1.0.0 (jts5 hardware verification, 2026-06-19),
# this reported:
#   '4b339d00721937451ee487759c04e2acb3215793'
# The current hash-pinned target is v1.0.1 (2026-06-29) — its
# BLD_REPO_HASH has not yet been recorded from hardware. Newer
# versions will report different hashes — that's fine, the value is
# for change-detection, not validation.
```

#### Step 5 — bring AEC online

The reconciler picks up the new firmware, flips voice's mic source
to the AEC bridge's UDP output, and resets the kernel ALSA mixer
to known-good values for the newly-exposed ch2-5 (which can
otherwise persist a stale mute from before the firmware change —
see "The reconciler step matters" below).

On a fresh install, `deploy/install.sh` seeds `JASPER_MIC_DEVICE` from
the detected card. On existing Pis, do not hand-pin
`JASPER_AEC_MIC_DEVICE` when swapping between legacy square/circular
(`Array`) and Flex LINEAR-4 (`L16K6Ch`) firmware. The reconciler derives
that bridge mic from the detected XVF profile for selectable input
profiles and writes the current card back to `/etc/jasper/jasper.env`.
`JASPER_AUDIO_INPUT_PROFILE=custom` remains the escape hatch for a
deliberately hand-pinned mic.

```sh
sudo systemctl start jasper-aec-reconcile

# Confirm everything's healthy:
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E '(Audio profile|AEC bridge|XVF)'
# Expect four "✓" lines:
#   AEC bridge service       running (software AEC3 enabled)
#   Audio profile            requested=xvf_software_aec3, active=xvf_software_aec3, ...
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
healthy, `/proc/asound/<card>/stream0` shows 6 channels, but
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
# Flex LINEAR-4 uses ALSA card L16K6Ch:
sudo amixer -c L16K6Ch cset name='Headset Capture Switch' on,on,on,on,on,on
sudo amixer -c L16K6Ch cset name='Headset Capture Volume' 60,60,60,60,60,60
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
- **Flex geometry-specific firmware blobs**: [`respeaker/reSpeaker_Flex` `xmos_firmwares/usb/`](https://github.com/respeaker/reSpeaker_Flex/tree/main/xmos_firmwares/usb) and the [Seeed Flex wiki](https://wiki.seeedstudio.com/respeaker_flex/).
- **In-system DFU mechanism**: confirmed empirically via `lsusb -v -d 2886:001a` showing the Application Specific class 254 interface at alt 1 = "reSpeaker DFU Upgrade" while the chip is in normal audio runtime. Same descriptor visible on both jts and jts2 chips on 2026-05-15; jts5 Flex LINEAR-4 was flashed through the same normal-runtime DFU flow on 2026-06-19 and re-enumerated as `2886:0022`.
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
- The active provider's API key might be missing/invalid. Keys
  live in `/etc/jasper/jasper.env` — check for the right one:
  `GEMINI_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY`.
- The active provider lives in `/var/lib/jasper/voice_provider.env`
  (the only place since PR #166): `grep JASPER_VOICE_PROVIDER
  /var/lib/jasper/voice_provider.env`.
- Daily spend cap might be hit. Visit `http://jts.local/voice/` for the
  spend-cap status/settings; the underlying ledger is
  `/var/lib/jasper/usage.db` if you need to inspect it with sqlite3.

**Music plays but voice TTS is silent (or vice versa).**
- In solo mode, assistant TTS/cues enter `jasper-fanin` over
  `/run/jasper-fanin/tts.sock`, then travel through CamillaDSP before
  `jasper-outputd` owns the final DAC. Check
  `systemctl status jasper-fanin jasper-outputd`,
  `journalctl -u jasper-voice -u jasper-fanin`, `/state.outputd`, and
  `cat /etc/asound.conf`.
- In multi-room bonded mode, the grouping reconciler may instead layer
  `/var/lib/jasper/grouping-voice.env` so voice targets outputd's
  member-local TTS lane at `/run/jasper-outputd/tts.sock`; the matching
  outputd lane is armed from `/var/lib/jasper/grouping-outputd.env`.
  `jasper-doctor`'s grouping check reports stale or half-armed TTS lane
  drift.

**AirPlay senders see the speaker but won't connect.**
- shairport-sync.conf must use `shairport_substream`, the AirPlay
  private fan-in lane. `jasper-doctor` catches stale bare
  `hw:Loopback,*` or retired `jasper_renderer_in` wiring.

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
