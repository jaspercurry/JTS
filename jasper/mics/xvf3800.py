# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Seeed ReSpeaker XVF3800 (USB UA variant) — mic profile.

Canonical reference: docs/HANDOFF-xvf3800.md (hardware identity,
parameter space, firmware variants, failure modes).

Chip control library: jasper/xvf/xvf_host.py (JTS-owned USB
vendor-control helper used for chip-side parameter reads/writes).

This module holds the mic-family-specific knowledge consulted by
doctor checks, the AEC bridge, and operator tooling. The bash
reconciler consumes these facts through `python -m jasper.cli.xvf_profile`
so geometry/channel truth stays in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import pi
from pathlib import Path
from typing import Any, Mapping


# ---------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------

USB_VID_PID = "2886:001a"
FLEX_USB_VID_PID = "2886:0022"
USB_VID_PIDS = (USB_VID_PID, FLEX_USB_VID_PID)
DISPLAY_NAME = "Seeed ReSpeaker XVF3800 (USB UA/Flex)"

# ALSA card names as enumerated by snd-usb-audio (the kernel's literal
# `id` field). The legacy square USB firmware enumerates as `Array`;
# the ReSpeaker Flex linear/circular USB firmware enumerates by firmware
# family, e.g. `L16K6Ch` for the 16 kHz linear 6-channel build.
ALSA_CARD_NAME = "Array"
FLEX_LINEAR_2CH_ALSA_CARD_NAME = "L16K2Ch"
FLEX_LINEAR_ALSA_CARD_NAME = "L16K6Ch"
FLEX_CIRCULAR_2CH_ALSA_CARD_NAME = "C16K2Ch"
FLEX_CIRCULAR_ALSA_CARD_NAME = "C16K6Ch"
ALSA_CARD_NAMES = (
    ALSA_CARD_NAME,
    FLEX_LINEAR_2CH_ALSA_CARD_NAME,
    FLEX_LINEAR_ALSA_CARD_NAME,
    FLEX_CIRCULAR_2CH_ALSA_CARD_NAME,
    FLEX_CIRCULAR_ALSA_CARD_NAME,
)


# ---------------------------------------------------------------------
# Firmware variants
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ChipBeamLeg:
    """One chip-emitted hardware-AEC beam stream.

    token is the frozen JTS wake-leg/corpus identifier. It is not a UI
    label and must not be re-used for a different physical beam plan.
    """

    token: str
    channel_index: int
    azimuth_deg: float
    label: str
    elevation_deg: float = 0.0

    @property
    def azimuth_rad(self) -> float:
        return self.azimuth_deg * pi / 180.0

    @property
    def elevation_rad(self) -> float:
        return self.elevation_deg * pi / 180.0


@dataclass(frozen=True)
class ChipBeamPlan:
    """Geometry-specific XVF chip beam configuration.

    This is the line between "the chip can produce some processed
    channels" and "JTS is allowed to label/use those channels as a
    production wake profile." Flex linear intentionally has no production
    plan yet; corpus evidence can add one later without changing the
    square-board contract.
    """

    plan_id: str
    display_name: str
    geometry: str
    description: str
    legs: tuple[ChipBeamLeg, ...]
    production_validated: bool = True

    @property
    def leg_tokens(self) -> tuple[str, ...]:
        return tuple(leg.token for leg in self.legs)


@dataclass(frozen=True)
class FirmwareVariant:
    variant_id: str            # Stable JTS id for runtime state/artifacts
    display_name: str
    bld_msg: str               # BLD_MSG string the chip reports (xvf_host BLD_MSG)
    capture_channels: int      # USB capture endpoint channel count
    raw_mic_indices: tuple[int, ...]  # capture channels carrying raw PDM mic data
    geometry: str              # square/linear/circular; beams/DoA are geometry-specific
    usb_vid_pid: str = USB_VID_PID
    alsa_card_name: str = ALSA_CARD_NAME
    chip_beam_plan_id: str | None = None


