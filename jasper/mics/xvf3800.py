"""Seeed ReSpeaker XVF3800 (USB UA variant) — mic profile.

Canonical reference: docs/HANDOFF-xvf3800.md (hardware identity,
parameter space, firmware variants, failure modes).

Chip control library: jasper/xvf/xvf_host.py (vendored from
respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY upstream — used for
chip-side parameter reads/writes and the REBOOT path).

This module holds the mic-family-specific knowledge consulted by
doctor checks, the AEC bridge, and operator tooling. The bash
reconciler at deploy/bin/jasper-aec-reconcile carries its own
copies (it can't import Python); when changing constants here,
update there too.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------

USB_VID_PID = "2886:001a"
DISPLAY_NAME = "Seeed ReSpeaker XVF3800 (USB UA)"

# ALSA card name as enumerated by snd-usb-audio (the kernel's literal
# `id` field). Stable across reboots and across the 2-ch / 6-ch
# firmware variants — both expose the same iProduct string.
ALSA_CARD_NAME = "Array"


# ---------------------------------------------------------------------
# Firmware variants
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class FirmwareVariant:
    bld_msg: str               # BLD_MSG string the chip reports (xvf_host BLD_MSG)
    capture_channels: int      # USB capture endpoint channel count
    raw_mic_indices: tuple[int, ...]  # capture channels carrying raw PDM mic data


# The two USB firmware variants Seeed publishes. Both share the same
# repo hash and chip silicon — the 6-ch variant just adds the raw-mic
# capture channels needed by the software AEC bridge.
VARIANT_2CH = FirmwareVariant(
    bld_msg="ua-io16-sqr",
    capture_channels=2,
    raw_mic_indices=(),
)
VARIANT_6CH = FirmwareVariant(
    bld_msg="ua-io16-6ch-sqr",
    capture_channels=6,
    raw_mic_indices=(2, 3, 4, 5),
)

# Required for the reconciler-managed XVF AEC profiles. The bridge opens
# the 6-channel capture shape for both chip-AEC (fixed beams on ch0/1)
# and the software-AEC fallback (raw-ish ch1 plus raw mic legs).
RECOMMENDED_FIRMWARE = VARIANT_6CH


# ---------------------------------------------------------------------
# DFU re-flash
# ---------------------------------------------------------------------

# The XVF3800 supports in-system DFU upgrade — the chip in normal
# runtime mode exposes a USB DFU interface (Application Specific
# class 254) alongside its audio class, at interface 4 alt 1.
# `dfu-util` can write to this interface while the chip is running
# normally; no button combo or "Safe Mode" entry is required for
# routine firmware upgrades. Confirmed empirically via `lsusb -v`
# on both jts and jts2 chips (2026-05-15). The button-combo
# procedure on the Seeed wiki is for Safe Mode recovery only —
# used when the DataPartition is corrupted (see HANDOFF-xvf3800.md
# §5.1 for the recovery flow).
#
# When the chip enters DFU during a flash it briefly enumerates as
# the XMOS bootloader at 20b1:0008, then resets back to its normal
# 2886:001a runtime identity after the flash completes.
DFU_VID_PID = "20b1:0008"

# Alt 0 is the read-only Factory partition; alt 1 is the Upgrade
# partition where firmware actually gets written. Writes to alt 0
# silently no-op (the chip stays on whatever firmware it had).
# See HANDOFF-xvf3800.md §2.4 for the alt-setting table.
DFU_ALT_SETTING = 1

# ---------------------------------------------------------------------
# Known-good firmware (snapshot, not a hard pin)
# ---------------------------------------------------------------------
#
# The 6-channel firmware variant JTS has tested with. This is a
# point-in-time snapshot of what we know works — not a contract
# that forbids upgrades. When a newer 6-channel variant ships
# upstream:
#
#   1. Browse FIRMWARE_UPSTREAM_DIR_URL for newer entries.
#   2. Read the changelog/PRs against what JTS depends on —
#      channel 0 = Conference, channel 1 = ASR, channels 2-5 = raw
#      mic data feeding the AEC bridge. If the upgrade preserves
#      those, it should drop into JTS by bumping the three
#      constants below (filename + repo hash + date).
#   3. After flashing, verify with `jasper-doctor` that the
#      "XVF firmware 6-ch" and "AEC bridge service" checks both
#      come back green.
#
# Doctor reads the running chip's capture-channel count from the
# kernel, not from these constants, so it'll continue to flag
# 6-ch correctly even if we forget to update them after a flash.
# Updating them keeps the doc references + remediation messages
# accurate.
FIRMWARE_KNOWN_GOOD_AS_OF = "2026-05-15"
FIRMWARE_BLOB_6CH = "respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin"
# Built from sw_xvf3800 commit `a1f70651e992d6f0bcff655b26925d33999b9c2d`.
# The chip reports this via `xvf_host BLD_REPO_HASH` — useful to
# verify after a flash that you actually wrote what you intended.
FIRMWARE_KNOWN_GOOD_BLD_REPO_HASH = "a1f70651e992d6f0bcff655b26925d33999b9c2d"

# Upstream firmware directory. Single canonical source for blobs.
FIRMWARE_UPSTREAM_DIR_URL = (
    "https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY"
    "/tree/master/xmos_firmwares/usb"
)
FIRMWARE_RAW_URL_6CH = (
    "https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY"
    f"/raw/master/xmos_firmwares/usb/{FIRMWARE_BLOB_6CH}"
)


# ---------------------------------------------------------------------
# ALSA mixer invariants
# ---------------------------------------------------------------------

# When the chip is flashed from 2-ch to 6-ch firmware mid-bringup,
# ALSA assigns new per-channel mixer slots for ch2-5 with defaults
# of off / 0. `alsactl restore` then persists that silently across
# reboot, killing the raw mics in spite of correct chip-side state.
# `ensure_capture_open()` resets both controls to known-good values;
# the reconciler calls it on every pass to self-heal.
#
# These names are looked up via `amixer -c <card> cset name='...'`
# (cset, not get — these controls aren't in any aggregated "simple
# control" group, so the plain `amixer set` form misses them).
MIXER_CAPTURE_SWITCH = "Headset Capture Switch"
MIXER_CAPTURE_VOLUME = "Headset Capture Volume"
MIXER_VOLUME_MAX = 60  # ALSA units; 0=-60 dB, 60=0 dB on this device


# ---------------------------------------------------------------------
# AEC bridge wiring
# ---------------------------------------------------------------------

# Mic channel the bridge captures from for sw AEC's near-end input.
# Index into the 6-ch capture endpoint:
#   0 = Conference (chip BF + NS + AGC + HPF, comms-tuned)
#   1 = ASR        (chip BF + NS + AGC + HPF, speech-recognition-tuned)
#   2 = Raw mic 0  (pre-everything — no BF, no NS, no AGC, no HPF)
#   3-5 = Raw mics 1-3
#
# We use channel 1 for the software-AEC fallback because it is the
# canonical XVF3800 voice-assistant capture channel — used by Seeed's own
# examples, the Reachy Mini stack, and the formatBCE/ESPHome integration.
# In that fallback profile, jasper-aec-init writes `SHF_BYPASS=1` because
# the chip's AEC pipeline is incompatible unless the outputd USB-IN
# reference path is armed. Empirically that also bypasses the chip SHF
# post-processing path on channels 0/1, so this is a raw-ish input rather
# than a beamformed / NS / AGC output. Software AEC3 then runs host-side.
# In chip-AEC mode, the bridge captures ch0/ch1 as fixed 150/210 ASR
# beams instead and forwards the selected beam directly.
#
# Was previously channel 2 (raw mic 0). The switch was made on
# 2026-05-15 after measuring that raw mic 0 has literally no chip
# processing (verified by toggling chip NS/AGC and observing 0.4 dB
# of variation on ch 2 vs 8+ dB on ch 0/1) — we were paying for the
# chip's DSP and not using it. See HANDOFF-xvf3800.md §3.
MIC_CHANNEL_INDEX = 1


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def is_present() -> bool:
    """True if the chip's ALSA card has enumerated under /proc/asound."""
    return Path(f"/proc/asound/{ALSA_CARD_NAME}/stream0").exists()


