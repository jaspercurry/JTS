# HANDOFF — Seeed ReSpeaker XVF3800 (XMOS XVF3800 chip)

The XVF3800 is the 4-mic USB array on every JTS speaker. This is the
canonical reference for everything we know about that chip and the
specific Seeed board we use: identity, firmware variants, channel
layout, the full vendor-control parameter space, documented failure
modes, and the diagnostic moves we'd reach for first when something
goes wrong.

It is written to **stand alone** — read this and you should not need
to chase the XMOS PDF or the Seeed wiki. URLs are cited where claims
aren't load-bearing from local code so future sessions can verify if
upstream changes.

Scope note: this doc covers the **USB UA variant** (the board JTS
uses). The chip itself also supports an **I2S/INT-Device** mode with
separate firmware images, and Seeed sells a XIAO-ESP32S3 variant of
the board for that mode. We don't use it, but it's documented here
because diagnostics sometimes need to know "is this even the right
firmware family."

If you came here trying to understand AEC, **read this first then
[HANDOFF-aec.md](HANDOFF-aec.md)** — the AEC investigation assumes
you already know the channel layout and parameter space documented
below.

## Quick lookup

| If you're trying to… | Read |
|---|---|
| Identify the chip / board | §1 Hardware identity |
| Flash a different firmware (DFU) | §2 Firmware variants |
| Understand the 6-channel USB capture layout | §3 Channel layout |
| Set a vendor parameter (gain, NS, AEC) | §4 Parameter space, §6 reference tables |
| Debug a "no audio" / "channel silent" symptom | §5 Failure modes → §7 (ch2-5 silence: resolved root cause at top) → §8 Diagnostic cookbook |
| Control the chip from Python | §9 The library we use |

---

## 1. Hardware identity

### Physical board

| Component | Detail |
|---|---|
| Product (no XIAO) | Seeed Studio reSpeaker XVF3800 USB 4-Mic Array, SKU 101991441 |
| Product (with XIAO ESP32S3) | SKU 114993701 — same chip, adds the XIAO module for I2S/INT mode |
| Main DSP | XMOS XVF3800 ("VocalFusion 4-Mic"), purpose-built voice processor |
| Microphones | 4× PDM MEMS in a circular array (geometry: square, 33 mm radius — see `AEC_MIC_ARRAY_GEO` below) |
| Onboard codec | TI TLV320AIC3104 (drives the 3.5 mm jack + JST speaker connector) — **not** used in the JTS build |
| LEDs | 12× WS2812 individually-addressable RGB ring |
| Buttons | Mute (latching software state) + Reset (hardware reset of the XVF chip) |
| USB | Single USB-C connector (the one "close to the 3.5 mm jack" — this is the XMOS USB-C port; if XIAO is fitted, its USB-C is separate) |
| GPIO | 3 GPI + 5 GPO (see GPIO table in §6.5) — exposed both to the host and to onboard subsystems (mute LED, amp enable, WS2812 power) |
| Speaker outputs | 3.5 mm AUX jack + JST speaker connector (5 W amp). **Both driven by the chip's AIC3104, not the host.** JTS leaves both unconnected and drives the speaker from a separate Apple USB-C → 3.5 mm dongle. |

Source: [Seeed wiki — Getting Started](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/),
specifically the "Main Components" table.

### USB descriptor