@dataclass(frozen=True)
class RuntimeProfile:
    """Detected XVF runtime facts used by reconcilers and status surfaces."""

    present: bool
    variant: FirmwareVariant | None
    alsa_card_name: str
    capture_channels: int | None
    chip_beam_plan: ChipBeamPlan | None
    reason: str

    @property
    def variant_id(self) -> str:
        return self.variant.variant_id if self.variant else ""

    @property
    def display_name(self) -> str:
        return self.variant.display_name if self.variant else DISPLAY_NAME

    @property
    def geometry(self) -> str:
        return self.variant.geometry if self.variant else ""

    @property
    def chip_beam_plan_id(self) -> str:
        return self.chip_beam_plan.plan_id if self.chip_beam_plan else ""

    @property
    def chip_aec_supported(self) -> bool:
        return bool(self.chip_beam_plan and self.chip_beam_plan.production_validated)

    @property
    def recommended_profile(self) -> str:
        if self.present and self.capture_channels == RECOMMENDED_CAPTURE_CHANNELS:
            return "xvf_chip_aec" if self.chip_aec_supported else "xvf_software_aec3"
        return "direct_mic"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "present": self.present,
            "variant_id": self.variant_id,
            "display_name": self.display_name,
            "geometry": self.geometry,
            "usb_vid_pid": self.variant.usb_vid_pid if self.variant else "",
            "alsa_card_name": self.alsa_card_name,
            "capture_channels": self.capture_channels,
            "recommended_capture_channels": RECOMMENDED_CAPTURE_CHANNELS,
            "raw_mic_indices": (
                list(self.variant.raw_mic_indices) if self.variant else []
            ),
            "chip_beam_plan": (
                {
                    "id": self.chip_beam_plan.plan_id,
                    "display_name": self.chip_beam_plan.display_name,
                    "geometry": self.chip_beam_plan.geometry,
                    "production_validated": self.chip_beam_plan.production_validated,
                    "legs": [
                        {
                            "token": leg.token,
                            "channel_index": leg.channel_index,
                            "azimuth_deg": leg.azimuth_deg,
                            "elevation_deg": leg.elevation_deg,
                            "label": leg.label,
                        }
                        for leg in self.chip_beam_plan.legs
                    ],
                }
                if self.chip_beam_plan else None
            ),
            "chip_aec_supported": self.chip_aec_supported,
            "recommended_profile": self.recommended_profile,
            "reason": self.reason,
        }


RECOMMENDED_CAPTURE_CHANNELS = 6


@dataclass(frozen=True)
class FirmwareUpdateTarget:
    """One firmware update JTS can perform without guessing geometry."""

    target_id: str
    from_variant_ids: tuple[str, ...]
    to_variant_id: str
    label: str
    geometry: str
    filename: str
    url: str
    sha256: str
    expected_size_bytes: int
    upstream_dir_url: str
    expected_capture_channels: int = RECOMMENDED_CAPTURE_CHANNELS

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.target_id,
            "from_variant_ids": list(self.from_variant_ids),
            "to_variant_id": self.to_variant_id,
            "label": self.label,
            "geometry": self.geometry,
            "filename": self.filename,
            "url": self.url,
            "sha256": self.sha256,
            "expected_size_bytes": self.expected_size_bytes,
            "upstream_dir_url": self.upstream_dir_url,
            "expected_capture_channels": self.expected_capture_channels,
            "dfu_alt_setting": DFU_ALT_SETTING,
        }


SQUARE_FIXED_150_210_PLAN = ChipBeamPlan(
    plan_id="xvf_square_fixed_150_210",
    display_name="Square/circular fixed 150/210 ASR beams",
    geometry="square",
    description=(
        "Legacy square/circular XVF3800 USB 6-channel chip-AEC plan. "
        "Channels 0/1 carry the fixed 150° and 210° ASR beams."
    ),
    legs=(
        ChipBeamLeg("chip_aec_150", channel_index=0, azimuth_deg=150.0,
                    label="Chip AEC ASR 150"),
        ChipBeamLeg("chip_aec_210", channel_index=1, azimuth_deg=210.0,
                    label="Chip AEC ASR 210"),
    ),
)
CHIP_BEAM_PLANS: dict[str, ChipBeamPlan] = {
    SQUARE_FIXED_150_210_PLAN.plan_id: SQUARE_FIXED_150_210_PLAN,
}