def capture_channels() -> int | None:
    """Return the chip's USB capture endpoint channel count from
    /proc/asound/<card>/stream0, or None if the card is absent.

    Pinned to the ^Capture: section — /proc/asound/<card>/stream0
    has Playback first (Channels: 2 for the XVF chip's playback
    endpoint) then Capture (Channels: 6 on 6-ch firmware). A naive
    `grep Channels:` returns the Playback value, which was the May
    2026 reconciler bug that silently disabled software AEC."""
    p = Path(f"/proc/asound/{ALSA_CARD_NAME}/stream0")
    if not p.exists():
        return None
    in_capture = False
    for line in p.read_text().split("\n"):
        if line.startswith("Capture:"):
            in_capture = True
            continue
        if in_capture and "Channels:" in line:
            try:
                return int(line.split("Channels:", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                return None
    return None


def is_recommended_firmware() -> bool:
    """True if the chip is on the 6-ch firmware variant — the one the
    software AEC bridge needs."""
    return capture_channels() == RECOMMENDED_FIRMWARE.capture_channels


def ensure_capture_open() -> bool:
    """Reset capture switch + volume to known-good values, then
    `alsactl store`. Idempotent — safe to call on every reconcile
    pass. Returns True if the commands succeeded, False otherwise.

    Caller is responsible for sudo: this runs `amixer` directly with
    no privilege escalation. The reconciler invokes us as root."""
    on = ",".join(["on"] * RECOMMENDED_FIRMWARE.capture_channels)
    max_vol = ",".join([str(MIXER_VOLUME_MAX)] * RECOMMENDED_FIRMWARE.capture_channels)
    try:
        subprocess.run(
            ["amixer", "-c", ALSA_CARD_NAME, "cset",
             f"name={MIXER_CAPTURE_SWITCH}", on],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(
            ["amixer", "-c", ALSA_CARD_NAME, "cset",
             f"name={MIXER_CAPTURE_VOLUME}", max_vol],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(["alsactl", "store"], check=False, timeout=5)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def dfu_flash_command(firmware_path: str = "") -> str:
    """Return the canonical DFU flash command as a string. Useful for
    doctor remediation messages and BRINGUP cross-references.

    The chip exposes its DFU interface in normal runtime mode, so the
    command runs against the chip as-plugged-in — no Safe Mode entry
    or button combo required. `-R` resets the chip to runtime after
    flashing; `-e` detaches from DFU before download (harmless and
    required on some host stacks)."""
    blob = firmware_path or FIRMWARE_BLOB_6CH
    return f"sudo dfu-util -R -e -a {DFU_ALT_SETTING} -D {blob}"
