# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Doctor checks: JTS-managed ``dtoverlay=`` lines in boot config, vs. what
they should currently say.

Every other hardware check sees only what the reconciler last *observed* on
the bus, and a removed ``dtoverlay=`` line does not change that observation
until the next reboot (#2575) — so a box can run for days on a DAC the kernel
will not re-attach. ``check_i2s_dac_overlay_persists`` reads the saved
topology's declared DAC and warns ahead of that reboot instead of after it.

``check_i2s_hat_block_orphaned`` covers the opposite drift: JTS's managed I2S
HAT block staying in config.txt after the HAT it was written for is gone
(#4027 R3).
"""
from __future__ import annotations

from ...audio_hardware.config_txt import boot_config_path, overlay_declared_anywhere
from ...audio_hardware.dac import by_id
from ...audio_hardware.i2s_hat import (
    configured_i2s_overlays,
    i2s_hat_managed,
    managed_i2s_hat_block_present,
)
from ...control.transport_park import I2S_DAC_OVERLAY_CHECK_NAME as CHECK_NAME
from ...output_topology import OutputTopologyError
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult

REASON_SKIPPED = "skipped"
REASON_BOOT_CONFIG_UNREADABLE = "boot_config_unreadable"
REASON_OVERLAY_PRESENT = "overlay_present"
REASON_OVERLAY_PRESENT_SCOPED = "overlay_present_scoped"
REASON_OVERLAY_MISSING = "overlay_missing"
REASON_ORPHAN_MANAGED_I2S_BLOCK = "orphan_managed_i2s_block"
REASON_I2S_HAT_BLOCK_MALFORMED = "i2s_hat_block_malformed"


def _skipped(detail: str) -> CheckResult:
    return CheckResult(CHECK_NAME, "ok", f"skipped — {detail}", reason=REASON_SKIPPED)


@doctor_check()
def check_i2s_dac_overlay_persists() -> CheckResult:
    """The saved active-output I2S DAC's dtoverlay line survives a reboot."""

    label = CHECK_NAME

    try:
        topology = evidence.output_topology_strict()
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
    content = evidence.boot_config_text()
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


@doctor_check()
def check_i2s_hat_block_orphaned() -> CheckResult:
    """A managed I2S HAT block survives with no HAT left to justify it.

    ``i2s_hat_apply`` (deploy/bin/jasper-audio-hardware-reconcile) leaves the
    block untouched whenever neither a fitted HAT nor an intent file exists
    (ADR-0234's jts3-incident guard) — so a HAT that was only ever
    auto-detected, once removed, leaves its block in config.txt forever.
    """

    label = "I2S HAT boot block"
    config_path = boot_config_path()
    content = evidence.boot_config_text()
    if content is None:
        return CheckResult(
            label,
            "warn",
            f"could not read {config_path} — cannot confirm no orphaned "
            "I2S HAT block remains",
            reason=REASON_BOOT_CONFIG_UNREADABLE,
        )
    try:
        block_present = managed_i2s_hat_block_present(content)
    except ValueError:
        return CheckResult(
            label,
            "warn",
            f"a managed I2S HAT block in {config_path} is malformed; repair "
            "or remove it by hand",
            reason=REASON_I2S_HAT_BLOCK_MALFORMED,
        )
    if not block_present:
        return CheckResult(label, "ok", "no managed I2S HAT block present")
    if i2s_hat_managed():
        return CheckResult(
            label,
            "ok",
            f"managed I2S HAT block present in {config_path}, HAT intended",
        )
    return CheckResult(
        label,
        "warn",
        f"a managed I2S HAT block is still in {config_path} but no HAT is "
        "detected or intended; select None under Sound setup",
        reason=REASON_ORPHAN_MANAGED_I2S_BLOCK,
    )