# The USB firmware variants JTS has validated. The 6-ch variants add
# the raw-mic capture channels needed by the software AEC bridge.
VARIANT_2CH = FirmwareVariant(
    variant_id="xvf3800_legacy_square_2ch",
    display_name="Legacy square/circular XVF3800 USB 2-channel",
    bld_msg="ua-io16-sqr",
    capture_channels=2,
    raw_mic_indices=(),
    geometry="square",
)
VARIANT_6CH = FirmwareVariant(
    variant_id="xvf3800_legacy_square_6ch",
    display_name="Legacy square/circular XVF3800 USB 6-channel",
    bld_msg="ua-io16-6ch-sqr",
    capture_channels=6,
    raw_mic_indices=(2, 3, 4, 5),
    geometry="square",
    chip_beam_plan_id=SQUARE_FIXED_150_210_PLAN.plan_id,
)
VARIANT_FLEX_LINEAR_2CH = FirmwareVariant(
    variant_id="xvf3800_flex_linear_2ch",
    display_name="ReSpeaker Flex XVF3800 LINEAR-4 16 kHz 2-channel",
    bld_msg="ua-io16-2ch-lin",
    capture_channels=2,
    raw_mic_indices=(),
    geometry="linear",
    usb_vid_pid=FLEX_USB_VID_PID,
    alsa_card_name=FLEX_LINEAR_2CH_ALSA_CARD_NAME,
)
VARIANT_FLEX_LINEAR_6CH = FirmwareVariant(
    variant_id="xvf3800_flex_linear_6ch",
    display_name="ReSpeaker Flex XVF3800 LINEAR-4 16 kHz 6-channel",
    bld_msg="ua-io16-6ch-lin",
    capture_channels=6,
    raw_mic_indices=(2, 3, 4, 5),
    geometry="linear",
    usb_vid_pid=FLEX_USB_VID_PID,
    alsa_card_name=FLEX_LINEAR_ALSA_CARD_NAME,
)
VARIANT_FLEX_CIRCULAR_2CH = FirmwareVariant(
    variant_id="xvf3800_flex_circular_2ch",
    display_name="ReSpeaker Flex XVF3800 Circular-4 16 kHz 2-channel",
    bld_msg="ua-io16-2ch-cir",
    capture_channels=2,
    raw_mic_indices=(),
    geometry="circular",
    usb_vid_pid=FLEX_USB_VID_PID,
    alsa_card_name=FLEX_CIRCULAR_2CH_ALSA_CARD_NAME,
)
VARIANT_FLEX_CIRCULAR_6CH = FirmwareVariant(
    variant_id="xvf3800_flex_circular_6ch",
    display_name="ReSpeaker Flex XVF3800 Circular-4 16 kHz 6-channel",
    bld_msg="ua-io16-6ch-cir",
    capture_channels=6,
    raw_mic_indices=(2, 3, 4, 5),
    geometry="circular",
    usb_vid_pid=FLEX_USB_VID_PID,
    alsa_card_name=FLEX_CIRCULAR_ALSA_CARD_NAME,
)

