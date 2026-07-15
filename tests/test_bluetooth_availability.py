# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.bluetooth.availability import (
    BLUETOOTH_CONTROL_PLANE_UNIT,
    probe_bluetooth_availability,
)
from jasper.local_sources import local_source_lifecycle
from jasper.music_sources import Source
from jasper.source_intent import BluetoothRfkillState


_REQUIRED_UNITS = (
    BLUETOOTH_CONTROL_PLANE_UNIT,
    *local_source_lifecycle(Source.BLUETOOTH).runtime_units,
)


def _rfkill(
    *,
    present: bool = True,
    soft_blocked: bool = False,
    hard_blocked: bool = False,
    all_soft_blocked: bool | None = None,
) -> BluetoothRfkillState:
    return BluetoothRfkillState(
        present=present,
        soft_blocked=soft_blocked,
        hard_blocked=hard_blocked,
        all_soft_blocked=all_soft_blocked,
    )


def test_soft_blocked_radio_remains_available_for_an_on_request():
    result = probe_bluetooth_availability(
        lambda _unit: True,
        rfkill_reader=lambda: _rfkill(
            soft_blocked=True,
            all_soft_blocked=True,
        ),
        path_isdir=lambda _path: False,
    )

    assert result.available is True
    assert result.radio_present is True
    assert result.any_soft_blocked is True
    assert result.all_soft_blocked is True
    assert result.hard_blocked is False


def test_hardware_blocked_radio_is_not_activatable():
    result = probe_bluetooth_availability(
        lambda _unit: True,
        rfkill_reader=lambda: _rfkill(hard_blocked=True),
        path_isdir=lambda _path: True,
    )

    assert result.available is False
    assert result.radio_present is True
    assert result.hard_blocked is True


def test_absent_rfkill_and_sysfs_adapter_is_unavailable():
    result = probe_bluetooth_availability(
        lambda _unit: True,
        rfkill_reader=lambda: _rfkill(present=False),
        path_isdir=lambda _path: False,
    )

    assert result.available is False
    assert result.radio_present is False
    assert result.any_soft_blocked is None
    assert result.all_soft_blocked is None
    assert result.hard_blocked is None


def test_sysfs_fallback_preserves_availability_when_rfkill_probe_fails():
    def fail_rfkill():
        raise RuntimeError("rfkill unreadable")

    result = probe_bluetooth_availability(
        lambda _unit: True,
        rfkill_reader=fail_rfkill,
        path_isdir=lambda _path: True,
    )

    assert result.available is True
    assert result.radio_present is True
    assert result.any_soft_blocked is None
    assert result.error == "RF-kill probe failed: rfkill unreadable"


def test_missing_lifecycle_unit_makes_support_unavailable():
    missing = _REQUIRED_UNITS[0]
    result = probe_bluetooth_availability(
        lambda unit: unit != missing,
        rfkill_reader=_rfkill,
        path_isdir=lambda _path: False,
    )

    assert result.available is False
    assert result.missing_units == (missing,)


def test_missing_pairing_agent_makes_support_unavailable():
    assert "bt-agent.service" in _REQUIRED_UNITS
    result = probe_bluetooth_availability(
        lambda unit: unit != "bt-agent.service",
        rfkill_reader=_rfkill,
        path_isdir=lambda _path: False,
    )

    assert result.available is False
    assert result.missing_units == ("bt-agent.service",)


def test_missing_bluez_control_plane_makes_support_unavailable():
    result = probe_bluetooth_availability(
        lambda unit: unit != BLUETOOTH_CONTROL_PLANE_UNIT,
        rfkill_reader=_rfkill,
        path_isdir=lambda _path: False,
    )

    assert result.available is False
    assert result.missing_units == (BLUETOOTH_CONTROL_PLANE_UNIT,)


def test_probe_failures_are_reported_and_fail_closed_without_raising():
    def fail_unit(unit: str) -> bool:
        if unit == _REQUIRED_UNITS[0]:
            raise TimeoutError("systemd timed out")
        return True

    def fail_path(_path: str) -> bool:
        raise OSError("sysfs unreadable")

    result = probe_bluetooth_availability(
        fail_unit,
        rfkill_reader=lambda: _rfkill(present=False),
        path_isdir=fail_path,
    )

    assert result.available is False
    assert result.radio_present is False
    assert result.missing_units == (_REQUIRED_UNITS[0],)
    assert "adapter path probe failed: sysfs unreadable" in result.error
    assert (
        f"unit probe failed for {_REQUIRED_UNITS[0]}: systemd timed out"
        in result.error
    )
