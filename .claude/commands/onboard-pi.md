---
description: |
  Set up a JTS smart speaker on a Raspberry Pi from scratch — the
  complete journey from "I have hardware in front of me" through
  "Hey Jarvis works." Use whenever the user says any natural-language
  variant of: "set this up", "install JTS", "set up a Pi", "I bought
  a new Pi", "help me get started", "flash my SD card", "I have a
  new speaker", or mentions Raspberry Pi Imager. Also handles
  multi-Pi households — if there's already a JTS speaker on the
  LAN, probes for it before the user picks a hostname in Imager
  and suggests a non-colliding alternative.
---

# Onboard a JTS Raspberry Pi speaker

You are walking a user through setting up a JTS smart speaker. The
user may be at any point in the journey — already-flashed Pi, brand
new Pi in the box, or still shopping for hardware. Your job is to
**figure out where they are** (Phase 0), then drive the remaining
phases interactively.

## Interaction discipline

- **One question per turn.** Never dump a multi-step checklist at the
  user. After each step, confirm before advancing.
- **Detect before asking.** If you can check something programmatically
  (which OS, is Imager installed, is the Pi reachable, are there other
  speakers on the LAN), do it. Don't ask the user what you can verify.
- **Front-load anti-patterns.** Several Pi Imager + Trixie footguns
  are listed in "Things to warn about" at the bottom — surface the
  relevant ones at the right phase, before the user can hit them.
- **One source of truth.** Read `QUICKSTART.md` for the canonical
  human-facing steps. This skill is the conversational driver; the
  shell script is the deterministic executor. Don't reinvent either.

---

## Phase 0 — Where is the user?

Before asking anything, run these in parallel to figure out the situation:

```sh
# 1. Are there existing JTS speakers on the LAN? (multi-Pi case)
dns-sd -B _jasper-control._tcp 2>/dev/null & sleep 3 && kill $! 2>/dev/null
# OR on Linux:
avahi-browse -tr _jasper-control._tcp 2>/dev/null

# 2. Is Pi Imager installed on the user's laptop?
ls "/Applications/Raspberry Pi Imager.app" 2>/dev/null   # macOS
which rpi-imager 2>/dev/null                              # Linux

# 3. Does the user have an SSH keypair?
ls ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub ~/.ssh/id_ecdsa.pub 2>/dev/null

# 4. Is anything already at the default hostname?
ping -c 1 -W 2 jts.local 2>/dev/null
```

Then ask the user one question:

> "I'm helping you set up a JTS speaker. Where are you in the process —
> do you already have the hardware in front of you and Pi Imager installed,
> or are you starting from scratch?"

Adapt the rest of the flow to their answer. If you found existing
speakers in Phase 0.1, mention that proactively: *"I see one JTS
speaker already on your network (`jts.local`). Are you adding a
second speaker, or replacing the existing one?"*

---

## Phase 1 — Hardware checklist

Ask the user, one or two items at a time, whether they have:

1. Raspberry Pi 5 (2 GB recommended)
2. microSD card (16 GB+)
3. Pi 5 power supply
4. Apple USB-C → 3.5mm dongle
5. Seeed ReSpeaker XVF3800 (USB-UA variant)
6. TPA3255 amp + its own 32 V power supply
7. Speakers + speaker wire

Full BOM in [README.md § Hardware](../../README.md#hardware). If anything
is missing, don't block — tell them to come back to `/onboard-pi`
when hardware arrives. Nothing is persisted yet; the skill picks
up cleanly later.

---

## Phase 1.5 — Assembly check

Ask: *"Is everything wired up — Pi powered, amp powered (its own
32 V supply), speakers connected to the amp, Apple dongle in the
Pi with its 3.5mm into the amp's RCA input, ReSpeaker plugged into
the Pi?"*

If yes: proceed.
If no or unsure: point them at [BRINGUP.md](../../BRINGUP.md) Phase 1 for
the wiring diagram.

---

## Phase 2 — Pi Imager