# Required for the reconciler-managed XVF AEC profiles. The bridge opens
# the 6-channel capture shape for both chip-AEC (fixed beams on ch0/1)
# and the software-AEC fallback (raw-ish ch1 plus raw mic legs).
RECOMMENDED_FIRMWARE = VARIANT_6CH
SUPPORTED_6CH_FIRMWARE = (
    VARIANT_6CH,
    VARIANT_FLEX_LINEAR_6CH,
    VARIANT_FLEX_CIRCULAR_6CH,
)
FIRMWARE_VARIANTS = (
    VARIANT_2CH,
    VARIANT_6CH,
    VARIANT_FLEX_LINEAR_2CH,
    VARIANT_FLEX_LINEAR_6CH,
    VARIANT_FLEX_CIRCULAR_2CH,
    VARIANT_FLEX_CIRCULAR_6CH,
)
VARIANTS_BY_BLD_MSG = {variant.bld_msg: variant for variant in FIRMWARE_VARIANTS}
VARIANTS_BY_ID = {variant.variant_id: variant for variant in FIRMWARE_VARIANTS}


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
# the XMOS bootloader at 20b1:0008, then resets back to its runtime
# identity after the flash completes: 2886:001a for the legacy square
# firmware, 2886:0022 for Flex firmware.
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
FIRMWARE_KNOWN_GOOD_SIZE_BYTES = 933888
FIRMWARE_BLOB_FLEX_LINEAR_6CH = "respeaker_flex_usb_l16k6ch_v1.0.1.bin"
FIRMWARE_BLOB_FLEX_CIRCULAR_6CH = "respeaker_flex_usb_c16k6ch_v1.0.1.bin"
FIRMWARE_KNOWN_GOOD_SHA256 = (
    "8dd27762ebd87a28f0b4546f1634ece5e7eae308375d66952f7a9e3fb948266a"
)
# Built from sw_xvf3800 commit `a1f70651e992d6f0bcff655b26925d33999b9c2d`.
# The chip reports this via `xvf_host BLD_REPO_HASH` — useful to
# verify after a flash that you actually wrote what you intended.
FIRMWARE_KNOWN_GOOD_BLD_REPO_HASH = "a1f70651e992d6f0bcff655b26925d33999b9c2d"
FIRMWARE_FLEX_KNOWN_GOOD_AS_OF = "2026-06-29"
FIRMWARE_FLEX_KNOWN_GOOD_SIZE_BYTES = 929792
FIRMWARE_FLEX_LINEAR_KNOWN_GOOD_SHA256 = (
    "85743239b4c4b069fb153b4a23f29dde9c29f34768b47601fa92daaaf09f2a99"
)
FIRMWARE_FLEX_CIRCULAR_KNOWN_GOOD_SHA256 = (
    "731e3ff77f092dbf301db41f652f02fee762ed634e80bd443811771c76f75af7"
)

# Upstream firmware directory. Single canonical source for blobs.
FIRMWARE_UPSTREAM_DIR_URL = (
    "https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY"
    "/tree/master/xmos_firmwares/usb"
)
FIRMWARE_RAW_URL_6CH = (
    "https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY"
    f"/raw/master/xmos_firmwares/usb/{FIRMWARE_BLOB_6CH}"
)
FIRMWARE_UPSTREAM_FLEX_DIR_URL = (
    "https://github.com/respeaker/reSpeaker_Flex/tree/main/xmos_firmwares/usb"
)
FIRMWARE_RAW_URL_FLEX_LINEAR_6CH = (
    "https://github.com/respeaker/reSpeaker_Flex"
    f"/raw/main/xmos_firmwares/usb/{FIRMWARE_BLOB_FLEX_LINEAR_6CH}"
)
FIRMWARE_RAW_URL_FLEX_CIRCULAR_6CH = (
    "https://github.com/respeaker/reSpeaker_Flex"
    f"/raw/main/xmos_firmwares/usb/{FIRMWARE_BLOB_FLEX_CIRCULAR_6CH}"
)


