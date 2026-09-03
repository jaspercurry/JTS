# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Doctor check: the saved I2S DAC's overlay line is still in boot config.

Nothing else in the doctor reads ``config.txt`` — every other hardware check
sees only what the reconciler last *observed* on the bus. A removed
``dtoverlay=`` line does not change that observation until the next reboot
(#2575), so a box can run for days on a DAC the kernel will not re-attach the
next time it boots. This check reads the saved topology's declared DAC and
warns ahead of that reboot instead of after it.
"""
from __future__ import annotations

import os
from pathlib import Path

from ...audio_hardware.dac import by_id
from ...audio_hardware.usb_port_role import (
    DEFAULT_BOOT_CONFIG_PATH,
    configured_i2s_overlays,
)
from ...output_topology import OutputTopologyError, load_output_topology_strict
from ._registry import doctor_check
from ._shared import CheckResult

REASON_SKIPPED = "skipped"
REASON_OVERLAY_PRESENT = "overlay_present"
REASON_OVERLAY_MISSING = "overlay_missing"

#: Display name, shared with the ring-transport-park renderer
#: (:mod:`.audio_runtime`) so a DAC-not-recognized park can point at this
#: check by name without a second hardcoded copy of it.
CHECK_NAME = "I2S DAC overlay persists"


def _boot_config_path() -> Path:
    return Path(os.environ.get("JTS_BOOT_CONFIG_FILE", DEFAULT_BOOT_CONFIG_PATH))


def _read_boot_config(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@doctor_check(order=20.55, group="audio")
def check_i2s_dac_overlay_persists() -> CheckResult:
    """The saved active-output I2S DAC's dtoverlay line survives a reboot."""

    label = CHECK_NAME

    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            label,
            "ok",
            f"skipped — saved output topology is unavailable or invalid: {exc}",
            reason=REASON_SKIPPED,
        )

    device_id = topology.hardware.device_id
    if not device_id or device_id == "unknown":
        return CheckResult(
            label, "ok", "skipped — no saved output topology configured",
            reason=REASON_SKIPPED,
        )

    profile = by_id(device_id)
    if profile is None:
        return CheckResult(
            label,
            "ok",
            f"skipped — {device_id} has no registry DAC profile",
            reason=REASON_SKIPPED,
        )

    if profile.connection != "i2s" or not profile.dtoverlay:
        return CheckResult(
            label,
            "ok",
            f"skipped — {device_id} is not an I2S HAT DAC",
            reason=REASON_SKIPPED,
        )

    config_path = _boot_config_path()
    overlays = configured_i2s_overlays(_read_boot_config(config_path))
    if profile.dtoverlay.lower() in overlays:
        return CheckResult(
            label,
            "ok",
            f"dtoverlay={profile.dtoverlay} present in {config_path}",
            reason=REASON_OVERLAY_PRESENT,
        )
    return CheckResult(
        label,
        "fail",
        f"this box loses its DAC at the next reboot; add "
        f"dtoverlay={profile.dtoverlay} to {config_path}",
        reason=REASON_OVERLAY_MISSING,
    )