**Required version: 2.0.6 or later.** Earlier 2.0.x releases have an
[open bug ([rpi-imager#1320](https://github.com/raspberrypi/rpi-imager/issues/1320))]
where selecting public-key auth silently breaks all OS customization
on the Trixie image. Surface this warning before they download.

If Imager isn't installed (Phase 0 check), tell them to download from
[raspberrypi.com/software](https://www.raspberrypi.com/software/) and
confirm when open. If installed, ask them to verify via
`Raspberry Pi Imager → About`.

Walk them through the wizard, **one step per turn**. (Imager 2.0 is a
multi-step wizard — each customisation step below is its own full
screen, not a tab.)

1. **Device** → Raspberry Pi 5
2. **OS** → Raspberry Pi OS Lite (64-bit), Trixie
3. **Storage** → their SD card. Imager prompts about customisation —
   say yes, edit settings.
4. **Customisation → Hostname**: pick a name (default `jts`).
   **If Phase 0 found an existing JTS speaker**, suggest a non-
   colliding alternative (`jts2`, `kitchen`, `bedroom`). Don't let
   them pick a name already in use — Avahi will silently
   suffix-resolve to `<name>-2.local` and break URL discovery.
5. **Customisation → Localisation**: three dropdowns —
   **Capital city** (this also sets the WiFi country, so pick one in
   their actual region), **Time zone** (auto-fills from city, confirm
   or override), **Keyboard layout** (auto-fills, confirm).
6. **Customisation → User**: **Username** `pi` (JTS's beginner path
   defaults to this), **Password** (any — it's a fallback; SSH uses
   pubkey), **Confirm password**. Recommend ✅ **"Enable passwordless
   sudo"** for unattended deploys. If they leave it unchecked, the
   deploy script can still prompt for the sudo password through an
   interactive SSH session and will not store it. Do not suggest a
   custom username to beginners: `--user` / `PI_USER` is advanced and
   currently supported for onboarding/deploy only; some diagnostics may
   still assume `pi` or `/home/pi`.
7. **Customisation → Wi-Fi**: leave "Secure network" selected.
   **SSID** (auto-detected from the laptop's WiFi if available),
   **Password**, **Confirm**. Leave "Hidden SSID" unchecked.
8. **Customisation → Remote Access (SSH)**: **Enable SSH** ON,
   pick **"Use public key authentication"**, then in the SSH Key
   Manager either paste the contents of `~/.ssh/id_ed25519.pub` or
   click **Browse** and select the file. Imager 2.0.x does NOT
   auto-import — keys must be added explicitly. If they don't have
   a pubkey yet (Phase 0 check), generate one first:
   `ssh-keygen -t ed25519 -C "$(whoami)@$(hostname -s)-jts"`.
9. **Skip** Raspberry Pi Connect (next step) — not needed for JTS.
10. **Skip** Interfaces & Features — JTS configures I2C/SPI itself;
    don't enable USB Gadget Mode here (that's a separate rescue
    feature documented elsewhere).
11. **Save → Yes → Yes**. Confirm when flashing is done.

---

## Phase 3 — Flash, eject, boot

Ask the user, after Imager finishes:

> "Imager done? Please: eject the SD card from your laptop, insert it
> into the Pi (the slot is on the underside, opposite the USB ports),
> connect the audio peripherals (Apple dongle, ReSpeaker mic), and
> power the Pi on. Then let me know — I'll watch for it to come up."

After they confirm "powered on", **poll for reachability** rather
than asking again:

```sh
# Repeat until reachable or ~120 s elapsed
for i in $(seq 1 24); do
    if ping -c 1 -W 2 <hostname>.local >/dev/null 2>&1; then
        echo "reachable after ~$((i*5))s"
        break
    fi
    sleep 5
done
```

If still not reachable after 2 minutes, walk them through the failure
ladder from `QUICKSTART.md` "I can't reach `<hostname>.local`" (router
admin page → ARP scan → USB-C gadget rescue). Don't pre-emptively
recite all four rungs; check them one at a time.

---

## Phase 4 — Onboard

Once the Pi answers ping, run the onboarder:

```sh
bash scripts/onboard.sh <hostname>.local
```

If you've already established the user picked password auth in Imager
(or if they're adopting an existing Pi that doesn't have their key),
pass `--adopt`:

```sh
bash scripts/onboard.sh <hostname>.local --adopt
```

If you only found the Pi by IP, prefer an explicit speaker identity:

```sh
bash scripts/onboard.sh <ip-address> --adopt --speaker-hostname <hostname>.local
```

Without `--speaker-hostname`, the script queries the Pi's hostname and
uses that for `JASPER_HOSTNAME`; it must never use the IP address as
the speaker identity/certificate name.

Stream the output. The script emits both `==>` headers (human-readable
phase milestones) and `event=onboard.<phase> status=<s>` lines
(parseable; same convention as the Pi-side daemons). Expect 15-20
minutes — the long pole is shairport-sync compiling from source on
the Pi. Don't ask the user to wait silently; let them know what's
happening at each phase.

If a phase fails, the script prints a detailed remediation block.
Surface it verbatim to the user; don't paraphrase.

After success, the script writes `.env.local`, `CLAUDE.local.md`, and
an `~/.ssh/config` Host alias automatically.

---

## Phase 5 — Configure

The script's success banner lists the post-install wizard URLs. Walk
the user through them, one at a time:

1. **`http://<hostname>.local/voice/`** — required. Pick a voice
   provider (Gemini is the cheapest at ~$0.025/min; OpenAI Realtime
   is best quality at ~$0.30/min; Grok is the middle option). Paste
   an API key. The speaker won't respond to "Hey Jarvis" until this
   is set.
2. **`http://<hostname>.local/transit/`** — optional. NYC subway / bus
   / Citi Bike. Skip if they're not in NYC.
3. **`http://<hostname>.local/spotify/`** — optional. Connect Spotify
   so "play Taylor Swift" works without phone interaction.
4. **`http://<hostname>.local/system/`** — the dashboard. Show them
   where status, dial onboarding, mic-mute live.

Tell them they're done after Step 1 (voice provider). The rest can
happen anytime later.

**If `jasper-doctor` warned about XVF firmware**: speaker works fine;
AEC is just off. To enable, walk through [BRINGUP.md](../../BRINGUP.md)
Phase 2A.5 later. Not a blocker.

---

## Things to warn about (anti-patterns)

Surface these at the right phase, before the user can hit them:

- **Imager 2.0.0-2.0.5 + Trixie**: silently breaks customization when
  pubkey is selected ([rpi-imager#1320](https://github.com/raspberrypi/rpi-imager/issues/1320)).
  Required version is 2.0.6+. Warn before they download.
- **Hostname collision**: if Phase 0 found an existing speaker, NEVER
  let the user pick that hostname again. Avahi silently suffix-resolves
  and breaks URL discovery. Suggest `jts2`, `kitchen`, etc.
- **Password in Imager**: works, but requires `--adopt` later to install
  a pubkey. Passwordless sudo is optional for friendly interactive
  setup; it is required only for unattended deploys. The scripts
  intentionally do not install broad sudoers rules.
- **USB-C → USB-C cables for gadget-mode rescue**: hit kernel bug
  [raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289).
  Use USB-A → USB-C only. Power the Pi from the GPIO 5V/GND header,
  not the USB-C port, in gadget mode.
- **Apple dongle won't enumerate without analog plug**: the Apple USB-C
  → 3.5mm dongle is a smart cable. If headphones (or the amp's input)
  aren't physically connected, the dongle refuses to enumerate as a
  USB audio class device. Doctor catches this but it's worth a heads-up.
- **Editing `wpa_supplicant.conf` directly**: don't. Use Pi Imager's
  WiFi field, or the `/wifi/` wizard post-install.
- **Bulk-dumping QUICKSTART**: don't. QUICKSTART is a reference for
  humans; this skill is the interactive driver. Read QUICKSTART
  yourself to fill in detail, but walk the user through one step at
  a time.

---

## Multi-Pi households

The Phase 0 LAN probe handles this. If the user has an existing
speaker:

1. Suggest a unique hostname during Phase 2 (Pi Imager field).
2. The new checkout's `.env.local` and `CLAUDE.local.md` will
   automatically target the new hostname after `onboard.sh`.
3. To switch this checkout's active target between speakers later
   without re-installing: `bash scripts/use <hostname>.local`
   (`kubectl config use-context`-style).

If the user runs `onboard.sh` from a fresh checkout per Pi, they get
one `.env.local` per checkout — clean separation, no fleet manifest
needed.

---

## After success

Confirm one last time:
- They can hit `http://<hostname>.local/voice/` from their phone/laptop.
- They've pasted an API key for at least one voice provider.
- They've heard the speaker respond to "Hey Jarvis" with a brief
  greeting (or whatever the model decides to say) at least once.

Tell them they're done. Mention that future Claude Code sessions in
this checkout will automatically know which Pi is active because of
`CLAUDE.local.md`. They don't need to re-onboard, and they don't need
to remember the hostname — just `cd` into the checkout and `ssh
<alias>` works.
