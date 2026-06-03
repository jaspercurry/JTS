# JTS QUICKSTART

From "I just bought a Pi" to "Hey Jarvis, play Taylor Swift" in
about 30 minutes — most of which is the speaker building software
by itself while you do something else.

> [!TIP]
> **Using Claude Code?** Skip this doc and just say *"set up a Pi"*
> (or similar). Claude auto-invokes the [`/onboard-pi`](.claude/commands/onboard-pi.md)
> skill and walks you through every step interactively — Raspberry Pi
> Imager, SD card flash, first boot, network discovery, install, and
> the first setup pages. This QUICKSTART is the same flow in
> human-readable form, for when you want to read it yourself.

This guide assumes you have the hardware from [README.md](README.md#hardware)
in front of you, a laptop or desktop on your home Wi-Fi, and a
2.4 GHz Wi-Fi network the Pi can join. Put the Pi on the **same
Wi-Fi network as your computer** during setup; the laptop talks to
it by local hostname.

---

## Before you start: two names to remember

The recommended first hostname is `jts`. If you use that, the Pi's
local network name will be:

```sh
jts.local
```

If you choose a different hostname in Raspberry Pi Imager, carry that
name forward everywhere. For example, if you type `jts3`, then later
commands and browser URLs use:

```sh
jts3.local
```

The examples below use `jts.local`. Substitute your own
`<hostname>.local` if you picked something else.

---

## Before you start: Raspberry Pi Imager

Use **Raspberry Pi Imager 2.0.6 or later** from
[raspberrypi.com/software](https://www.raspberrypi.com/software/).
Check the installed version from **Raspberry Pi Imager -> About**.

Older 2.0.x releases had a Trixie customization bug when public-key
SSH was selected. The beginner path below uses password-based SSH
instead, but installing the current Imager keeps the setup boring in
the best way.

---

## 1. Flash the Pi (5 minutes)

1. Insert a microSD card (16 GB+) into your computer.
2. Open Raspberry Pi Imager and choose:
   - **Device**: Raspberry Pi 5
   - **OS**:
     **Raspberry Pi OS -> Raspberry Pi OS (Other) -> Raspberry Pi OS Lite (64-bit)**
   - **Storage**: your SD card
3. Imager asks if you want to customise the OS. Choose **Edit
   Settings** or **Yes**.
4. Step through the customization screens:
   - **Hostname**: type `jts` for your first speaker, or another
     simple name like `jts3`, `kitchen`, or `bedroom`. If you pick
     `jts3`, remember that the address later is `jts3.local`.
   - **Localisation**: pick your **Capital city**, then confirm the
     **Time zone** and **Keyboard layout** that Imager fills in.
   - **User**: username `pi`, then choose and confirm a password.
     Write this password down for the next step. The beginner path
     assumes username `pi`; custom users via `--user` / `PI_USER` are
     advanced and currently supported for onboarding/deploy only.
     Passwordless sudo is optional for unattended deploys; if you leave
     it off, the deploy script can prompt for the sudo password through
     SSH when needed.
   - **Wi-Fi**: select the same Wi-Fi network your computer is on.
     Enter the Wi-Fi password. Leave hidden-network options alone
     unless your network is actually hidden.
   - **Remote Access / SSH**: turn SSH on and choose
     **password authentication**. Public-key authentication is an
     advanced option; you do not need it in Imager for the beginner
     path.
   - **Raspberry Pi Connect**: leave it off. JTS does not use it.
   - **Interfaces & Features**: leave the defaults. JTS configures
     the hardware features it needs during install.
5. Save the settings and write the card.
6. When Imager finishes, it usually auto-ejects the SD card. Physically
   remove it from your computer and put it into the Pi.

If you ever need to inspect the card's `bootfs` partition after Imager
finishes, physically remove and reinsert the SD card into your computer.
Auto-eject means the files may not be visible until you do.

---

## 2. Boot the Pi (3 minutes)

1. Insert the SD card into the Pi.
2. Connect the Apple USB-C dongle, ReSpeaker XVF3800, amp, and
   speakers. See [BRINGUP.md](BRINGUP.md) Phase 1 for the full
   hardware connections.
3. Power on the Pi. It should join Wi-Fi and start SSH in 45-90
   seconds.
4. From your computer, check that it is reachable:

   ```sh
   ping jts.local
   ```

   If you chose `jts3` as the hostname, run `ping jts3.local`
   instead. If it does not respond, jump to the
   [failure ladder](#failure-ladder).

---

## 3. Onboard (15-20 minutes)

Clone the repo and run the onboarder from your computer:

```sh
git clone https://github.com/jaspercurry/JTS.git
cd JTS
bash scripts/onboard.sh jts.local --adopt
```

Use the hostname you chose in Imager. For example:

```sh
bash scripts/onboard.sh jts3.local --adopt
```

`--adopt` is the normal beginner path. It asks for the Pi password
once, installs your laptop's SSH key for future deploys, writes this
checkout's local target files, then installs JTS.

If the script says your computer does not have an SSH key yet, run
the command it prints:

```sh
ssh-keygen -t ed25519 -C "$(whoami)@$(hostname -s)-jts"
```

Then rerun the same onboard command with `--adopt`. You do **not**
need to re-flash the SD card or paste this key into Raspberry Pi
Imager.

The onboarder will:

1. **probe** — verify the Pi is reachable on the local network
2. **adopt** — ask for the Pi password once and install your laptop's
   SSH key
3. **persist** — write `~/.ssh/config`, `.env.local`, and
   `CLAUDE.local.md` so this checkout remembers the chosen Pi. If you
   connect by IP, it records the Pi's hostname separately so the
   speaker identity does not become an IP address.
4. **install** — copy the repo to the Pi's `$HOME/jts` staging directory
   and run `deploy/install.sh`, with a sudo preflight before upload.
   Passwordless sudo runs unattended; otherwise an interactive terminal
   prompts through SSH. The Pi consumes the staged source tree and
   pinned/hash-checked source archives; it does not need `git` for the
   normal install path.
5. **validate** — run `jasper-doctor` and show the result

When it finishes, you'll see a banner with the next URLs to visit.

> [!NOTE]
> Advanced SSH-key path: if you already know how to put your public
> key into Raspberry Pi Imager, you can choose public-key SSH there
> and run `bash scripts/onboard.sh jts.local` without `--adopt`.
> For first-time setup, password SSH plus `--adopt` is simpler.

---

## 4. Configure (10 minutes, one-time)

Visit these pages from any device on the same Wi-Fi. Replace
`jts.local` with your chosen hostname if needed.

- **`http://jts.local/voice/`** — required. Pick a voice provider
  (Gemini / OpenAI / Grok) and paste an API key. The speaker will
  not respond to "Hey Jarvis" until this is done.
- **`http://jts.local/transit/`** — optional. NYC subway / bus /
  Citi Bike. Geocode your address; pick stops.
- **`http://jts.local/spotify/`** — optional. Connect a Spotify
  account so "play Taylor Swift" works without your phone.
- **`http://jts.local/speaker/`** — optional. Rename the speaker as it
  appears in AirPlay, Spotify Connect, Bluetooth, and USB Audio pickers.
- **`http://jts.local/bluetooth/`** — optional. Open a five-minute
  no-code pairing window for phones and Bluetooth accessories.
- **`http://jts.local/system/`** — the dashboard. Status, dial
  onboarding, mic-mute, software version, Wi-Fi.

---

## What you have now

A speaker that:
- Plays music from any device that supports AirPlay 2, Spotify
  Connect, or Bluetooth A2DP.
- Listens for "Hey Jarvis" and answers via the LLM provider you
  picked.
- Has every wizard URL persisted across reboots.

Future Claude Code sessions in this checkout will automatically
read `CLAUDE.local.md` and know which Pi you're targeting — no
re-onboarding needed.

---

## Failure ladder

When something goes wrong, work down the symptom that matches what
you actually observed.

### "I can't reach `<hostname>.local`"

1. **Use the hostname you chose.** If you typed `jts3` in Imager,
   the address is `jts3.local`, not `jts.local`.
2. **Same Wi-Fi.** Confirm your computer is on the same Wi-Fi network
   you entered in Imager. Guest networks and phone hotspots often
   block local-device discovery.
3. **Wait two minutes.** The first boot can take longer than a normal
   boot while Raspberry Pi OS expands the filesystem.
4. **Pi Imager version.** Run **Raspberry Pi Imager -> About**. If it
   is older than 2.0.6, upgrade Imager, re-flash, and try again.
5. **Find the Pi's IP from your router.** Most home routers have an
   admin page (often `http://192.168.1.1`) that lists connected
   devices. Look for `jts`, `jts3`, or whatever hostname you set.
   Re-run with the IP:

   ```sh
   bash scripts/onboard.sh 192.168.1.42 --adopt
   ```

   The script queries the Pi's hostname and records that as
   `JASPER_HOSTNAME`. If you already know the intended speaker name,
   make it explicit:

   ```sh
   bash scripts/onboard.sh 192.168.1.42 --adopt --speaker-hostname jts.local
   ```

6. **ARP scan for Pi MAC OUIs** from your computer:

   ```sh
   arp -a | grep -iE 'b8:27:eb|d8:3a:dd|dc:a6:32|2c:cf:67'
   ```

7. **USB-C gadget rescue** (Pi 5, when Wi-Fi has not come up at all):
   - Power the Pi from the **GPIO 5V/GND header**, not the USB-C
     port. The USB-C port has to be free for the data connection.
   - Connect a **USB-A -> USB-C** cable from your computer to the Pi's
     USB-C port. USB-C to USB-C cables hit an open kernel bug
     ([raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289))
     and do not work reliably.
   - On Apple Silicon Macs there is a known USB-PD interaction
     ([raspberrypi/linux#6569](https://github.com/raspberrypi/linux/issues/6569))
     that can break detection; try a Linux laptop if available.
   - Pi appears at `10.12.194.1`:

     ```sh
     bash scripts/onboard.sh 10.12.194.1 --adopt
     ```

### "Onboarding says no SSH key was found"

The beginner path still creates a laptop SSH key for future deploys;
it just does that after the Pi is already on the network. Run:

```sh
ssh-keygen -t ed25519 -C "$(whoami)@$(hostname -s)-jts"
```

Then rerun:

```sh
bash scripts/onboard.sh jts.local --adopt
```

That runs `ssh-copy-id` once (you'll type the password) and then
uses pubkey auth for everything after. If passwordless sudo is not
enabled, the later deploy step prompts for the sudo password through
an interactive SSH session; it is not stored.

Use your chosen hostname in place of `jts.local`.

### "Ping works but SSH or adoption fails"

Check the basics first:

1. The username in Imager should be `pi`.
2. The password is the one you typed in Imager's User screen.
3. SSH must be enabled in Imager with password authentication.

Then rerun:

```sh
bash scripts/onboard.sh jts.local --adopt
```

If you chose public-key authentication in Imager instead, omit
`--adopt`:

```sh
bash scripts/onboard.sh jts.local
```

### "Imager said it customized the OS but the Pi acts unconfigured"

This usually means either the wrong SD card was written, the Pi joined
a different network, or an older Imager build hit a customization bug.
Use your router's connected-device list to look for either your chosen
hostname or `raspberrypi`.

If you find an IP, onboard with:

```sh
bash scripts/onboard.sh 192.168.1.42 --adopt
```

Because the hostname may still be `raspberrypi`, either pass the
intended identity during onboarding:

```sh
bash scripts/onboard.sh 192.168.1.42 --adopt --speaker-hostname jts.local
```

or rename the Pi after onboarding:

```sh
ssh pi@192.168.1.42 'sudo hostnamectl set-hostname jts && sudo reboot'
```

After the reboot, substitute the hostname you chose, such as
`jts.local` or `jts3.local`.

### "deploy says non-interactive sudo failed"

SSH worked, but the Pi user needs a password for sudo and the deploy
is not attached to an interactive terminal. Nothing was rsynced yet.

Run the same command from a terminal so it can prompt through SSH, or
enable passwordless sudo deliberately for that user and rerun. JTS does
not install broad sudoers rules for you.

### "install.sh failed partway through"

Re-run the same command. `install.sh` is idempotent — already-done
steps are detected and skipped.

If it keeps failing on the same step, pull the journal to see what
went wrong:

```sh
bash scripts/fetch-pi-logs.sh
ls logs/   # most recent journal is symlinked as *-latest.log
```

### "Onboarding succeeded but `jasper-doctor` shows warnings"

Open `http://jts.local/system/` — or your chosen hostname's `/system/`
page. The dashboard surfaces the same checks with more context and
remediation links. Common warnings:

- **XVF firmware is 2-channel** — software AEC stays off until you
  DFU-flash 6-channel firmware. See [BRINGUP.md](BRINGUP.md) Phase 2A.5.
- **Voice provider not configured** — visit `/voice/` and paste an
  API key.
- **Apple USB-C dongle not detected** — check that headphones (or
  the amp's input) are physically connected. The dongle is a smart
  cable and refuses to enumerate as a USB audio class device unless
  something is plugged into its analog jack.

---

## Multi-Pi households

A second speaker is just a second checkout with its own `.env.local`.
Worktrees work fine too.

```sh
# In a fresh terminal:
cd ~/Code
git clone https://github.com/jaspercurry/JTS.git JTS-kitchen
cd JTS-kitchen
bash scripts/onboard.sh jts2.local --adopt
```

That checkout's `.env.local` will point at `jts2.local`. Switching
between Pis is `cd` between the two checkouts. The `~/.ssh/config`
aliases (`ssh jts`, `ssh jts2`) work from anywhere.

If you'd rather keep a single checkout and flip the active target,
use the `scripts/use` helper after both Pis have been onboarded at
least once:

```sh
bash scripts/use jts2.local      # flip this checkout to jts2
bash scripts/use jts.local       # flip back
```

That just rewrites `.env.local` and `CLAUDE.local.md` — no
re-install, no SSH activity.

Inside Claude Code, each checkout gets its own `CLAUDE.local.md`
loaded into context automatically, so a Claude session in the
kitchen checkout knows it targets `jts2.local`, and a session in
the living-room checkout knows it targets `jts.local`.

---

## FAQ

**Why password SSH in Imager?**

It matches what a first-time Raspberry Pi Imager user sees: type a
username, type a password, turn SSH on. The JTS onboarder then uses
`--adopt` to install your laptop SSH key after the Pi boots. You type
the password once; future deploys use the key.

Public-key SSH in Imager is still fine for advanced users who already
know where their public key lives. It is no longer the beginner path.

**What about passwordless sudo?**

Do not worry about it during the beginner Imager flow. Use the normal
Raspberry Pi OS user setup, keep the password you chose, and let the
onboarding script tell you if the image needs anything unusual.

**What about a custom JTS-branded OS image?**

Intentionally not shipped. Custom images carry a maintenance tax
(firmware version drift, kernel security fixes, etc.) and the
quickstart works from stock Raspberry Pi OS Lite + `install.sh`.

**Does this work without Claude Code?**

Yes. Every command above is plain bash. Claude Code just gives
you a friendlier surface when something breaks (it can read the
logs, explain the failure, suggest the next step). The shell
script does the actual work.

**Where does Pi-side state live?**

`/etc/jasper/jasper.env` (operator-set), `/var/lib/jasper/*.env`
(wizard-owned). The Pi-side single source of truth for "what
hostname am I" is `JASPER_HOSTNAME`. See
[AGENTS.md](AGENTS.md#speaker-hostname--single-source-of-truth).

**Where does laptop-side state live?**

`.env.local` and `CLAUDE.local.md` in each repo checkout, both
gitignored, both written by `scripts/onboard.sh`. `.env.local` records
`PI_HOST` as the SSH target and `JASPER_HOSTNAME` as the speaker's
hostname/cert identity, which may differ when you connect by IP. See
[AGENTS.md](AGENTS.md#laptop-side-state--envlocal-and-claudelocalmd).

---

When something breaks that isn't in the failure ladder, the
[docs/](docs/) directory has subsystem deep-dives (look for files
named `HANDOFF-*.md`). The full bringup walkthrough — including
the XVF firmware DFU flash, dial onboarding, satellite mic setup —
is in [BRINGUP.md](BRINGUP.md).
