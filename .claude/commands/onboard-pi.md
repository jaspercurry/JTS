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

Walk the user through what they need. **Ask explicitly, one or two
items at a time.** Don't dump the whole BOM at once — confirm what
they have as you go.

> "Before we flash anything, let me check what hardware you have in
> front of you. I'll go through each piece — just answer yes/no or
> tell me what model you got. The full BOM with costs is in
> [README.md § Hardware](README.md#hardware); I'll quote the essentials."

The items to confirm, in roughly this order:

1. **Raspberry Pi 5** — 1 GB or 2 GB model. 2 GB strongly recommended
   (the AEC pipeline gets tight on 1 GB). Note: NOT a Pi 4 — Pi 5 has
   a different USB controller, kernel, and we've only tested on Pi 5.
2. **microSD card** — 16 GB or larger, any modern brand
   (SanDisk Ultra, Samsung Evo, Kingston Canvas, etc.). 32 GB+ is
   slightly more comfortable but 16 GB works.
3. **Official Raspberry Pi 5 power supply** — the 27W USB-C one
   (5.1 V / 5 A). Pi 5 needs more current than older Pi PSUs deliver
   reliably; an off-brand 3 A supply can cause undervoltage warnings.
4. **Apple USB-C → 3.5mm dongle** — ~$9 from Apple. This is the DAC.
   **NOT optional**, and don't substitute — CamillaDSP's config is
   tuned to this specific dongle's 48 kHz UAC2 profile. (Other USB
   dongles work as audio out but you'd need to re-tune the DSP.)
5. **Seeed ReSpeaker XVF3800, USB-UA variant** — ~$70 from
   [Seeed Studio](https://www.seeedstudio.com/). The USB-UA variant
   specifically (4 mics + onboard XMOS DSP). NOT the USB-A, USB-2 mic,
   or Mini variants.
6. **TPA3255 class-D amp board** — any reputable seller (Amazon /
   AliExpress / Parts Express); ~$25-40. The exact PCB layout
   doesn't matter; any TPA3255-based board with RCA input works.
7. **32 V power supply for the amp** — at least 5 A. Mean Well
   GST60A32 (~$25) is the canonical choice. **Separate** from the
   Pi's PSU; the amp gets its own brick.
8. **Speakers + speaker wire** — bookshelf or similar, 4-8 ohm, any
   driver. The user may already have these.

**If the user is missing items**: don't block. Tally what they need
with the cost ballpark, and tell them to come back to `/onboard-pi`
when hardware arrives. Nothing is persisted yet at this point, so
re-invoking the skill later is clean.

**If they have everything**: confirm with one sentence and proceed
to Phase 1.5.

---

## Phase 1.5 — Assembly check

Ask: *"Is everything wired up — amp powered from its 32 V supply,
speakers connected to the amp's terminals, Pi's USB-A port to the
Apple dongle, dongle's 3.5mm to the amp's RCA input (via a 3.5mm-to-RCA
cable), and the ReSpeaker plugged into another Pi USB-A port?"*

If **yes**: confirm, move to Phase 2.

If **no or unsure**: walk them through the minimum-viable wiring,
one connection at a time. The list:

- Pi USB-C port ← official Pi 5 PSU
- Pi USB-A port → Apple USB-C-to-3.5mm dongle (any of the Pi's USB-A ports)
- Apple dongle's 3.5mm output → amp's RCA-L input (3.5mm-to-RCA cable)
- Amp's barrel jack ← 32 V PSU
- Amp's speaker terminals → speaker wire → speakers (mind +/− polarity)
- Pi USB-A port (the other one) → ReSpeaker XVF3800
- microSD card slot: empty for now — Phase 3 flashes the card

The full wiring diagram is in [BRINGUP.md](BRINGUP.md) Phase 1, with
diagrams. Reference it if the user wants more detail.

**Photo guidance**: if the user has hardware in front of them and is
unsure where something plugs in, they can paste a photo into Claude
Code and you can identify components and point to which port goes
where. This is genuinely useful and much faster than text-only
descriptions for the wiring step.

Once they confirm assembly is done (or you've walked through it),
proceed to Phase 2.

---

## Phase 2 — Pi Imager

**Required version: 2.0.6 or later.** Earlier 2.0.x releases have an
[open bug ([rpi-imager#1320](https://github.com/raspberrypi/rpi-imager/issues/1320))]
where selecting public-key auth silently breaks all OS customization
on the Trixie image. Surface this warning before they download.

If Imager isn't installed (Phase 0 check), tell them to download from
[raspberrypi.com/software](https://www.raspberrypi.com/software/) and
confirm when open. If installed, ask them to verify the version via
`Raspberry Pi Imager → About` (or `defaults read "/Applications/Raspberry Pi Imager.app/Contents/Info.plist" CFBundleShortVersionString` on macOS, which you can run yourself).

Then walk them through the Imager wizard, **one field per turn**:

1. **Device**: Raspberry Pi 5
2. **OS**: Raspberry Pi OS Lite (64-bit) — Trixie release
3. **Storage**: their SD card
4. **OS customisation → General**:
   - **Hostname**: pick one. Default `jts`.
     - **If you found an existing JTS speaker in Phase 0**, propose
       a non-colliding alternative: *"You already have `jts.local` —
       what should we name this one? Common choices: `jts2`,
       `kitchen`, `bedroom`, `livingroom`."* Don't let them pick a
       name that's already taken — Avahi will silently suffix-resolve
       to `jts-2.local` and break URL discovery.
   - **Username**: `pi`
   - **Password**: any (fallback only; pubkey is the primary path)
   - **Wireless LAN**: their 2.4 GHz SSID + password + country
   - **Locale**: timezone + keyboard
5. **OS customisation → Services**:
   - **Enable SSH**: yes
   - **Use public-key authentication** (NOT password). If they don't
     have a pubkey (Phase 0 check showed nothing), generate one
     first: `ssh-keygen -t ed25519 -C "$(whoami)@$(hostname -s)-jts"`.
     Then they can paste `~/.ssh/id_ed25519.pub` into Imager.
6. **Save → Yes → Yes**. Confirm with the user that flashing is done.

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

**If `jasper-doctor` warned about XVF firmware**: the speaker WILL
work without it — the chip ships on 2-channel firmware which gives
beamforming + noise suppression but no echo cancellation. Wake-word
detection is just less reliable when music is loud. To enable
software AEC (recommended after the speaker is otherwise working),
walk through the DFU firmware flash in [BRINGUP.md](BRINGUP.md)
Phase 2A.5 — it's a one-time `dfu-util` flash that takes ~5 minutes
including the jumper / button-hold dance to enter DFU mode. Mention
this as a follow-up, not a blocker for "your speaker is working."

---

## Things to warn about (anti-patterns)

Surface these at the right phase, before the user can hit them:

- **Imager 2.0.0-2.0.5 + Trixie**: silently breaks customization when
  pubkey is selected ([rpi-imager#1320](https://github.com/raspberrypi/rpi-imager/issues/1320)).
  Required version is 2.0.6+. Warn before they download.
- **Hostname collision**: if Phase 0 found an existing speaker, NEVER
  let the user pick that hostname again. Avahi silently suffix-resolves
  and breaks URL discovery. Suggest `jts2`, `kitchen`, etc.
- **Password in Imager**: works but requires `--adopt` later, which
  exposes the password to `ssh-copy-id`. Pubkey is the canonical
  path; only fall back to password if the user pushes back on
  pubkey setup.
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