FIRMWARE_UPDATE_TARGETS = (
    FirmwareUpdateTarget(
        target_id="legacy_square_6ch",
        from_variant_ids=(VARIANT_2CH.variant_id,),
        to_variant_id=VARIANT_6CH.variant_id,
        label="Legacy square/circular XVF3800 USB 6-channel",
        geometry="square",
        filename=FIRMWARE_BLOB_6CH,
        url=FIRMWARE_RAW_URL_6CH,
        sha256=FIRMWARE_KNOWN_GOOD_SHA256,
        expected_size_bytes=FIRMWARE_KNOWN_GOOD_SIZE_BYTES,
        upstream_dir_url=FIRMWARE_UPSTREAM_DIR_URL,
    ),
    FirmwareUpdateTarget(
        target_id="flex_linear_6ch",
        from_variant_ids=(VARIANT_FLEX_LINEAR_2CH.variant_id,),
        to_variant_id=VARIANT_FLEX_LINEAR_6CH.variant_id,
        label="ReSpeaker Flex LINEAR-4 16 kHz 6-channel",
        geometry="linear",
        filename=FIRMWARE_BLOB_FLEX_LINEAR_6CH,
        url=FIRMWARE_RAW_URL_FLEX_LINEAR_6CH,
        sha256=FIRMWARE_FLEX_LINEAR_KNOWN_GOOD_SHA256,
        expected_size_bytes=FIRMWARE_FLEX_KNOWN_GOOD_SIZE_BYTES,
        upstream_dir_url=FIRMWARE_UPSTREAM_FLEX_DIR_URL,
    ),
    FirmwareUpdateTarget(
        target_id="flex_circular_6ch",
        from_variant_ids=(VARIANT_FLEX_CIRCULAR_2CH.variant_id,),
        to_variant_id=VARIANT_FLEX_CIRCULAR_6CH.variant_id,
        label="ReSpeaker Flex Circular-4 16 kHz 6-channel",
        geometry="circular",
        filename=FIRMWARE_BLOB_FLEX_CIRCULAR_6CH,
        url=FIRMWARE_RAW_URL_FLEX_CIRCULAR_6CH,
        sha256=FIRMWARE_FLEX_CIRCULAR_KNOWN_GOOD_SHA256,
        expected_size_bytes=FIRMWARE_FLEX_KNOWN_GOOD_SIZE_BYTES,
        upstream_dir_url=FIRMWARE_UPSTREAM_FLEX_DIR_URL,
    ),
)
FIRMWARE_UPDATE_TARGETS_BY_ID = {
    target.target_id: target for target in FIRMWARE_UPDATE_TARGETS
}


# ---------------------------------------------------------------------
# ALSA mixer invariants
# ---------------------------------------------------------------------

# When the chip is flashed from 2-ch to 6-ch firmware mid-bringup,
# ALSA assigns new per-channel mixer slots for ch2-5 with defaults
# of off / 0. `alsactl restore` then persists that silently across
# reboot, killing the raw mics in spite of correct chip-side state.
# `deploy/bin/jasper-aec-reconcile::ensure_capture_mixer_open` resets both
# controls to known-good values before arming the profile-managed six-channel
# AEC path. The constants below are the canonical data source for that
# Bash-owned repair; cross-language tests keep the duplicated shell literals
# in sync.
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

def _capture_channels_for_card(
    card: str,
    *,
    asound_root: Path = Path("/proc/asound"),
) -> int | None:
    p = asound_root / card / "stream0"
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


def variant_for_bld_msg(bld_msg: str | None) -> FirmwareVariant | None:
    value = (bld_msg or "").strip().strip("'\"")
    return VARIANTS_BY_BLD_MSG.get(value)


def variant_for_card(
    card: str,
    capture_channel_count: int | None,
) -> FirmwareVariant | None:
    if card == FLEX_LINEAR_2CH_ALSA_CARD_NAME and capture_channel_count == 2:
        return VARIANT_FLEX_LINEAR_2CH
    if card == FLEX_LINEAR_ALSA_CARD_NAME and capture_channel_count == 6:
        return VARIANT_FLEX_LINEAR_6CH
    if card == FLEX_CIRCULAR_2CH_ALSA_CARD_NAME and capture_channel_count == 2:
        return VARIANT_FLEX_CIRCULAR_2CH
    if card == FLEX_CIRCULAR_ALSA_CARD_NAME and capture_channel_count == 6:
        return VARIANT_FLEX_CIRCULAR_6CH
    if card == ALSA_CARD_NAME and capture_channel_count == 6:
        return VARIANT_6CH
    if card == ALSA_CARD_NAME and capture_channel_count == 2:
        return VARIANT_2CH
    return None