| Field | Value |
|---|---|
| Vendor ID (VID) | **0x2886** (Seeed Studio / Seeed Technology) |
| Product ID (PID) | **0x001a** |
| Class | USB Audio Class 2.0 (UAC2) compliant, in normal mode |
| Speed | **High Speed (480 Mb/s) — confirmed required for the 6-channel firmware** (see §5.5) |
| DFU interface | Interface number is platform-dependent: `intf=3` on Linux, `intf=4` on macOS / Windows (per the upstream dfu_guide.md sample outputs) |
| Control interface | Interface 3 (per `host_control/README.md`: *"Found device VID: 10374 PID: 26 interface: 3"*) |
| iSerial format | 18 ASCII digits, format `<SKU 9 digits><device number 9 digits>` — e.g. `101991441000000001`. Stable across reboots (it's burned into the chip during board manufacture). |
| Audio bit-depth (USB endpoint) | Default S16_LE; can be set to 24-bit or 32-bit via the `USB_BIT_DEPTH` parameter (see §6.1) — changing this reboots the chip. Seeed wiki says "16 kHz / 32-bit depth" for both 2-ch and 6-ch USB firmware; in practice `cat /proc/asound/Array/stream0` on JTS shows `S16_LE` after default install. |
| Audio sample rate (USB endpoint) | **16 kHz, fixed.** Not switchable on the USB firmware (issue #6 on the upstream repo asks for 48 kHz support; v2.0.10 attempts to add it but the PR has not been merged). The HA-targeted I2S firmware runs at 48 kHz; the USB firmware does not. |
| Endpoint layout | One ISO IN endpoint (capture, `0x81`), one ISO OUT endpoint (playback, `0x01`), both SYNC mode, 500 µs data packet interval. Channel count differs per firmware variant — see §3. |

Sources: [respeaker upstream dfu_guide.md](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/xmos_firmwares/dfu_guide.md), [host_control README](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md), [Seeed wiki](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/), upstream issue #6.

How to read all of this on a live Pi:

```sh
# VID/PID + bus topology
lsusb -d 2886:001a -v 2>/dev/null | head -40

# Speed (high vs full) + interfaces + endpoint sizes + serial
lsusb -t -v -d 2886:001a 2>/dev/null
cat /sys/bus/usb/devices/*/idVendor | grep -B1 -A1 2886
# … or
udevadm info -a -p /sys/class/sound/card*/ | grep -E 'idVendor|idProduct|speed|serial'

# What ALSA sees once the kernel enumerates it
arecord -l | grep -i array            # the card row
cat /proc/asound/Array/stream0        # capture channel count, format, rate
cat /proc/asound/Array/stream1        # playback equivalent
```

Note on the iSerial: it is exposed both via `lsusb -v` (the
`iSerial` field) and via `dfu-util -l` (the `serial=` field on the
DFU descriptor when the chip is in safe / DFU mode). It is also
fetchable via the chip's i2c control interface — see
"`xvf_i2c_dfu` / `rpi_64bit`" reference in the upstream repo if we
ever need it programmatically.

---

## 2. Firmware variants

Seeed publishes four official firmwares across two interface modes,
plus a recovery "all-FF" blob.

### 2.1 USB firmwares (in `xmos_firmwares/usb/`)

| Filename | Variant | Channels (capture) | Notes |
|---|---|---|---|
| `respeaker_xvf3800_usb_dfu_firmware_v2.0.5.bin` | "2-channel" | 2 | Early production stable. |
| `respeaker_xvf3800_usb_dfu_firmware_v2.0.6.bin` | "2-channel" | 2 | Adds `DOA_VALUE` command, fixes a WS2812 bug, raises DAC output 0 dB → +6 dB. **Has `SAVE_CONFIGURATION` brick hazard — see §5.1.** |
| `respeaker_xvf3800_usb_dfu_firmware_v2.0.7.bin` | "2-channel" | 2 | Adds `LED_RING_COLOR` for per-LED control. |
| `respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin` | **"6-channel"** | **6** | **The one JTS uses.** Adds raw mics on USB-OUT channels 2–5. |
| (PR #11, unmerged) | `usb_dfu_firmware_v2.0.9.bin` | 2 | Adds fixed-beam support and "enhanced device finding" for Windows. Not in `master`. |
| (PR #13, unmerged) | `usb_dfu_firmware_v2.0.10.bin` | 6 @ 48 kHz | The first attempt at 48 kHz 6-channel firmware. Not in `master`; treat as experimental until merged. |

Both 2-ch and 6-ch share **channel 0 = Conference and channel 1 =
ASR** — i.e. the 6-ch firmware is a strict superset of the 2-ch
output. Switching back to 2-ch firmware is reversible without losing
the "what does ch0/1 do" mental model; it just removes ch2-5.

Source: [upstream `xmos_firmwares/usb/` listing](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb).

### 2.2 I2S / INT-Device firmwares (in `xmos_firmwares/i2s/`)

Documented for completeness; **JTS does not use these.** Listed
because if a Pi shows up with no `2886:001a` USB device at all, the
board might be on an I2S firmware (the I2S firmwares **do not
expose USB Audio at all** — only the I2C control + I2S audio pins).

| Filename | Use case | Sample rate |
|---|---|---|
| `respeaker_xvf3800_i2s_dfu_firmware_v1.0.4.bin` | Generic I2S slave | 16 kHz |
| `respeaker_xvf3800_i2s_dfu_firmware_v1.0.7.bin` | Generic I2S slave (current) | 16 kHz |
| `respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.5_48k.bin` | I2S master, ESPHome demo era | 48 kHz |
| `respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.7_48k_test5.bin` | I2S master, Home Assistant Voice PE-targeted, with wakeword support and 48 kHz | 48 kHz |

The HA I2S firmware uses a different channel layout — `Channel 0:
ASR, Channel 1: Wake word`. Unlike the USB firmware, the channel
1 here carries an on-chip wake-word indicator, not a second audio
stream.

### 2.3 Recovery firmware

`xmos_firmwares/recover/4mb_all_ff.bin` — a 4 MB blob of 0xFF used
to wipe the DataPartition when a corrupted `SAVE_CONFIGURATION`
has bricked normal boot. **It is intentional that the flash fails
at ~96%** ("Cannot program memory due to received address that is
out of range"); the partition has been wiped by that point and you
re-flash real firmware on top. See §5.1 for the full recovery
procedure.

### 2.4 DFU flashing — alt-setting semantics

The chip exposes three DFU alt-settings:

| alt= | Name | Purpose |
|---|---|---|
| `0` | `reSpeaker DFU Factory` | The **Safe Mode** / recovery partition. Read-only during normal operation. Don't write here. |
| `1` | `reSpeaker DFU Upgrade` | **The firmware partition.** This is where routine firmware writes go. Always available, in normal runtime as well as Safe Mode. |
| `2` | `reSpeaker DFU DataPartition` | Persistent parameter store (what `SAVE_CONFIGURATION` writes to). Visible only in Safe Mode (entering it via the mute-button-during-power-on dance). |

**The chip supports in-system DFU upgrade** — the USB descriptor in
normal runtime advertises a DFU function at interface 4 alt 1
alongside the chip's audio class interfaces, so `dfu-util` can write
firmware while the chip is plugged in and running normally. No button
combo or Safe Mode entry is required for routine upgrades. Confirmed
empirically via `lsusb -v -d 2886:001a` on both jts and jts2 chips
on 2026-05-15; the relevant descriptor block is:

```
Interface Descriptor:
  bAlternateSetting       1
  bInterfaceClass       254 Application Specific Interface
  Device Firmware Upgrade Interface Descriptor:
    ...
```

The Seeed wiki's "button combo to enter DFU mode" procedure is for
**Safe Mode** entry — used when the DataPartition is corrupted and
the normal-mode DFU interface isn't reachable (because boot hangs
before USB enumerates). For a routine 2-ch → 6-ch firmware upgrade,
the chip flashes itself in place.

**The correct flash command is `dfu-util -R -e -a 1`.** Writes to
alt 0 silently no-op — the chip stays on whatever firmware it had.

```sh
sudo apt install -y dfu-util
sudo dfu-util -l                                   # confirm alt=1 is visible
sudo dfu-util -R -e -a 1 -D <firmware-blob.bin>
```

`-R` resets the chip back to run-time mode after flashing; `-e` is
"detach (exit DFU) before download" (harmless and required on some
host stacks). The `Invalid DFU suffix signature` warning at the
start of dfu-util output is normal — Seeed doesn't sign the
binaries. BRINGUP.md Phase 2A.5 has the full operator-facing
procedure (download URL, verification steps, what each `dfu-util`
flag does and why).

Sources: [upstream dfu_guide.md](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/xmos_firmwares/dfu_guide.md), [upstream issue #8](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/8) (DataPartition + recovery), [Seeed wiki Update Firmware section](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/), and the captured `lsusb -v` from the 2026-05-15 jts2 investigation (`logs/xvf-interrogate-*-jts2-*-20260515T*.txt`).

### 2.5 Reading the running firmware version

Three useful parameters are bundled into "what firmware am I
running":

```sh
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
#   → e.g. VERSION: [2, 0, 8]
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_MSG
#   → e.g. BLD_MSG: ['u', 'a', '-', 'i', 'o', '1', '6', '-', 's', 'q', 'r']
#     means "ua-io16-sqr"  =  USB-UA / 16-kHz IO / square mic geometry
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_REPO_HASH
#   → e.g. BLD_REPO_HASH: ['a','1','f','7','0','6','5','1', …]
#     i.e. git hash from sw_xvf3800 — the firmware-side repo
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BLD_MODIFIED
#   → 'False' if built from a clean tree; 'True' if XMOS modified it
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host BOOT_STATUS
#   → e.g. BOOT_STATUS: ['J', 'o', 'F']
#     character meanings unverified upstream (see §6.6 below)
```

As of 2026-05-15, the 6-ch firmware variant v2.0.8 (the one JTS
production has been tested with) reports `BLD_REPO_HASH` =
`a1f70651e992d6f0bcff655b26925d33999b9c2d`. Different production
chips on the same firmware report identical hashes, so this is
useful for verifying "did the flash actually take" after an
upgrade. The known-good hash is also tracked as
`FIRMWARE_KNOWN_GOOD_BLD_REPO_HASH` in
[`jasper/mics/xvf3800.py`](../jasper/mics/xvf3800.py) — bump it
there when you verify a newer firmware version.

The 6-channel firmware can also be confirmed at the ALSA layer
without USB control:

```sh
# Pin to the Capture: section — /proc/asound/<card>/stream0 has
# Playback first (Channels: 2 from the XVF's 2-ch playback endpoint)
# then Capture (Channels: 6 on 6-ch firmware). A naive `grep Channels:`
# returns the Playback value — exactly the May 2026 reconciler bug.
awk '/^Capture:/{c=1} c && /Channels:/{print; exit}' /proc/asound/Array/stream0
# expect "Channels: 6" on 6-ch; "Channels: 2" on default firmware
```

---

## 3. Channel layout (6-channel firmware, JTS production)

**Primary sources (cite these before guessing):**
- [XMOS XVF3800 User Guide v3.2.1, XM-014888-PC §3.6.1 Table 3.2](https://www.xmos.com/documentation/XM-014888-PC/pdf/xvf3800_user_guide_v3.2.1.pdf) — audio manager mux options + Category definitions.
- [XMOS Audio Pipeline doc, Fig. 4.1](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html) — block diagram showing where each tap lives.
- [Seeed wiki USB firmware table](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/) — verbatim channel listing.
- [respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY host_control README](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md) — Output Selection section.

### What lives on each channel

| Idx | Content | Mux category | DSP applied | Tap point |
|---|---|---|---|---|
| 0 | **Conference** | Cat 7 (auto-select beam, Conference branch) | **AEC + BF + NS + NLP + AGC + HPF**, comms-tuned (slower beam tracking, comms-AGC dynamics) | Output of full SHF post-processing chain |
| 1 | **ASR** | Cat 7 (auto-select beam, ASR branch) | **AEC + BF + NS + NLP + AGC + HPF**, ASR-tuned (faster beam tracking, fixed gain for speech engines) | Output of full SHF post-processing chain |
| 2 | **Raw mic 0** | Cat 1 — *"Raw microphone data — before amplification, no system delay applied"* | **NONE**. Not even `AUDIO_MGR_MIC_GAIN`. Bit-exact ADC output. | Direct from the 192-tap PDM-to-PCM decimator, BEFORE the Gain block in Fig. 4.1. |
| 3 | Raw mic 1 | Cat 1 | NONE | Same |
| 4 | Raw mic 2 | Cat 1 | NONE | Same |
| 5 | Raw mic 3 | Cat 1 | NONE | Same |

### What "the chip processing" means for ch 0/1 specifically

Channels 0 and 1 are the output of the chip's full SHF DSP chain.
What that chain does, in order (User Guide §4.1, Fig. 4.1):

1. **MIC_GAIN** — `AUDIO_MGR_MIC_GAIN` linear pre-amp on the 4 mics.
2. **AEC_HPFONOFF** — 4th-order Butterworth HPF, applied per-mic
   before the SHF block. Disabled by default; we set `on125`
   (125 Hz) in jasper-aec-init.
3. **SHF block** (AEC + beamformer + NS + AGC + NLP, gated on
   `SHF_BYPASS`). In our config we set `SHF_BYPASS=1` to bypass
   the entire SHF block because the chip's AEC reference path is
   incompatible with our external-DAC topology (see
   [HANDOFF-aec.md](HANDOFF-aec.md) § "Why SHF_BYPASS=1 instead of
   relying on chip AEC").
4. **Output mux** — routes the SHF output (or, when bypassed, the
   pre-SHF signal) to USB capture channels 0/1.

**`SHF_BYPASS=1` removes the entire step-3 SHF block — not just
AEC, but BF/NS/AGC/NLP along with it.** Empirically verified
2026-05-16: with `SHF_BYPASS=1`, toggling `PP_MIN_NS` and
`PP_AGCONOFF` produces 0.2–1.0 dB of variation on channel 1 —
indistinguishable from measurement noise. The chip post-processing
params are inert when SHF_BYPASS=1.

So **channels 0/1 with `SHF_BYPASS=1` carry raw-ish mic data**
(only MIC_GAIN + AEC_HPFONOFF still apply), functionally similar
to channels 2–5. The level difference between ch 1 (SHF_BYPASS=1)
and ch 2 (raw mic, Category 1) is about 1 dB across all bands —
likely just MIC_GAIN being applied on ch 1 via the output mux but
not on ch 2's Category 1 tap.

If you want chip BF/NS/AGC to actually run, you need `SHF_BYPASS=0`.
But that re-enables the chip's AEC, which is broken in our
external-DAC topology. There is no chip parameter that lets you
keep BF/NS/AGC while disabling only the AEC adaptive filter — they
are gated on the same flag.

### Which chip parameters affect which channels

| Parameter family | Affects ch 0/1 (SHF_BYPASS=0) | Affects ch 0/1 (SHF_BYPASS=1) | Affects ch 2-5 |
|---|---|---|---|
| `AEC_HPFONOFF` (HPF, pre-SHF) | ✅ yes | ✅ yes | ❌ no |
| `PP_AGCONOFF` / `PP_AGC*` (AGC) | ✅ yes (User Guide §4.2.6: *"applied equally to all four processed outputs"*) | ❌ no (SHF bypassed) | ❌ no |
| `PP_MIN_NS` / `PP_MIN_NN` (NS) | ✅ yes | ❌ no (SHF bypassed) | ❌ no |
| `PP_ECHOONOFF` / `PP_NL*` (NLP) | ✅ yes | ❌ no (SHF bypassed) | ❌ no |
| `AEC_FIXEDBEAMS*` (beamformer) | ✅ yes | ❌ no (SHF bypassed) | ❌ no |
| `SHF_BYPASS` (toggles entire SHF block) | ✅ yes | ✅ yes | ❌ no |
| `AUDIO_MGR_MIC_GAIN` | ✅ yes (pre-SHF) | ✅ yes (pre-SHF) | ❌ no — Category 1 taps before this stage |

**Empirically verified 2026-05-15/16**:

- With `SHF_BYPASS=0` (default), toggling `PP_MIN_NS` /
  `PP_AGCONOFF` while capturing 6 channels of pink noise showed
  1.5–8 dB of variation on ch 0/1; ch 2 showed 0.0–0.4 dB.
- With `SHF_BYPASS=1` (current JTS production), the same toggles
  produced 0.2–1.0 dB of variation on ch 1 — same as the
  measurement noise on ch 2 (0.1–0.7 dB).

The empirical SHF_BYPASS=1 test confirms: with SHF bypassed, the
chip post-processing parameters are inert across ch 0/1, ch 2-5
alike. Channels 0/1 become raw-ish mic feeds.

### What JTS uses, and why

**JTS captures channel 1 (ASR beam tap, post-SHF mux)** as the
AEC bridge's near-end input — see `jasper/mics/xvf3800.py`
`MIC_CHANNEL_INDEX = 1`. Combined with `SHF_BYPASS=1` in
`jasper-aec-init`, this means JTS captures a raw-ish mic feed
with chip MIC_GAIN + AEC_HPFONOFF applied (per the table above).

Ch 0 vs ch 1 is canonical XVF3800 territory:
- **Seeed's own example code**: 2-channel `arecord`, takes the
  default Conference/ASR output, no manual channel selection.
- **formatBCE ESPHome integration** (HA Voice with XVF3800):
  `i2s_mics, channels: 1` — ASR beam.
- **Reachy Mini (Pollen Robotics)**: default device + chip AEC.
- **Public reference**: no project in public code consumes raw
  mic channels (2-5) for ASR.

We pick ch 1 over ch 0 partly out of community convention (the
"ASR" tap is the speech-recognition-oriented one when chip SHF
is running) and partly because ch 1 receives MIC_GAIN via the
output mux (ch 2 doesn't). With our SHF_BYPASS=1 the ASR-tuning
distinction is moot — both ch 0 and ch 1 carry raw-ish data
when SHF is bypassed — but ch 1 is still the right choice
because: (a) it's the project-standard tap; (b) it includes
MIC_GAIN; (c) if anyone ever flips SHF_BYPASS=0, ch 1 will
automatically give us the ASR-tuned signal.

The pairing with `SHF_BYPASS=1` exists because the chip's own AEC
stage is sabotaged by our external-DAC topology (the chip mirrors
the host's UAC playback volume into AEC_FAR_EXTGAIN, which in our
setup attenuates the reference by an unpredictable amount).
`SHF_BYPASS=1` removes the chip AEC from the path — and, as a
side effect, also disables chip BF + NS + AGC. Software AEC3 in
jasper-aec-bridge handles echo cancellation + residual NS
host-side using the music chain as a clean digital reference.

See [HANDOFF-aec.md](HANDOFF-aec.md) § "Production tuning
(2026-05-16)" for the full rationale and the bridge-side knobs
(REF_GAIN, MIC_GAIN, HPF, etc.) that complete the picture.

### Historical: why we previously used channel 2 (and why we stopped)

Until 2026-05-15, the bridge captured channel 2 (raw mic 0). The
rationale at the time was "AEC3 wants clean linear input;
the chip's AGC introduces non-linearity that could confuse the
adaptive filter." That argument was defensible in isolation, but:

1. **No public XVF3800 deployment does this.** We were inventing
   a topology with no field validation.
2. **The trade-off was lopsided.** We gave up BF, NS, AGC, HPF,
   and MIC_GAIN — every single chip DSP feature — to preserve
   linearity that AEC3's residual echo suppressor is designed to
   tolerate anyway.
3. **Measured outcome**: AEC3's bass cancellation was weak
   (10-15 dB at sub-bass vs 25-34 dB at high freqs). Users heard
   "boomy bass" in the post-AEC output because the chip wasn't
   doing its job and AEC3 alone couldn't compensate.

The switch to channel 1 + `SHF_BYPASS=1` is the canonical
architecture with one targeted modification (chip AEC off,
software AEC on). The chip's mild AGC non-linearity is a
theoretical concern that AEC3 absorbs in practice.

### How channel routing actually works inside the chip

The chip has an internal **output mux** that lets you re-route what
goes onto each USB capture channel using two write-only parameters,
`AUDIO_MGR_OP_L` (channel 0) and `AUDIO_MGR_OP_R` (channel 1). Each
takes a `(category, source)` pair from the table in §6.3. The
defaults on the 2-ch firmware are:

- Channel 0 ← `(8, 0)` — User-chosen, which by default mirrors
  Category 6 / Source 3 (the auto-select beam) — i.e. the
  Conference output.
- Channel 1 ← `(6, 3)` or equivalent — Auto-select beam, but
  via the ASR processing branch.

The 6-ch firmware **adds four more output mux slots** for channels
2-5, defaulting them to Category 1 sources 0-3 (Raw microphone
data — pre-amplification, no system delay applied). These defaults
are NOT exposed via individual `AUDIO_MGR_OP_*` commands on the
6-ch firmware — the only commands the host can issue are still
`OP_L`/`OP_R`/`OP_L_PK*`/`OP_R_PK*` (slots 0/1), so ch2-5 routing
is fixed in firmware. **You cannot misconfigure ch2-5 routing
from the host** on the stock 6-ch firmware.

That said, channel routing **can be affected indirectly**:

- If `AUDIO_MGR_OP_PACKED` (§6.4) is enabled for L or R, the
  affected slot expects PACKED input from the I2S side, which it
  may not be receiving — the slot can render silence in some pack
  modes if the upstream data is missing. **This affects ch0/1
  only on the USB firmware** (the chip's "L"/"R" map onto USB
  capture channels 0 and 1). Ch2-5 are NOT subject to this on
  6-ch firmware.
- `SHF_BYPASS` (§6.1) replaces the post-SHF DSP with raw mic
  data on ch0/1 — saturating the "Conference"/"ASR" slots. **It
  does not affect ch2-5**, which are already raw.

So if you see "ch0/1 have content but ch2-5 are zeros," **that is
NOT a routing/parameter problem reachable from the host on stock
6-ch firmware.** It is a data-plane problem upstream of the output
mux. See §7 for the full hypothesis ladder.

---

## 4. The full parameter space (resid/cmdid table)

The vendored `jasper/xvf/xvf_host.py` (verbatim from
[respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY @ python_control/xvf_host.py](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/python_control/xvf_host.py))
encodes the host-visible parameter table. Each parameter is a
`(resid, cmdid, length, type, access, description)` tuple that maps
to a USB vendor control transfer (`bmRequestType =
VENDOR|DEVICE|OUT`, `bRequest = 0`, `wValue = cmdid` for write or
`0x80|cmdid` for read, `wIndex = resid`).

Resource IDs (`resid`) group the parameters by subsystem:

| `resid` | Subsystem | What lives here |
|---|---|---|
| 17 | **PP** — Post-Processing | AGC, limiter, noise suppression, echo suppression tuning |
| 20 | **GPO** — General-Purpose Output + LED control | GPI/GPO read/write, LED effect/color/brightness, DOA |
| 33 | **AEC** — Acoustic Echo Cancellation | Filter coeffs, AEC convergence, RT60, beamformer azimuths |
| 35 | **AUDIO_MGR** — Audio Manager | **Channel routing**, MIC/REF gain, packed/upsample modes, I2S idle timing |
| 48 | **APPLICATION_SERVICER** — App-level | VERSION, BLD_*, BOOT_STATUS, REBOOT, USB_BIT_DEPTH, **SAVE_/CLEAR_CONFIGURATION** |

The four parameters most relevant to "why are channels zero" all
live in `resid 35` (`AUDIO_MGR`) and `resid 48` (`APPLICATION`).

Below we walk through each one that's load-bearing for channel
routing, audio I/O, and the silent-channel hypothesis ladder.
Defaults marked "verified" come from `jasper-doctor` / on-device
reads; defaults marked "Seeed pre-tuned" come from the
`host_control/README.md` "Tunning" section.

### 4.1 `AUDIO_MGR_MIC_GAIN` (resid 35, cmdid 0, length 1, float, RW)

**Audio Mgr pre-SHF microphone gain** — a single global linear
gain applied to the mic signals **after the PDM decimator and
before the SHF (echo cancellation + beamformer) cores**.

| | |
|---|---|
| Type | float (linear gain, not dB) |
| Scope | **global** — affects all four mics equally; not per-channel |
| Default | **90.0** (Seeed pre-tuned; this is the value we see on JTS) |
| Range | Not documented upstream — `xvf_host.py` accepts any float. Likely 0.0 to a large value; 90.0 is the production setting. |

**Important for the silent-channel question:** this gain feeds the
SHF / beamformer path, which produces ch0/1. **It does not affect
the raw-mic path that feeds ch2-5** on the 6-ch firmware (that
path is pre-amplification per Category 1 in §6.3). So setting
`AUDIO_MGR_MIC_GAIN=0` would silence ch0/1 but **not** ch2-5.

Conversely, observing ch0/1 with signal AND ch2-5 silent says
the SHF/beamformer is fine, so the PDM decimator output reaching
the SHF input is fine; the silent-ch2-5 path forks somewhere
between the PDM decimator and the output mux on the 6-ch
firmware's data plane.

### 4.2 `AUDIO_MGR_REF_GAIN` (resid 35, cmdid 1, length 1, float, RW)

**Audio Mgr pre-SHF reference gain** — symmetric to MIC_GAIN but
for the far-end reference signal coming in over I2S or USB-IN.

| | |
|---|---|
| Type | float (linear gain, not dB) |
| Default | **8.0** (Seeed pre-tuned) |

Unrelated to USB capture channel content — this gain is on the
*input* side, scaling whatever the host writes into the chip's
playback endpoint before it enters the AEC reference path.

### 4.3 `AUDIO_MGR_SELECTED_CHANNELS` (resid 35, cmdid 12, length 2, uint8, RW)

> "Default implementation of post processing will use this to
> select which channels should be output to
> MUX_USER_CHOSEN_CHANNELS. Note that a customer implementation of
> the beam selection stage could override this command. How this
> channel selection aligns with actual output depends on the mux
> configuration."

| | |
|---|---|
| Format | 2 × uint8 — `[selected_for_left, selected_for_right]` |
| JTS value | **`[3, 3]`** — both User-chosen slots map to the auto-select beam |
| Default | `[3, 3]` (same — auto-select beam for both) |
| Meaningful values | 0..3, corresponding to the four beams: Focused 1, Focused 2, Free-running, Auto-select |

This drives what Category 8 ("User chosen channels") sources 0/1
contain, **not** what they get routed to. Routing to USB capture
channels 0/1 is via `AUDIO_MGR_OP_L`/`OP_R`. Default routing has
USB ch0 = `(8, 0)` and ch1 = something post-processed-ASR-flavored.

**Cannot cause ch2-5 silence on 6-ch firmware** — only affects the
*content* of the User-chosen category, which by default goes to
ch0/1 only.

### 4.4 `AUDIO_MGR_OP_PACKED` (resid 35, cmdid 13, length 2, uint8, RW)

> "`<L>`, `<R>`; Sets/gets packing status for L and R output
> channels"

| | |
|---|---|
| Format | 2 × uint8 — `[L_packed_flag, R_packed_flag]` |
| Values | `0` = unpacked (single-source per output channel), nonzero = packed (output channel carries 3 multiplexed sources via OP_L_PK0/PK1/PK2) |
| Default | `[0, 0]` (unpacked; verified on JTS) |

When packed mode is enabled, **the output channel emits a
specific bit-pattern interleaving 3 source signals into one
24-bit word**, decoded by the host. This is XMOS's
"three-mics-per-USB-channel" trick to squeeze 6 mics into 2
USB channels on a stock UAC2 endpoint, used in some XVF3800
configurations.

**Read this carefully if you see ch0/1 output as garbage / static
when it should be voice:** the packed flag affects content
decoding. But again, **it only affects ch0/1 on the 6-ch
firmware** — ch2-5 are raw and not subject to packing.

### 4.5 `AUDIO_MGR_OP_UPSAMPLE` (resid 35, cmdid 14, length 2, uint8, RW)

> "`<L>`, `<R>`; Sets/gets upsample status for L and R output
> channels, where appropriate"

| | |
|---|---|
| Format | 2 × uint8 — `[L_upsample_flag, R_upsample_flag]` |
| Default | `[0, 0]` (verified on JTS) |

Toggles internal upsampling on the L/R output paths. Useful when
the USB endpoint rate is 48 kHz but the SHF pipeline runs at 16 kHz
(or vice versa). On stock 16-kHz USB firmware (which is what
v2.0.8 6chl is), this is always 0.

**Cannot cause ch2-5 silence on 6-ch firmware.**

### 4.6 `AUDIO_MGR_OP_L` / `_OP_R` / `_OP_ALL` and the packing variants

The output-mux configuration commands. Detailed table in §6.3
below.

- `AUDIO_MGR_OP_L` / `AUDIO_MGR_OP_R` (cmdid 15/19): pair of
  `(category, source)` for USB channels 0 and 1. Equivalent to
  the `_PK0` variants below.
- `AUDIO_MGR_OP_L_PK0` / `_PK1` / `_PK2` (cmdid 16/17/18): three
  sources for the L channel when in PACKED mode (`AUDIO_MGR_OP_PACKED[0]=1`).
- `AUDIO_MGR_OP_R_PK0` / `_PK1` / `_PK2` (cmdid 20/21/22): same for R.
- `AUDIO_MGR_OP_ALL` (cmdid 23, length **12**): writes all six
  `_PK0/1/2` slot pairs in one transfer.

**Defaults**: L=`(8, 0)`, R=`(0, 0)` per Seeed wiki Output Selection
section (the "L is auto-select beam, R is Silence" baseline).
**Production JTS does not leave `OP_R` at firmware default.** The AEC
bridge consumes XVF capture channel 1 (`MIC_CHANNEL_INDEX = 1`), so
`jasper-aec-init` writes and read-back verifies OP_L=`(8, 0)` and
OP_R=`(8, 0)`. Leaving OP_R at the Seeed default `OP_R=(0, 0)` mutes
the bridge input. The wake-corpus chip-AEC comparison profile is the
narrow exception: while corpus test mode owns
`/var/lib/jasper/wake_corpus_bridge.env`, `jasper-aec-init`
temporarily writes and read-back verifies OP_L=`(7, 0)` and
OP_R=`(7, 1)` to expose the fixed-gated 150°/210° ASR outputs as
corpus-only capture legs. Exiting corpus test mode removes that overlay
and re-runs the production init, which explicitly restores OP_L=`(8, 0)`
and OP_R=`(8, 0)`.

**These commands address slots 0/1 only.** There is no
`AUDIO_MGR_OP_2` / `_OP_3` / `_OP_4` / `_OP_5` — the routing for
ch2-5 on 6-ch firmware is not host-controllable.

### 4.7 `AEC_NUM_MICS` (resid 33, cmdid 71, length 1, int32, **read-only**)

Number of mic inputs the AEC subsystem is configured for. On 6-ch
firmware, expected value: **4**.

Useful as a sanity check that the chip's data plane has 4 mics
connected to the SHF cores — if this reads 0 or a value < 4 in a
firmware that should have 4, something has gone wrong at chip
init.

### 4.8 `AEC_NUM_FARENDS` (resid 33, cmdid 72, length 1, int32, **read-only**)

Number of far-end (reference) inputs the AEC sees. Expected: **1**
on stock firmware (one stereo I2S input that the chip downsamples
internally).

### 4.9 `SHF_BYPASS` (resid 33, cmdid 70, length 1, uint8, RW)

**AEC bypass** — bypasses the entire SHF (Sub-band Howling-cancellation
Filter, internally meaning the AEC + beamformer + NS chain), routing
raw mic data through to the output mux for the "post-SHF" categories.

| | |
|---|---|
| Format | uint8 |
| Default | 0 (SHF active) |
| JTS value | 0 (verified on jts production) |
| When set to 1 | The "post-SHF DSP channels" output category (Category 9 in §6.3) carries raw mic data instead of beamformed/noise-suppressed data. Conference (ch0) and ASR (ch1) get bypassed too. |

The prompt observed: **"`SHF_BYPASS=0`, but `SHF_BYPASS=1`
saturates ch0/ch1 while ch2-5 stay zero."** That is consistent
with what SHF_BYPASS controls. It tells us ch0/1 are downstream
of SHF (because they react to SHF_BYPASS), and that ch2-5 are
**upstream** of SHF (because they don't). The silent path is
between the PDM decimator and the ch2-5 output mux on the 6-ch
firmware data plane.

### 4.10 `USB_BIT_DEPTH` (resid 48, cmdid 8, length 2, uint8, RW)

> "Only relevant for the UA device variant. For the UA device,
> set or get the USB bit depth IN, OUT to either 16, 24 or 32.
> Setting will reboot the chip, resetting all other parameters
> to default. If issued to the INT device, a set is ignored and
> the device is not rebooted while a get always returns 0 as
> both IN and OUT bit depths."

| | |
|---|---|
| Format | 2 × uint8 — `[IN_bit_depth, OUT_bit_depth]` |
| JTS value | **`[16, 16]`** (verified) |
| Side effect of writing | **The chip reboots and ALL parameters return to defaults** (including LED state, GPIO, etc.). |

If anyone has ever called this with mismatched values, the
endpoint advertisement on USB might be inconsistent with how the
host expects to read it — but on JTS we use defaults. **Cannot
cause ch2-5 silence** in isolation; would more likely cause the
audio class endpoint to fail to enumerate at all.

### 4.11 `SAVE_CONFIGURATION` (resid 48, cmdid 9, length 1, uint8, WO)

> "Set to any value to save the current configuration to flash."

**DO NOT CALL THIS.** Known brick hazard on firmware 2.0.6 (upstream
issue #8). Not confirmed fixed in 2.0.7 or 2.0.8. The chip writes
parameters to its DataPartition (DFU alt=2), and a corrupted write
puts the chip into "USB doesn't enumerate, only Safe Mode works"
state requiring `4mb_all_ff.bin` recovery. See §5.1.

### 4.12 `CLEAR_CONFIGURATION` (resid 48, cmdid 10, length 1, uint8, WO)

> "Set to any value to clear the current configuration and revert
> to the default configuration."

The companion to `SAVE_CONFIGURATION`. Returns the chip to
factory-default parameters at next boot. Sanity move when you
suspect the DataPartition contains a configuration that's silencing
channels or otherwise misbehaving — **but be aware it won't reset
DataPartition corruption that has already bricked the chip** (you
need the recovery procedure in §5.1 for that).

### 4.13 `REBOOT` (resid 48, cmdid 7, length 1, uint8, WO)

> "Set to any value to reboot the chip and reset all parameters to
> default."

A soft reset. The chip drops off USB, then re-enumerates ~2-3
seconds later with all parameters returned to defaults. Used by
`jasper-aec-init` as the first thing it does on boot, to clear
any stale AEC filter state.

This is the **safest fault-isolation move**: if `REBOOT 1` clears
the symptom, your stale state was held in RAM (live parameters)
rather than in flash (DataPartition).

### 4.14 `BOOT_STATUS` (resid 48, cmdid 5, length 3, char, RO)

> "Shows whether or not the firmware has been booted via SPI or
> JTAG/FLASH"

| | |
|---|---|
| Format | 3-character ASCII string |
| JTS value | **`'Jof'`** observed on production (the prompt mentions `'J','o','F'`) |
| Documented meaning | **Not documented upstream.** xvf_host.py just describes the field; the char meanings are not in the host_control README or any other Seeed-published doc we've found. |

**Best educated guess based on context:** the characters are
status flags. The XVF chip has multiple boot sources (internal
flash via SPI vs JTAG-loaded firmware vs Safe Mode); the
characters likely encode "which boot source," "is the data
partition clean," and "is the firmware verified." We have **not
verified** this — treat as unverified pending an XMOS source code
release or further Seeed clarification.

What we have empirically:
- `'Jof'` (capital J, lowercase o, lowercase F) is what JTS shows
  in normal operation on jts.local.
- Upstream `BLD_MSG` output example shows `['u', 'a', '-', 'i',
  'o', '1', '6', '-', 's', 'q', 'r']` (i.e. "ua-io16-sqr") which
  is just `<USB-UA>-<16kHz I/O>-<square mic geometry>` and is
  unrelated to BOOT_STATUS.

If a future investigation needs this, the most likely "fault"
indicator value to look for is anything other than `'Jof'`. Until
we have a working chip side-by-side with a broken chip both
returning BOOT_STATUS, we can't conclusively map char positions
to meanings.

### 4.15 The XMOS chip parameter table in full

The full PARAMETERS dict in `jasper/xvf/xvf_host.py` is the
single source of truth — Python file, 95 entries — covering AEC
(filter coeffs, beamformer azimuths, RT60), Audio Manager
(routing, gain, idle timing), Post-Processing (AGC, limiter, NS,
echo suppression), GPIO, LED, App-level (VERSION, REBOOT,
USB_BIT_DEPTH). For diagnostics not covered above, `xvf_host.py
--list` prints the whole table on the Pi.

---

## 5. Documented failure modes

### 5.1 `SAVE_CONFIGURATION` bricks the chip (upstream issue #8)

Symptom: chip stops enumerating as `2886:001a` USB Audio in
normal mode. LEDs come up (so it has power and bootloader runs)
but USB audio class is dead. The chip is still reachable in Safe
Mode (alt=0 visible via `dfu-util -l`).

Cause: `SAVE_CONFIGURATION` writes the live parameter set to
flash (DataPartition, DFU alt=2). On firmware 2.0.6, this write
can leave the partition in a state the firmware can't parse on
the next boot, and the firmware crashes early in init — before
USB audio class registration. **Not confirmed fixed in 2.0.7 or
2.0.8.** Upstream comment from Seeed says "I think the
configuration data is corrupted on your device" — i.e. they treat
it as expected behaviour under certain conditions.

Recovery (the only known one):

1. Power off the device.
2. Hold the mute button; reconnect power. The red LED blinks → Safe Mode.
3. Confirm: `sudo dfu-util -l` shows `alt=0`, `alt=1`, and
   crucially `alt=2 "reSpeaker DFU DataPartition"`.
4. Wipe DataPartition by attempting to flash the all-FF blob:
   ```sh
   sudo dfu-util -e -a 1 -D /path/to/4mb_all_ff.bin
   ```
   **It will fail at ~96%** with `Cannot program memory due to
   received address that is out of range`. **This is expected.**
   The DataPartition has been zeroed by that point.
5. Re-flash a known-good USB firmware:
   ```sh
   sudo dfu-util -R -e -a 1 \
       -D /path/to/respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin
   ```
6. Power cycle. The chip should now enumerate normally.

JTS's defence-in-depth: `jasper-aec-init` never calls
`SAVE_CONFIGURATION` (we set everything fresh on each boot via
the chip's RAM-state parameters), and the rule is recorded in
both the AEC handoff and the CLAUDE.md instructions.

Source: [upstream issue #8](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/8) including the Seeed (`jerryyip`) recovery comment.

### 5.2 No 48 kHz USB capture rate

The default USB firmware advertises **only 16 kHz** on its capture
endpoint. Upstream issue #6 asks for 48 kHz; PR #13 attempts to
add it as v2.0.10 with a 6-channel 48 kHz firmware variant — but
the PR has not been merged. Practical implication: every host-side
processing chain is locked to 16 kHz on the input, and the AEC
bridge has to operate at 16 kHz too (which is fine — WebRTC AEC3's
default is 16 kHz).

For 48 kHz speech work, the only path today is the I2S **HA
firmware** (`respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.7_48k_test5.bin`),
which is incompatible with USB use.

Source: [upstream issue #6](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/6), [upstream PR #13](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/13).

### 5.3 Rainbow boot flash unconfigurable (upstream issue #5)

On every boot, the WS2812 ring runs ~2 seconds of "rainbow"
animation before settling into DoA mode. Saving an alternate LED
config to flash does NOT override this — the rainbow phase is
hardcoded. Cosmetic; harmless. No known recovery / disablement
short of source-code modification (which requires XMOS XTAG-4
hardware and gated SDK access).

### 5.4 DOA reads stale unless `LED_EFFECT == DoA` (upstream issue #16)

Bug: `DOA_VALUE` returns a stale cached value whenever
`LED_EFFECT != 4` (4 = DoA mode). The DoA pipeline still runs
(beamformer/auto-select is feeding ch0/1) — only the readback
shim is broken. Workaround: keep `LED_EFFECT=4` (the default), or
poll the chip's beam azimuths via `AEC_AZIMUTH_VALUES` instead.

### 5.5 USB bandwidth / high-speed requirement

The 6-channel firmware doubles the capture endpoint width
(2 × 16-bit → 6 × 16-bit at 16 kHz = 192 kB/s vs 64 kB/s on 2-ch).
Both fit comfortably in USB 2.0 High-Speed isochronous, but:

- If the Pi negotiates Full-Speed (12 Mb/s) for any reason — a
  flaky USB-C cable that misadvertises high-speed, a hub in the
  path that's not transparent, a poorly seated connector — the
  isochronous endpoints may either fail to enumerate or deliver
  truncated packets.
- Truncated isoc packets typically appear as **silence on the
  later channels** because USB Audio Class delivers data in
  channel order within each microframe. A 6-channel packet that
  arrives short would have ch0-1 intact and ch2-5 missing — i.e.
  **exactly the symptom on jts2.**

This is a real failure mode, not speculation. The kernel side
should log `endpoint underrun` or similar; check `dmesg -T` and
`journalctl -k --since "5 minutes ago" | grep -i -E 'usb|audio|underrun'`.

How to inspect:

```sh
# What speed did the chip negotiate?
lsusb -t | grep -B2 2886
#   line should say "10000M" (10 Gb/s, USB 3) or "480M" (HS).
#   "12M" = Full-Speed = problem.

# Endpoint sizes — should be large enough for 6 channels
lsusb -v -d 2886:001a 2>/dev/null | grep -A2 'Endpoint Desc'

# Kernel errors
sudo dmesg -T | grep -i -E 'usb|audio|underrun' | tail -50
```

We have not seen this in production at jts.local, but jts2 is
running on a 1 GB Pi 5 which uses a different USB host controller
config layer (same dwc3, but with less RAM headroom for the USB
DMA scatter-gather lists). Pi 5 has known sensitivity to USB
power-delivery glitches when CPU governor changes happen during
ISO traffic; the symptom is usually "audio drops out for ~200 ms
and recovers," not "permanent silent channels," but it's worth
ruling out.

### 5.6 PortAudio one-shot streams on UAC2 underrun

Not a chip bug — a host-side downstream of XVF chip issues. When
the chip's USB IN endpoint underruns (because the Pi is too slow
to drain it, or because the chip's PDM decimator has a transient
fault), PortAudio's `sd.InputStream` enters an unrecoverable state
and silently stops invoking the callback. The bridge stops getting
mic frames. This is why `jasper-aec-bridge` has stall detection
(`BridgeStalled`) and systemd `Restart=on-failure`. Documented in
[AGENTS.md "AEC bridge — reconciler toggle"](../AGENTS.md#aec-bridge--reconciler-toggle) and at
the top of `jasper/cli/aec_bridge.py`.

This would manifest as **all six channels going silent
simultaneously**, not ch2-5 selectively. So it's NOT the
hypothesis for the jts2 symptom — but worth knowing if you see
the bridge log "mic queue empty" repeatedly.

### 5.7 Other documented issues (low relevance to channel silence)

- Issue #12: ESPHome `respeaker_xvf3800` driver fails with
  `resid=20 cmd=18 error=2`. resid 20 is GPO/LED, cmd 18 is
  `LED_RING_COLOR`. That command was added in v2.0.7; if the
  ESPHome driver targets a firmware that doesn't have it,
  control transfer fails. Affects LED behaviour, not audio.
- Issue #15: defaults aren't documented publicly. We've
  reconstructed them above where they matter.
- Issue #10: wake-word detection in the I2S-master firmware is
  partly documented; not relevant to our USB firmware use.
- Issue #14, #7: future I2S firmware work and ESP32S3 UDP
  streaming — both unrelated.

### 5.8 Has anyone reported "raw mic channels silent" specifically?

Searched upstream issues + Seeed forum + Stuart Naylor's HA/Rhasspy
writeups + the FormatBCE ESPHome XVF3800 project. **No public
report matches the jts2 symptom directly** — silent ch2-5 while
ch0/1 work on identical chip+firmware on a different host.

The closest neighbours:
- Issue #8 (`SAVE_CONFIGURATION` brick) — but that breaks USB
  enumeration entirely, not selective channels.
- Issue #6 (no 48k) — unrelated.
- The "USB bandwidth at marginal cables" failure mode (§5.5) is
  inferred from USB Audio Class standards and our knowledge of
  the Pi 5 USB stack; it has not been reported on the upstream
  repo specifically against XVF3800 6-ch firmware.

That makes the jts2 ch2-5 silence either (a) a host-side data
issue (USB bandwidth, kernel driver, ALSA capture config) that
specifically truncates channels — see §7, or (b) a
DataPartition-cached-config issue (if someone in this chip's
history ran `SAVE_CONFIGURATION` and persisted a routing /
gain that mutes those channels — though as we said in §3, ch2-5
routing is not host-controllable on stock 6-ch firmware), or
(c) genuinely novel.

---

## 6. Parameter reference tables (the ones that matter for routing)

This section is the lookup table you'll come back to for the
non-obvious bits. Most of this comes from the upstream
`host_control/README.md` and the XMOS user guide §4 ("Tuning the
Application"), but they're scattered across multiple docs so we
gather them here.

### 6.1 `USB_BIT_DEPTH` value semantics

| Index | Meaning | Allowed values |
|---|---|---|
| `[0]` | IN bit depth (host → chip, i.e. AEC reference path) | 16, 24, 32 |
| `[1]` | OUT bit depth (chip → host, i.e. capture) | 16, 24, 32 |

Writing forces a chip reboot. JTS default `[16, 16]`.

### 6.2 `AUDIO_MGR_SELECTED_CHANNELS` value semantics

| Source value | Beam selected |
|---|---|
| 0 | Focused beam 1 |
| 1 | Focused beam 2 |
| 2 | Free-running beam |
| 3 | Auto-select beam (recommended) |

JTS observed `[3, 3]`.

### 6.3 `AUDIO_MGR_OP_L` / `_OP_R` — the full (category, source) matrix

Authoritative source: [upstream `host_control/README.md` "Output
Selection" table](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md#output-selection), which mirrors the XMOS XVF3800 v3.2.1 User
Guide Table 3.2 "audio manager mux options."

| Category | Meaning | Sources |
|---|---|---|
| **0** | Silence | `0`: Silence. *Default for the right channel output (ch1).* |
| **1** | Raw microphone data — **before amplification** | `0,1,2,3`: Specific microphones by index, no system delay applied |
| **2** | Unpacked microphone data | `0,1,2,3`: Unpacked mic signals. Only defined when using PACKED input — undefined otherwise. |
| **3** | Amplified microphone data, with system delay | `0,1,2,3`: Specific microphones by index. This is the signal passed to the SHF cores. |
| **4** | Far-end (reference) data | `0`: Far end data over I2S, post sample-rate conversion to 16 kHz |
| **5** | Far-end data + system delay | `0`: Same as 4 but with system delay applied |
| **6** | Processed data | `0,1`: Slow-moving post-processed beamformed outputs; `2`: Fast-moving post-processed beamformed output; `3`: **Auto-select beam** (recommended for selecting beamformed outputs) |
| **7** | AEC residual / ASR data | `0,1,2,3`: AEC residuals for the specified mic, OR ASR output for the specified beam (depending on `AEC_ASROUTONOFF`) |
| **8** | User-chosen channels | `0,1`: Currently copy the auto-select beam (Category 6, Source 3). *Default for the left channel output (ch0).* |
| **9** | Post-SHF DSP channels | `0,1,2,3`: All output channels from user post-SHF DSP. Saturates when `SHF_BYPASS=1`. |
| **10** | Far end at native rate | `0,1,2,3,4,5`: I2S-side data at the external interface rate. Sources 0-1 useful at 16 kHz; sources 0-5 useful at 48 kHz. |
| **11** | Amplified mic data, **before** system delay | `0,1,2,3`: Specific mics by index. |
| **12** | Amplified far-end + system delay | `0`: Far-end + fixed gain + system delay (this is the reference signal passed to the SHF cores) |

JTS-relevant: production writes OP_L/OP_R from `jasper-aec-init`
because the bridge consumes channel 1 and channel 1's firmware default
is silence. Corpus chip-AEC comparison mode temporarily writes the same
registers to expose fixed-beam residuals. On 6-ch firmware, ch2-5 are
wired in the firmware data plane to Category 1 sources 0-3 (raw mics)
— host cannot remap them.

### 6.4 `AUDIO_MGR_OP_PACKED` semantics

When `[L_packed, R_packed]` has a nonzero entry, the L (or R)
output channel becomes a single 24-bit word per sample containing
**three 8-bit-ish slots** (the exact bit layout is specific to
XMOS's packed format; intent is to carry 3 mic signals on one
USB-class channel for high-density configurations).

Decode side is the host — there is no general-purpose
"unpacker" utility in Seeed-published code; you'd have to read
the XMOS user guide §4 "Packed mode" for the bit layout. **JTS
does not use packed mode** (both are 0).

### 6.5 GPIO map (from `host_control/README.md` GPIO table)

| Pin | Direction | Function |
|---|---|---|
| `X1D09` | Input (RO) | Mute-button state — high when released |
| `X1D13` | Input (RO) | Floating |
| `X1D34` | Input (RO) | Floating |
| `X0D11` | Output (RW) | Floating |
| `X0D30` | Output (RW) | Mute LED + mic-mute circuit (high = mute) |
| `X0D31` | Output (RW) | Audio amplifier enable (LOW = enabled) |
| `X0D33` | Output (RW) | WS2812 LED power (high = on) |
| `X0D39` | Output (RW) | Floating |

If `X0D30` is somehow stuck high in the DataPartition, **mics
would be muted**. But: this mutes via the AIC3104 / mic mute
circuit, which sits **between the analog PDM mics and the chip's
PDM decimator**. Muting via X0D30 would silence **all four mics**
which would mean ch0/1 (beamformed-then-output) would also go
silent — which is NOT the jts2 symptom. So we can rule this out.

The amp-enable (`X0D31`) is unrelated to mic data — it's a
speaker output enable.

### 6.6 `BOOT_STATUS` — unverified character semantics

```
BOOT_STATUS = [<char0>, <char1>, <char2>]
```

Observed values:
- JTS production (jts.local) — `'J', 'o', 'F'`

Possible meanings (educated guess; **not confirmed against XMOS
source**):
- `<char0>` may encode the boot source: `'J'` (JTAG-loaded?) vs
  `'S'` (SPI flash?). Capital letters elsewhere in XMOS docs
  often denote enum tags.
- `<char1>` `'o'` — lowercase, could be "ok."
- `<char2>` `'F'` — uppercase, could indicate "Flash" boot
  vs. "Factory" Safe Mode. Or it could be a fault flag we never
  see set on JTS because we always boot from clean state.

**Diagnostic value:** if you have a "broken" chip and a "working"
chip side-by-side and they return different `BOOT_STATUS` values,
that's a useful clue even without knowing what the chars mean.
Treat any value other than `'Jof'` (or whatever the working JTS
returns at the time of the comparison) as suspicious until
verified.

---

## 7. Most likely causes of ch2-5 silence (ranked)

### TL;DR — resolved root cause (2026-05-15 jts2 investigation)

**ALSA kernel mixer state had ch2-5 muted at the host.** Specifically:

```
Headset Capture Switch:  on,on,off,off,off,off
Headset Capture Volume:  60,60,0,0,0,0
```

The chip was healthy, on 6-ch firmware, USB descriptors were correct, `/proc/asound/Array/stream0` showed `Capture Channels: 6` — but the kernel ALSA mixer silently muted ch2-5 before `arecord` could see them. This persists across reboot via `alsactl restore`. The mute almost certainly originated when the chip was on 2-ch firmware (which only exposes ch0/1 to the kernel mixer); after the DFU flash to 6-ch firmware, ALSA created mixer slots for ch2-5 with defaults of off/0, and `alsactl store` later persisted that state.

**Fix:**
```sh
sudo amixer -c Array cset name='Headset Capture Switch' on,on,on,on,on,on
sudo amixer -c Array cset name='Headset Capture Volume' 60,60,60,60,60,60
sudo alsactl store
```

`jasper-aec-reconcile` runs this on every reconcile pass now (via `ensure_capture_mixer_open`), so it self-heals. `jasper-doctor` flags drift under "XVF mixer state". This hypothesis was NOT in the original ranking below because the doc was written before the actual diagnosis landed — keeping the original list as a record of the analytical process and to cover the remaining-uncovered failure modes if this ever happens again with a different root cause.

### Original ranking (pre-resolution)

Given the evidence in the issue prompt:

- Same chip + same 6-ch firmware (`v2.0.8`, hash
  `a1f70651e992d6f0bcff655b26925d33999b9c2d`)
- Ch0/1 have signal, ch2-5 are literal zeros
- `SHF_BYPASS=1` saturates ch0/1 but **ch2-5 still zero**
- `MIC_GAIN=90` (default)
- `SELECTED_CHANNELS=[3,3]` (default)
- `OP_PACKED=[0,0]` (default)
- `BOOT_STATUS='Jof'`
- Works on jts.local with the same firmware

Some of these confirm the silent-channel mechanism cannot be host-controllable parameter state (because ch2-5 routing is fixed in firmware on stock 6-ch — see §3). So the fault is either upstream of the output mux in the chip itself, or on the host-side USB data plane.

Ranked hypotheses, most → least likely:

### 7.1 **USB bandwidth / endpoint truncation at the host** (most likely)

**Why:** The symptom — channels delivered in order, with later
channels truncated to silence — is exactly what happens when a
UAC2 isoc IN endpoint underruns or the host can't keep up with
the negotiated packet size. ch0-1 fit in roughly the same packet
budget as the 2-ch firmware, so they're delivered cleanly; ch2-5
are the "marginal" addition and fail first.

This is consistent with the host being a 1 GB Pi 5 (less DMA
headroom than the 2 GB jts.local) and potentially a different
USB cable / hub topology.

**Diagnostic moves (host side):**

```sh
# 1. Confirm chip is on USB 2.0 High-Speed (480M), not Full-Speed (12M)
lsusb -t | grep -B3 2886
#   Expect "480M" near the chip's row.

# 2. Look for kernel-level USB errors
sudo dmesg -T | grep -i -E 'usb|audio|underrun|xhci|dwc' | tail -100

# 3. Are the bytes actually arriving? Capture raw and check per-channel content
arecord -D plughw:Array,0 -c 6 -r 16000 -f S16_LE -d 5 /tmp/raw6.wav
ffmpeg -i /tmp/raw6.wav -map_channel 0.0.0 /tmp/ch0.wav \
                       -map_channel 0.0.1 /tmp/ch1.wav \
                       -map_channel 0.0.2 /tmp/ch2.wav \
                       -map_channel 0.0.3 /tmp/ch3.wav \
                       -map_channel 0.0.4 /tmp/ch4.wav \
                       -map_channel 0.0.5 /tmp/ch5.wav
for c in 0 1 2 3 4 5; do
    sox /tmp/ch$c.wav -n stat 2>&1 | grep -E 'RMS|Maximum amp'
done

# 4. Try a different USB port (avoid hubs; XVF chip into a Pi USB-A port directly)
# 5. Try a different USB cable (rule out a marginal C-to-A or C-to-C cable)
```

If the symptom moves with the cable or the port, it's USB
bandwidth / electrical and the chip is fine.

### 7.2 **DataPartition has cached state from a prior `SAVE_CONFIGURATION`** (medium-likely)

**Why:** Even though ch2-5 routing is "not host-controllable on
stock 6-ch firmware," that's true only of routing — the
DataPartition can persist GAIN settings (MIC_GAIN, REF_GAIN), AGC
state, SHF bypass, and the AUDIO_MGR_OP_L/R routing for ch0/1.
What it CAN'T directly persist is "ch2-5 muted" (no parameter to
do that). But if the partition is corrupted, the chip's data
plane behaviour is undefined — including potentially silencing
later USB capture slots.

This is most likely if jts2 was previously used for some XMOS
demo (e.g. someone followed the official "Output Selection"
wiki and called `SAVE_CONFIGURATION` to persist a routing
change).

**Diagnostic moves (chip side):**

```sh
# 1. Soft reboot — clears RAM-state parameters
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host REBOOT --values 1
sleep 3
# Then re-test the channels. If ch2-5 wake up after REBOOT 1, the
# fault was in live param state.

# 2. Clear DataPartition (parameter store) — clears any persisted state
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host CLEAR_CONFIGURATION --values 1
sleep 1
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host REBOOT --values 1
sleep 3
# Re-test. If this changes things, the DataPartition had persisted state.

# 3. Nuclear option — wipe DataPartition via DFU recovery (procedure in §5.1).
#    Only do this if 1 and 2 don't help and the chip is otherwise responsive.
#    DO NOT call SAVE_CONFIGURATION on this chip again, regardless of outcome.
```

### 7.3 **The chip's PDM decimator path for mic 0-3 raw output is faulty** (lower likelihood)

The chip has separate data paths from the PDM decimator to (a)
the SHF cores (which feed ch0/1) and (b) the raw-output mux
(which feeds ch2-5 on 6-ch firmware). The fact that ch0/1 work
proves the PDM decimator output is fine for **the SHF input
path**. But the raw-output mux on the 6-ch firmware is its own
data plane — added in v2.0.8 as a new feature — and could in
principle fail without affecting ch0/1.

If this is the case, **`REBOOT 1` would not fix it** (live-state
parameters are not involved). `CLEAR_CONFIGURATION` would not
either. Re-flashing the same firmware **could** fix it (if the
issue is corrupted firmware bits in the run-time partition), or
not (if the chip silicon itself has a latch-up condition the
firmware can't clear).

The most useful diagnostic is **re-flashing v2.0.8 6chl** and
re-testing. If symptom persists, swap to the 2-ch firmware and
verify ch0/1 still work; that confirms the chip is at least
partially fine.

### 7.4 **Different revision of v2.0.8 6chl firmware between the two Pis** (less likely but cheap to rule out)

The prompt says same `BLD_REPO_HASH=a1f70651e992d6f0bcff655b26925d33999b9c2d`
on both Pis. That's a strong signature — same sw_xvf3800 commit.
But XMOS does sometimes ship "same hash, different binary" if
config files change without the source tree changing.

```sh
# Compare the actual .bin files used to flash, byte for byte
ssh pi@jts.local sudo find / -name 'respeaker_xvf3800_*' 2>/dev/null
ssh pi@jts2.local sudo find / -name 'respeaker_xvf3800_*' 2>/dev/null
# Get both files local and `cmp -s` them.
```

If hashes match, this is eliminated.

### 7.5 **PDM mic clock or DC-bias hardware fault on one channel of jts2's mic array** (low likelihood, but possible)

PDM mics on the XVF3800 are arranged so the 4 mics share clock
lines but have independent data lines. A solder defect on the
data line for mics 0-3 would silence all four (since they share
the same PDM clock). BUT — that would also kill the SHF cores'
input, which would kill ch0/1 too. So this is unlikely UNLESS
the chip has a hardware bypass somewhere that runs the SHF
cores from a separate (e.g. simulated / test-tone) input path,
which isn't documented anywhere we've seen.

Lowest priority for investigation, but if everything else is
ruled out, swapping the XVF board between the two Pis is the
definitive A/B test.

### 7.6 Catch-all: kernel/ALSA driver state vs. PortAudio enumeration

It's worth ruling out at the kernel/ALSA layer that the audio
stack actually believes the device has 6 channels. If ALSA only
exposes 2 channels (maybe the kernel UAC2 driver detected the
endpoint as 2-channel for some reason), the "channels 2-5
silent" interpretation may be misleading — they could be
absent entirely.

```sh
# What does the kernel think?
cat /proc/asound/Array/stream0
# Channels: 6   <- required for 6-ch firmware
#   If 2, ALSA hasn't seen the 6-ch endpoint.

# Re-plug the chip and check dmesg for the descriptor parse
sudo dmesg -T | grep -A5 -B2 -i 'XVF3800\|reSpeaker\|2886:001a' | tail -40
```

If `/proc/asound/Array/stream0` shows `Channels: 2` on jts2 while
the chip says it's running 6-ch firmware via `VERSION` and
`BLD_REPO_HASH`, that's a **firmware↔kernel disagreement**, which
points back to either §7.1 (USB bandwidth — the kernel couldn't
allocate ISO endpoints for 6 channels) or §7.4 (different firmware
binary actually running, despite hash match).

---

## 8. Diagnostic cookbook

The minimum set of commands needed to characterise a suspicious
XVF chip. Run all of these on the affected Pi and save the
outputs.

### 8.1 USB / kernel / ALSA layer

```sh
# Identity
lsusb -d 2886:001a
sudo lsusb -v -d 2886:001a 2>/dev/null | head -100

# Speed + topology
lsusb -t | grep -B2 -A1 2886
udevadm info --query=all --name=/dev/snd/controlC$(cat /proc/asound/Array/id 2>/dev/null && echo OK || arecord -l) 2>&1 | head -40

# What ALSA sees
arecord -l | grep -i array
cat /proc/asound/Array/stream0
cat /proc/asound/Array/stream1
amixer -c Array contents 2>&1 | head -100

# Kernel-side audio path
sudo dmesg -T | grep -i -E '2886:001a|reSpeaker|XVF3800|usb-audio|underrun|xhci' | tail -100
```

### 8.2 Chip-side parameter sweep

```sh
PY=/opt/jasper/.venv/bin/python
M='-m jasper.xvf.xvf_host'

for p in VERSION BLD_MSG BLD_HOST BLD_REPO_HASH BLD_MODIFIED BOOT_STATUS \
         USB_BIT_DEPTH \
         AEC_NUM_MICS AEC_NUM_FARENDS AEC_MIC_ARRAY_TYPE \
         SHF_BYPASS AEC_AECCONVERGED AEC_HPFONOFF \
         AUDIO_MGR_MIC_GAIN AUDIO_MGR_REF_GAIN \
         AUDIO_MGR_SELECTED_CHANNELS \
         AUDIO_MGR_OP_PACKED AUDIO_MGR_OP_UPSAMPLE \
         AUDIO_MGR_OP_L AUDIO_MGR_OP_R \
         AUDIO_MGR_OP_ALL \
         AUDIO_MGR_FAR_END_DSP_ENABLE \
         AUDIO_MGR_SYS_DELAY \
         I2S_INACTIVE I2S_DAC_DSP_ENABLE \
         GPO_READ_VALUES \
         LED_EFFECT LED_BRIGHTNESS; do
    echo "----- $p -----"
    sudo $PY $M $p 2>&1 | sed -n '/Done!\|Error/!p'
done
```

(`GPI_READ_VALUES` is not in the vendored `PARAMETERS` table —
the host_control C app has it but the vendored Python tool
doesn't; if you need to read the mute-button state from Python,
add it to `PARAMETERS` first or use the C `xvf_host` directly.)

The expected output on a healthy chip:

| Parameter | Healthy 6-ch firmware value |
|---|---|
| `VERSION` | `[2, 0, 8]` |
| `BLD_REPO_HASH` | `'a1f70651e992d6f0bcff655b26925d33999b9c2d'` |
| `BOOT_STATUS` | `'Jof'` (observed on JTS — treat as baseline; meaning unverified) |
| `USB_BIT_DEPTH` | `[16, 16]` |
| `AEC_NUM_MICS` | `4` |
| `AEC_NUM_FARENDS` | `1` |
| `SHF_BYPASS` | `0` |
| `AUDIO_MGR_MIC_GAIN` | `90.0` |
| `AUDIO_MGR_REF_GAIN` | `8.0` |
| `AUDIO_MGR_SELECTED_CHANNELS` | `[3, 3]` |
| `AUDIO_MGR_OP_PACKED` | `[0, 0]` |
| `AUDIO_MGR_OP_UPSAMPLE` | `[0, 0]` |
| `AUDIO_MGR_SYS_DELAY` | `12` |

Anything diverging from these on the broken chip is a clue.

### 8.3 Per-channel RMS / activity check

```sh
# Capture all 6 channels for 5 seconds
arecord -D plughw:Array,0 -c 6 -r 16000 -f S16_LE -d 5 /tmp/xvf6.wav

# Demux and stat each channel
mkdir -p /tmp/xvfch
for c in 0 1 2 3 4 5; do
    ffmpeg -y -i /tmp/xvf6.wav -map_channel 0.0.$c /tmp/xvfch/ch$c.wav 2>/dev/null
    rms=$(sox /tmp/xvfch/ch$c.wav -n stats 2>&1 | grep 'RMS lev dB' | awk '{print $4}')
    max=$(sox /tmp/xvfch/ch$c.wav -n stats 2>&1 | grep 'Max level' | awk '{print $3}')
    echo "ch$c: RMS_dB=$rms  max=$max"
done
```

A "dead" channel will show `RMS_dB=-inf` and `max=0.000`. A
"live" but quiet channel will still have a measurable RMS even
in a silent room (mic self-noise / preamp noise, typically
−60 to −80 dBFS).

### 8.4 The killshot test — try the same XVF board on the working Pi

If `jts2` continues to show ch2-5 silent after the diagnostics
above, **physically move the XVF board from jts2 to jts1** and
re-run §8.3. If the symptom moves with the board, the chip /
board is at fault. If the symptom stays on jts2 (now with the
working Pi's XVF board), the host is at fault (likely USB
electrical or kernel).

This is the cleanest A/B and is worth the disruption.

---

## 9. The library we use, and how to extend it

`jasper/xvf/xvf_host.py` is a verbatim vendored copy of
`python_control/xvf_host.py` from the upstream repo. We vendor
rather than clone-at-install for the reasons documented in
`jasper/xvf/__init__.py`. To call any parameter:

```sh
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host --list
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host VERSION
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_MIC_GAIN
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_OP_L --values 8 0
```

The `PARAMETERS` table at the top of the file is the contract
with the firmware — diff carefully when re-vendoring after an
upstream firmware update, because a misaligned (resid, cmdid)
will silently write to the wrong subsystem.

Updating procedure: clone upstream, `diff` the
`python_control/xvf_host.py` against ours, port any new entries
to our vendored file, leave the rest alone. The only
JTS-specific modifications today are the comment at the top —
no algorithm changes.

---

## 10. What we still don't know

For future sessions: items where upstream docs are silent and
where our local knowledge is incomplete or unverified.

- **`BOOT_STATUS` character meanings.** Need either a fault-state
  comparison or an XMOS source-code release to decode definitively.
- **`AUDIO_MGR_OP_PACKED` exact bit layout.** Useful only if
  someone wants to use packed mode; we don't.
- **Whether `SAVE_CONFIGURATION` is fixed in 2.0.7 / 2.0.8.**
  Upstream comments don't confirm; treat as still-brick-hazardous
  on every firmware version.
- **`AUDIO_MGR_MIC_GAIN=90` is "high" — is it the maximum, near
  the recommended ceiling, or just a Seeed default?** Range is
  not documented upstream. Practical implication: changing it is
  fine because the chip clamps internally, but we don't know
  *which* value would actually clip vs *which* would just be
  marginally hot.
- **Whether the 6-ch firmware's ch2-5 path has any host-reachable
  enable/disable parameter.** We've ruled out the documented
  ones (§4); could there be an undocumented one? Plausible but
  unverified. Inspecting the XMOS user guide §4 / §5 in full
  (the parts we couldn't fetch through anti-bot protection)
  might surface one — sourcing the PDF through a different
  channel is the next step.

---

## 11. Sources cited in this document

In rough order of how often we reach for each:

- **Vendored code**: `jasper/xvf/xvf_host.py` (parameter table),
  `jasper/cli/aec_init.py`, `jasper/cli/aec_bridge.py`
  (operational use of the parameters).
- **Local docs**: [`docs/HANDOFF-aec.md`](HANDOFF-aec.md) (chip-side
  AEC investigation), [`BRINGUP.md`](../BRINGUP.md) Phase 2A.5
  (DFU procedure — but flagged `-a 0` typo).
- **Upstream repo**: [`respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY),
  particularly:
  - [`host_control/README.md`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md) — most complete published parameter reference, especially the AUDIO_MGR_OP table.
  - [`xmos_firmwares/dfu_guide.md`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/xmos_firmwares/dfu_guide.md) — DFU flashing.
  - [`xmos_firmwares/usb/`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb), [`xmos_firmwares/i2s/`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/i2s), [`xmos_firmwares/recover/`](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/recover) — firmware binaries.
  - Issues — particularly [#8](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/8) (SAVE_CONFIGURATION brick + recovery), [#6](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/6) (no 48 kHz), [#16](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/16) (DOA stale), [#15](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/15) (no public defaults).
- **Seeed wiki**:
  - [Getting Started with reSpeaker XVF3800 USB Mic Array](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/) — channel layout per firmware, Safe Mode entry, hardware overview.
- **XMOS published documentation** (anti-bot blocked from direct fetch; references included for source attribution):
  - [XMOS XVF3800 v3.2.1 User Guide PDF](https://www.xmos.com/documentation/XM-014888-PC/pdf/xvf3800_user_guide_v3.2.1.pdf) — Table 3.2 categories, §3.5 audio pipeline, §4.2.1 AEC_FAR_EXTGAIN.
  - [Tuning the Application HTML](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/04_tuning_the_application.html) — system-delay, echo-suppression tuning.
  - [Voice Processing Pipeline HTML](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html) — beamformer + DoA architecture.
  - [APPENDIX – Control Commands HTML](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/AA_control_command_appendix.html) — natively-supported commands.
- **Other**:
  - [DeepWiki summary of respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY](https://deepwiki.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY) — used as a cross-check, less authoritative than the upstream docs.

---

Last verified: 2026-05-31 (production OP_R non-silent routing plus corpus-only chip-AEC routing restore/readback rechecked)
