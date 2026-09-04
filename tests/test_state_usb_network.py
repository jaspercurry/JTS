# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for state_aggregate._usb_network_snapshot — the /state
`usb_network` block (live interface state plus its desired address plan) that
surfaces the USB management network alongside the composite gadget's audio
side. jasper-doctor's
check_usbnet_* (tests/test_doctor_network.py) own the actionable
composed-vs-intent mismatch story; this block is the always-visible
dashboard mirror, read fresh from /sys/class/net/usb0 and the
kill-switch env on every call."""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.control import state_aggregate
from jasper.usb_network import (
    IPv4Observation,
    IPv4ObservationState,
    UsbNetworkPlanError,
    derive_plan,
)


PLAN = derive_plan("10000000abcdef01")


@pytest.fixture(autouse=True)
def _usb_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(state_aggregate, "load_usb_network_plan", lambda: PLAN)
    monkeypatch.setattr(state_aggregate, "attest_usb_network_plan", lambda plan: plan)
    monkeypatch.setattr(
        state_aggregate,
        "observe_ipv4_cidr",
        lambda _iface: IPv4Observation(
            IPv4ObservationState.OBSERVED, cidr=PLAN.device_cidr
        ),
    )
    monkeypatch.setattr(
        state_aggregate, "USB_NETWORK_PENDING_PATH", tmp_path / "pending"
    )


def _expected(*, enabled: bool, iface_present: bool, carrier: bool):
    return {
        "enabled": enabled,
        "iface_present": iface_present,
        "carrier": carrier,
        "address": PLAN.device_address if iface_present else None,
        "cidr": PLAN.device_cidr if iface_present else None,
        "observation_status": "observed" if iface_present else "absent",
        "observation_error": None,
        "desired_address": PLAN.device_address,
        "subnet": PLAN.subnet,
        "plan_version": PLAN.version,
        "identity_fingerprint": PLAN.identity_fingerprint,
        "migration_pending": False,
    }


def _patch_sys_class_net(monkeypatch, tmp_path):
    """Redirect state_aggregate's Path("/sys/class/net") lookup at
    tmp_path, leaving every other Path(...) call untouched."""
    real_path = Path

    def _fake_path(p):
        if p == "/sys/class/net":
            return tmp_path
        return real_path(p)

    monkeypatch.setattr(state_aggregate, "Path", _fake_path)


def test_usb_network_disabled_no_iface(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled")
    _patch_sys_class_net(monkeypatch, tmp_path)

    assert state_aggregate._usb_network_snapshot() == _expected(
        enabled=False, iface_present=False, carrier=False
    )


def test_usb_network_enabled_no_host_plugged_in(monkeypatch, tmp_path):
    """Network enabled (default) but usb0 not yet present — the gadget hasn't
    bound the NCM function yet (pre-reboot / no UDC). /state reports the
    kill-switch intent (enabled=True) with iface/carrier absent; this block is
    intentionally simpler than the doctor's compose/bind failure check, so
    iface absent here is reported, not judged."""
    monkeypatch.delenv("JASPER_USB_NETWORK", raising=False)
    _patch_sys_class_net(monkeypatch, tmp_path)

    assert state_aggregate._usb_network_snapshot() == _expected(
        enabled=True, iface_present=False, carrier=False
    )


def test_usb_network_enabled_host_plugged_in_carrier_up(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    iface = tmp_path / "usb0"
    iface.mkdir()
    (iface / "carrier").write_text("1\n")
    _patch_sys_class_net(monkeypatch, tmp_path)

    assert state_aggregate._usb_network_snapshot() == _expected(
        enabled=True, iface_present=True, carrier=True
    )


def test_usb_network_iface_present_no_carrier(monkeypatch, tmp_path):
    """usb0 composed (ncm.usb0 up) but nothing plugged in right now — the
    interface can exist with carrier down; its observed address is still
    reported regardless of link state."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    iface = tmp_path / "usb0"
    iface.mkdir()
    (iface / "carrier").write_text("0\n")
    _patch_sys_class_net(monkeypatch, tmp_path)

    block = state_aggregate._usb_network_snapshot()

    assert block["iface_present"] is True
    assert block["carrier"] is False
    assert block["address"] == PLAN.device_address


def test_usb_network_enabled_field_follows_the_kill_switch(monkeypatch, tmp_path):
    """The reported `enabled` is jasper.usbgadget.network_wanted's verdict;
    tests/test_usbgadget_status.py pins the literal parsing itself."""
    _patch_sys_class_net(monkeypatch, tmp_path)

    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled")
    assert state_aggregate._usb_network_snapshot()["enabled"] is False

    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    assert state_aggregate._usb_network_snapshot()["enabled"] is True


def test_usb_network_carrier_read_error_fails_soft(monkeypatch, tmp_path):
    """An unreadable carrier file (e.g. a race where the interface
    disappears between is_dir() and the carrier read, or a permissions
    oddity) must degrade to carrier=False, never raise and break /state."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    iface = tmp_path / "usb0"
    iface.mkdir()
    # No carrier file written -> read_text() raises FileNotFoundError, a
    # concrete OSError subclass caught by the snapshot's fail-soft path.
    _patch_sys_class_net(monkeypatch, tmp_path)

    block = state_aggregate._usb_network_snapshot()

    assert block["iface_present"] is True
    assert block["carrier"] is False
    assert block["address"] == PLAN.device_address


def test_usb_network_plan_failure_reports_no_fabricated_desired_address(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        state_aggregate,
        "load_usb_network_plan",
        lambda: (_ for _ in ()).throw(UsbNetworkPlanError("corrupt")),
    )
    _patch_sys_class_net(monkeypatch, tmp_path)

    block = state_aggregate._usb_network_snapshot()

    assert block["desired_address"] is None
    assert block["subnet"] is None
    assert block["plan_version"] is None
    assert block["identity_fingerprint"] is None


def test_usb_network_migration_shows_desired_and_observed_generations(
    monkeypatch, tmp_path,
):
    iface = tmp_path / "usb0"
    iface.mkdir()
    (iface / "carrier").write_text("1\n")
    _patch_sys_class_net(monkeypatch, tmp_path)
    monkeypatch.setattr(
        state_aggregate,
        "observe_ipv4_cidr",
        lambda _iface: IPv4Observation(
            IPv4ObservationState.OBSERVED, cidr="10.12.194.1/24"
        ),
    )
    pending = tmp_path / "pending"
    pending.write_text("pending\n")
    monkeypatch.setattr(state_aggregate, "USB_NETWORK_PENDING_PATH", pending)

    block = state_aggregate._usb_network_snapshot()

    assert block["address"] == "10.12.194.1"
    assert block["cidr"] == "10.12.194.1/24"
    assert block["desired_address"] == PLAN.device_address
    assert block["migration_pending"] is True


def test_usb_network_observation_error_is_visible_without_fabricated_address(
    monkeypatch, tmp_path,
):
    iface = tmp_path / "usb0"
    iface.mkdir()
    (iface / "carrier").write_text("1\n")
    _patch_sys_class_net(monkeypatch, tmp_path)
    monkeypatch.setattr(
        state_aggregate,
        "observe_ipv4_cidr",
        lambda _iface: IPv4Observation(
            IPv4ObservationState.ERROR, error="inspection denied"
        ),
    )

    block = state_aggregate._usb_network_snapshot()

    assert block["address"] is None
    assert block["cidr"] is None
    assert block["observation_status"] == "error"
    assert block["observation_error"] == "inspection denied"
