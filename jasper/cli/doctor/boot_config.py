# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Doctor check: the saved I2S DAC's overlay line is still in boot config.

Every other hardware check sees only what the reconciler last *observed* on
the bus, and a removed ``dtoverlay=`` line does not change that observation
until the next reboot (#2575) — so a box can run for days on a DAC the kernel
will not re-attach. This check reads the saved topology's declared DAC and
warns ahead of that reboot instead of after it.
"""
from __future__ import annotations

from ...audio_hardware.dac import by_id
from ...audio_hardware.usb_port_role import (
    boot_config_path,
    configured_i2s_overlays,
    overlay_declared_anywhere,
    read_boot_config_or_none,
)
from ...control.transport_park import I2S_DAC_OVERLAY_CHECK_NAME as CHECK_NAME
from ...output_topology import OutputTopologyError, load_output_topology_strict
from ._registry import doctor_check
from ._shared import CheckResult

REASON_SKIPPED = "skipped"
REASON_BOOT_CONFIG_UNREADABLE = "boot_config_unreadable"
REASON_OVERLAY_PRESENT = "overlay_present"
REASON_OVERLAY_PRESENT_SCOPED = "overlay_present_scoped"
REASON_OVERLAY_MISSING = "overlay_missing"


def _skipped(detail: str) -> CheckResult:
    return CheckResult(CHECK_NAME, "ok", f"skipped — {detail}", reason=REASON_SKIPPED)


@doctor_check()
def check_i2s_dac_overlay_persists() -> CheckResult:
    """The saved active-output I2S DAC's dtoverlay line survives a reboot."""

    label = CHECK_NAME

    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        return _skipped(f"saved output topology is unavailable or invalid: {exc}")

    device_id = topology.hardware.device_id
    if not device_id or device_id == "unknown":
        return _skipped("no saved output topology configured")

    profile = by_id(device_id)
    if profile is None:
        return _skipped(f"{device_id} has no registry DAC profile")

    if profile.connection != "i2s" or not profile.dtoverlay:
        return _skipped(f"{device_id} is not an I2S HAT DAC")

    config_path = boot_config_path()
    content = read_boot_config_or_none(config_path)
    if content is None:
        return CheckResult(
            label,
            "warn",
            f"could not read {config_path} — cannot confirm "
            f"dtoverlay={profile.dtoverlay} survives a reboot",
            reason=REASON_BOOT_CONFIG_UNREADABLE,
        )

    if profile.dtoverlay.lower() in configured_i2s_overlays(content):
        return CheckResult(
            label,
            "ok",
            f"dtoverlay={profile.dtoverlay} present in {config_path}",
            reason=REASON_OVERLAY_PRESENT,
        )
    if overlay_declared_anywhere(content, profile.dtoverlay):
        return CheckResult(
            label,
            "ok",
            f"dtoverlay={profile.dtoverlay} present in {config_path} "
            "under a model-scoped section",
            reason=REASON_OVERLAY_PRESENT_SCOPED,
        )
    return CheckResult(
        label,
        "fail",
        f"this box loses its DAC at the next reboot; add "
        f"dtoverlay={profile.dtoverlay} to {config_path}",
        reason=REASON_OVERLAY_MISSING,
    )