def firmware_update_target_for_profile(
    profile: RuntimeProfile,
) -> FirmwareUpdateTarget | None:
    """Return the safe update target for this exact detected mic profile."""

    variant_id = profile.variant_id
    if not variant_id:
        return None
    for target in FIRMWARE_UPDATE_TARGETS:
        if variant_id in target.from_variant_ids:
            return target
    return None


def firmware_update_status(
    profile: RuntimeProfile | None = None,
    *,
    service_active: bool = False,
    last_update: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the read-only firmware update card for /wake/.

    This is intentionally declarative: it decides whether JTS knows a safe,
    hash-pinned update for the detected geometry. It does not download or flash.
    """

    profile = profile or detect_runtime_profile()
    target = firmware_update_target_for_profile(profile)
    current = profile.as_dict()
    last = dict(last_update or {})
    last_state = str(last.get("state") or "")
    if service_active:
        state = "updating"
        title = "Updating microphone firmware"
        detail = str(last.get("detail") or "Firmware update is running.")
        required = True
    elif last_state == "failed" and target is not None:
        state = "failed"
        title = "Microphone firmware update failed"
        detail = str(
            last.get("error") or last.get("detail") or
            "The previous firmware update failed. You can retry the update."
        )
        required = True
    elif not profile.present:
        state = "no_mic"
        title = "No microphone firmware update"
        detail = "Connect a supported XVF3800 microphone before updating firmware."
        required = False
    elif profile.capture_channels == RECOMMENDED_CAPTURE_CHANNELS:
        state = "current"
        title = "Microphone firmware is current"
        detail = f"{profile.display_name} exposes the required 6-channel capture path."
        required = False
    elif target is not None:
        state = "update_required"
        title = "Microphone firmware update required"
        detail = (
            f"{profile.display_name} exposes {profile.capture_channels} capture "
            "channels. Hardware echo cancellation requires the 6-channel "
            f"{target.geometry} firmware."
        )
        required = True
    elif profile.variant is not None:
        state = "unsupported"
        title = "No safe firmware update is available"
        detail = (
            f"{profile.display_name} was detected, but JTS has no hash-pinned "
            "firmware update manifest for this exact geometry."
        )
        required = False
    else:
        state = "unknown"
        title = "Microphone firmware is unknown"
        detail = (
            "A supported XVF-like microphone is present, but JTS cannot identify "
            "its geometry and firmware. Firmware updates are disabled."
        )
        required = False
    return {
        "schema_version": 1,
        "state": state,
        "required": required,
        "updating": service_active,
        "title": title,
        "detail": detail,
        "current": current,
        "target": target.as_dict() if target else None,
        "last_update": last,
        "action": {
            "enabled": bool(target and not service_active),
            "label": "Download and update firmware",
            "danger": True,
        },
    }


def chip_beam_plan(plan_id: str | None) -> ChipBeamPlan | None:
    return CHIP_BEAM_PLANS.get((plan_id or "").strip())


def chip_beam_plan_for_variant(
    variant: FirmwareVariant | None,
) -> ChipBeamPlan | None:
    if variant is None or not variant.chip_beam_plan_id:
        return None
    return chip_beam_plan(variant.chip_beam_plan_id)


def chip_beam_plan_from_env(env: Mapping[str, str]) -> ChipBeamPlan | None:
    """Return the active chip beam plan from reconciler-applied env.

    Back-compat: older square/circular installs may have chip-AEC enabled
    without a plan id because the pre-geometry code only had one implicit
    plan. Treat missing plan + non-linear geometry as the legacy square
    plan; never do that when the env says the active geometry is linear.
    """

    explicit = chip_beam_plan(env.get("JASPER_XVF_CHIP_BEAM_PLAN", ""))
    if explicit:
        return explicit
    geometry = (env.get("JASPER_XVF_GEOMETRY", "") or "").strip().lower()
    if geometry == "linear":
        return None
    truthy = {"1", "true", "yes", "on"}
    if (
        str(env.get("JASPER_AEC_CHIP_AEC_ENABLED", "")).strip().lower() in truthy
        or str(env.get("JASPER_AEC_CORPUS_CHIP_AEC_ENABLED", "")).strip().lower()
        in truthy
    ):
        return SQUARE_FIXED_150_210_PLAN
    return None


def detect_runtime_profile(
    *,
    asound_root: Path = Path("/proc/asound"),
    bld_msg: str | None = None,
) -> RuntimeProfile:
    """Detect the active XVF runtime variant from cheap local facts.

    ALSA card identity is the hot path because it is available without
    USB control permissions. BLD_MSG, when supplied by a caller that has
    already read the chip, wins because it is the firmware's own word.
    """

    bld_variant = variant_for_bld_msg(bld_msg)
    for card in ALSA_CARD_NAMES:
        channels = _capture_channels_for_card(card, asound_root=asound_root)
        if channels is None:
            continue
        variant = bld_variant or variant_for_card(card, channels)
        plan = chip_beam_plan_for_variant(variant)
        if variant is None:
            return RuntimeProfile(
                present=True,
                variant=None,
                alsa_card_name=card,
                capture_channels=channels,
                chip_beam_plan=None,
                reason="XVF-like ALSA card present but firmware variant is unknown",
            )
        if plan:
            reason = f"{variant.display_name}; chip beam plan {plan.plan_id}"
        elif variant.capture_channels == RECOMMENDED_CAPTURE_CHANNELS:
            reason = (
                f"{variant.display_name}; no validated production chip "
                "beam plan for this geometry"
            )
        else:
            reason = f"{variant.display_name}; not 6-channel firmware"
        return RuntimeProfile(
            present=True,
            variant=variant,
            alsa_card_name=card,
            capture_channels=channels,
            chip_beam_plan=plan,
            reason=reason,
        )
    return RuntimeProfile(
        present=False,
        variant=None,
        alsa_card_name=ALSA_CARD_NAME,
        capture_channels=None,
        chip_beam_plan=None,
        reason="No supported XVF3800 ALSA card detected",
    )


def alsa_card_name() -> str:
    """Return the currently enumerated XVF ALSA card name.

    Legacy square USB firmware appears as `Array`; Flex linear firmware
    appears as `L16K6Ch`. If no supported card is present, return the
    legacy card name so existing defaults and remediation text stay
    stable on older installations.
    """
    for card in ALSA_CARD_NAMES:
        if Path(f"/proc/asound/{card}/stream0").exists():
            return card
    return ALSA_CARD_NAME


def _stream_path(card: str | None = None) -> Path:
    return Path(f"/proc/asound/{card or alsa_card_name()}/stream0")


def is_present() -> bool:
    """True if the chip's ALSA card has enumerated under /proc/asound."""
    return any(
        Path(f"/proc/asound/{card}/stream0").exists()
        for card in ALSA_CARD_NAMES
    )


def capture_channels() -> int | None:
    """Return the chip's USB capture endpoint channel count from
    /proc/asound/<card>/stream0, or None if the card is absent.

    Pinned to the ^Capture: section — /proc/asound/<card>/stream0
    has Playback first (Channels: 2 for the XVF chip's playback
    endpoint) then Capture (Channels: 6 on 6-ch firmware). A naive
    `grep Channels:` returns the Playback value, which was the May
    2026 reconciler bug that silently disabled software AEC."""
    return _capture_channels_for_card(alsa_card_name())


def chip_aec_supported() -> bool:
    """True only when the detected mic variant has a validated beam plan."""
    return detect_runtime_profile().chip_aec_supported


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
