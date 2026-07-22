# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared Bluetooth hardware/install availability for management surfaces."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from ..local_sources import local_source_lifecycle
from ..music_sources import Source
from ..source_intent import BluetoothRfkillState, read_bluetooth_rfkill_state

BLUETOOTH_ADAPTER_PATH = "/sys/class/bluetooth/hci0"
BLUETOOTH_CONTROL_PLANE_UNIT = "bluetooth.service"


@dataclass(frozen=True)
class BluetoothAvailability:
    available: bool
    radio_present: bool
    any_soft_blocked: bool | None
    all_soft_blocked: bool | None
    hard_blocked: bool | None
    error: str = ""
    missing_units: tuple[str, ...] = ()


def bluetooth_unavailable_reason(availability: BluetoothAvailability) -> str:
    """Return the shared household-facing explanation for an unavailable row."""

    if not availability.radio_present:
        return "No Bluetooth adapter was detected on this device."
    if availability.hard_blocked:
        return "Bluetooth is disabled by the device's hardware radio switch."
    if availability.missing_units:
        return (
            "Required Bluetooth services are not installed: "
            + ", ".join(availability.missing_units)
            + "."
        )
    if availability.error:
        return f"Bluetooth availability could not be verified: {availability.error}"
    return "Bluetooth adapter not available on this device."


def probe_bluetooth_availability(
    unit_available: Callable[[str], bool],
    *,
    rfkill_reader: Callable[[], BluetoothRfkillState] = read_bluetooth_rfkill_state,
    path_isdir: Callable[[str], bool] = os.path.isdir,
    adapter_path: str = BLUETOOTH_ADAPTER_PATH,
) -> BluetoothAvailability:
    """Return one role-independent adapter + installed-unit snapshot.

    RF-kill entries survive an intentionally powered-down adapter, while sysfs
    is a fallback when the RF-kill reader itself fails. This keeps Off repair
    available without pretending an absent adapter can be turned On.
    """

    errors: list[str] = []
    try:
        rfkill = rfkill_reader()
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        rfkill = None
        errors.append(f"RF-kill probe failed: {exc}")
    try:
        sysfs_present = path_isdir(adapter_path)
    except OSError as exc:
        sysfs_present = False
        errors.append(f"adapter path probe failed: {exc}")
    radio_present = bool((rfkill and rfkill.present) or sysfs_present)
    lifecycle = local_source_lifecycle(Source.BLUETOOTH)
    missing: list[str] = []
    # Availability is stronger than steady-state audio health: turning the
    # source On needs the shared BlueZ control plane and must support new
    # pairing, so bluetooth.service plus every source unit the reconciler
    # runs (including bt-agent) must be installed. The control plane is not
    # source-owned and therefore deliberately stays out of park_units.
    activation_units = (BLUETOOTH_CONTROL_PLANE_UNIT, *lifecycle.runtime_units)
    for unit in activation_units:
        try:
            loaded = unit_available(unit)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            loaded = False
            errors.append(f"unit probe failed for {unit}: {exc}")
        if not loaded:
            missing.append(unit)
    missing_units = tuple(missing)
    hard_blocked = (
        rfkill.hard_blocked if rfkill is not None and rfkill.present else None
    )
    return BluetoothAvailability(
        available=radio_present and not missing_units and hard_blocked is not True,
        radio_present=radio_present,
        any_soft_blocked=(
            rfkill.soft_blocked if rfkill is not None and rfkill.present else None
        ),
        all_soft_blocked=(
            rfkill.fully_soft_blocked
            if rfkill is not None and rfkill.present
            else None
        ),
        hard_blocked=hard_blocked,
        error="; ".join(errors),
        missing_units=missing_units,
    )


__all__ = [
    "BLUETOOTH_ADAPTER_PATH",
    "BLUETOOTH_CONTROL_PLANE_UNIT",
    "BluetoothAvailability",
    "bluetooth_unavailable_reason",
    "probe_bluetooth_availability",
]
