# JTS QUICKSTART

From "I just bought a Pi" to "Hey Jarvis, play Taylor Swift" in
about 30 minutes — most of which is the speaker building software
by itself while you do something else.

> [!TIP]
> **Using Claude Code?** Skip this doc and just say *"set up a Pi"*
> (or similar). Claude auto-invokes the [`/onboard-pi`](.claude/commands/onboard-pi.md)
> skill and walks you through every step interactively — including
> proactively probing for existing speakers on your network and
> suggesting a non-colliding hostname before you pick one in Imager.
> This QUICKSTART is the same flow in human-readable form, for when
> you want to read it yourself.

This guide assumes you have the hardware from [README.md](README.md#hardware)
in front of you, a laptop with [Claude Code](https://claude.com/claude-code)
installed (the rest of the flow works without it — Claude Code just
makes failures friendlier), and a 2.4 GHz home WiFi network the Pi
can join.

---

## Before you start: tool versions

> [!IMPORTANT]
> **Raspberry Pi Imager 2.0.6 or later** is required. Earlier
> 2.0.x releases have an [open bug ([rpi-imager#1320](https://github.com/raspberrypi/rpi-imager/issues/1320))]
> where selecting "Use public-key authentication" silently breaks
> hostname, WiFi, and locale customization on Trixie images — the
> Pi boots into a graphical first-boot wizard expecting a keyboard
> and monitor, not an SSH client.
>
> Download from [raspberrypi.com/software](https://www.raspberrypi.com/software/).
> Check installed version: `Raspberry Pi Imager → About`.

You also need a local SSH keypair. Check by running:

```sh
ls ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub 2>/dev/null
```

If neither file shows, generate one:

```sh
ssh-keygen -t ed25519 -C "$(whoami)@$(hostname -s)-jts"
```

---

## 1. Flash the Pi (5 minutes)

1. Insert a microSD card (16 GB+) into your laptop.
2. Open Raspberry Pi Imager. **Verify it's 2.0.6 or later** (Imager →
   About). Earlier 2.0.x releases have an [open bug](https://github.com/raspberrypi/rpi-imager/issues/1320)
   that silently breaks customization on Trixie.
3. Step through the wizard:
   - **Device**: Raspberry Pi 5
   - **OS**: Raspberry Pi OS Lite (64-bit), Trixie
   - **Storage**: your SD card
4. Imager asks if you want to customise — say yes. You'll see a
   multi-step wizard (one screen per step):
   - **Hostname**: pick a name (`jts` for your first speaker; pick a
     different name like `jts2`, `kitchen`, `bedroom` for a second
     speaker — two devices on the same LAN can't share a hostname).
   - **Localisation**: pick your **Capital city** (this also sets the
     WiFi country code). **Time zone** and **Keyboard layout** auto-fill
     from the city — confirm or override.
   - **User**: username `pi`, any password (fallback only — pubkey is
     the primary auth), confirm password, and **check "Enable
     passwordless sudo"**. This is important: `install.sh` runs `sudo`
     over SSH and will hang on a password prompt without it.
   - **Wi-Fi**: leave "Secure network" selected. SSID (auto-detected
     from your laptop's current WiFi if available), password, confirm.
   - **Remote Access (SSH)**: turn SSH on, pick **"Use public key
     authentication"**, then in the SSH Key Manager paste the contents
     of `~/.ssh/id_ed25519.pub` or click **Browse** and select the
     file. Imager 2.0.x doesn't auto-import — you have to add the key
     explicitly.
   - **Skip** the Raspberry Pi Connect and Interfaces & Features steps.
5. **Save → Yes → Yes** to write. ~2 minutes.

---

## 2. Boot the Pi (3 minutes)

1. Eject the SD card. Insert it into the Pi.
2. Connect the Apple USB-C dongle + the ReSpeaker XVF3800 + amp + speakers.
   (See [BRINGUP.md](BRINGUP.md) Phase 1 for the full hardware connections.)
3. Power on. The Pi boots, joins WiFi, and starts SSH in 45-90 seconds.
4. From your laptop, sanity-check that it's reachable:

   ```sh
   ping jts.local        # or whatever hostname you set
   ```

   You should see a response within 5 seconds. If not, jump to the
   [failure ladder](#failure-ladder).

---

## 3. Onboard (15-20 minutes)

Clone the repo and run the onboarder. From a fresh terminal on your laptop:

```sh
git clone https://github.com/jaspercurry/JTS.git
cd JTS
bash scripts/onboard.sh jts.local      # whatever hostname you set
```

That's the whole command. It will:

1. **probe** — verify SSH reachability and pubkey auth
2. **persist** — write `~/.ssh/config` alias, `.env.local`,
   `CLAUDE.local.md` (the last two are gitignored, per-checkout state)
3. **install** — rsync the repo to the Pi and run `deploy/install.sh`,
   which apt-installs deps, source-builds shairport-sync (~12 min,
   the longest single step), webrtc-audio-processing v2.1, and
   wires up every systemd unit
4. **validate** — run `jasper-doctor` and surface the result

When it finishes you'll see a banner with the next URLs to visit.

> [!NOTE]
> If you're using Claude Code, you can also just say *"onboard a
> new Pi at jts.local"* — there's a `/onboard-pi` slash command
> that orchestrates this with friendlier failure handling. The
> shell script is the deterministic primitive; the slash command
> is a thin wrapper around it.

---

## 4. Configure (10 minutes, one-time)

Visit the wizards. None of these require Claude Code or the
laptop — anything on your LAN can hit them:

- **`http://jts.local/voice/`** — pick a voice provider (Gemini /
  OpenAI / Grok) and paste an API key. The speaker won't respond
  to "Hey Jarvis" until this is done.
- **`http://jts.local/transit/`** — optional. NYC subway / bus /
  Citi Bike. Geocode your address; pick stops.
- **`http://jts.local/spotify/`** — optional. Connect a Spotify
  account so "play Taylor Swift" works without your phone.
- **`http://jts.local/system/`** — the dashboard. Status, dial
  onboarding, mic-mute, software version, WiFi.

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

### "I can't reach `jts.local`"

1. **Pi Imager version**. Run **Raspberry Pi Imager → About** —
   if it's older than 2.0.6, your OS customization may not have
   applied. Upgrade Imager, re-flash, retry.
2. **Find the Pi's IP from your router**. Most home routers have an
   admin page (often `http://192.168.1.1`) that lists DHCP leases.
   Look for `raspberrypi` or whatever hostname you set. Re-run with
   the IP:

   ```sh
   bash scripts/onboard.sh 192.168.1.42
   ```

3. **ARP scan for Pi MAC OUIs** from your laptop:

   ```sh
   arp -a | grep -iE 'b8:27:eb|d8:3a:dd|dc:a6:32|2c:cf:67'
   ```

4. **USB-C gadget rescue** (Pi 5, when WiFi has not come up at all):
   - Power the Pi from the **GPIO 5V/GND header**, not the USB-C
     port (the USB-C port has to be free for the data connection).
   - Connect a **USB-A → USB-C** cable from your laptop to the Pi's
     USB-C port. USB-C→USB-C cables hit an open kernel bug
     ([raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289))
     and don't work reliably.
   - On Apple Silicon Macs there's a known USB-PD interaction
     ([raspberrypi/linux#6569](https://github.com/raspberrypi/linux/issues/6569))
     that can break detection; try a Linux laptop if available.
   - Pi appears at `10.12.194.1`:

     ```sh
     bash scripts/onboard.sh 10.12.194.1
     ```

### "Ping works but SSH fails"

You probably picked password auth in Imager (or you're adopting an
existing Pi that doesn't have your pubkey). Re-run with `--adopt`:

```sh
bash scripts/onboard.sh jts.local --adopt
```

That runs `ssh-copy-id` once (you'll type the password) and then
uses pubkey auth for everything after.

### "Imager said it customized the OS but the Pi acts unconfigured"

This is [rpi-imager#1320](https://github.com/raspberrypi/rpi-imager/issues/1320)
— an open bug in Pi Imager 2.0.0-2.0.5 where selecting "Use public-key
authentication" silently disables the rest of OS Customization (hostname,
WiFi, locale revert to defaults; the Pi boots into the first-boot
graphical wizard).

You don't have to re-flash. Find the Pi's IP in your router's admin
page (it'll be the one with hostname `raspberrypi` since the custom
hostname didn't apply), then onboard via `--adopt`:

```sh
bash scripts/onboard.sh 192.168.1.42 --adopt
```

`--adopt` uses `ssh-copy-id` over the default `raspberrypi`/`raspberry`
password, then proceeds normally. After onboarding, you can rename
the Pi:

```sh
ssh pi@192.168.1.42 'sudo hostnamectl set-hostname jts && sudo reboot'
```

After the reboot it's reachable as `jts.local` again.

Long-term fix: upgrade Pi Imager to 2.0.6 or later for future Pis.

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

Open `http://jts.local/system/` — the dashboard surfaces the same
checks with more context and remediation links. Common warnings:

- **XVF firmware is 2-channel** — software AEC stays off until you
  DFU-flash 6-channel firmware. See [BRINGUP.md](BRINGUP.md) Phase 2A.5.
- **Voice provider not configured** — visit `/voice/` and paste an
  API key.
- **Apple USB-C dongle not detected** — check that headphones (or
  the amp's input) are physically connected. The dongle is a
  smart cable and refuses to enumerate as a USB audio class device
  unless something is plugged into its analog jack.

---

## Multi-Pi households

A second speaker is just a second checkout with its own
`.env.local`. Worktrees work fine too.

```sh
# In a fresh terminal:
cd ~/Code
git clone https://github.com/jaspercurry/JTS.git JTS-kitchen
cd JTS-kitchen
bash scripts/onboard.sh jts2.local
```

That checkout's `.env.local` will point at `jts2.local`. Switching
between Pis is `cd` between the two checkouts. The `~/.ssh/config`
aliases (`ssh jts`, `ssh jts2`) work from anywhere.

If you'd rather keep a single checkout and flip the active target,
use the `scripts/use` helper (after both Pis have been onboarded
at least once so the SSH aliases exist):

```sh
bash scripts/use jts2.local      # flip this checkout to jts2
bash scripts/use jts.local       # flip back
```

That just rewrites `.env.local` and `CLAUDE.local.md` — no
re-install, no SSH activity. Like `kubectl config use-context`.

Inside Claude Code, each checkout gets its own `CLAUDE.local.md`
loaded into context automatically — so a Claude session in the
kitchen checkout knows it targets `jts2.local`, and a session in
the living-room checkout knows it targets `jts.local`.

---

## FAQ

**Why pubkey, not password?**

Pi Imager 2.0.6+ supports either — they're a single radio button.
Pubkey is more secure (no password ever crosses the network), and
`ssh-copy-id` (used by `--adopt`) is a fallback path for the
already-flashed-with-password case anyway. The default is pubkey
because that's what the Imager workflow is best at.

**What about a custom JTS-branded OS image?**

Intentionally not shipped. Custom images carry a maintenance tax
(firmware version drift, kernel security fixes, etc.) and the
quickstart works just as well from stock Trixie + `install.sh`.

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
gitignored, both written by `scripts/onboard.sh`. See
[AGENTS.md](AGENTS.md#laptop-side-state--envlocal-and-claudelocalmd).

---

When something breaks that isn't in the failure ladder, the
[docs/](docs/) directory has subsystem deep-dives (look for files
named `HANDOFF-*.md`). The full bringup walkthrough — including
the XVF firmware DFU flash, dial onboarding, satellite mic setup —
is in [BRINGUP.md](BRINGUP.md).
