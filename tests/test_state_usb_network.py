# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for state_aggregate._usb_network_snapshot — the /state
`usb_network` block ({enabled, iface_present, carrier, address}) that
surfaces the USB management network (docs/HANDOFF-usb-gadget.md)
alongside the composite gadget's audio side. jasper-doctor's
check_usbnet_* (tests/test_doctor_usbnet.py) own the actionable
composed-vs-intent mismatch story; this block is the always-visible
dashboard mirror, read fresh from /sys/class/net/usb0 and the
kill-switch env on every call."""
from __future__ import annotations

from pathlib import Path

from jasper.control import state_aggregate


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

    assert state_aggregate._usb_network_snapshot() == {
        "enabled": False,
        "iface_present": False,
        "carrier": False,
        "address": None,
    }


def test_usb_network_enabled_no_host_plugged_in(monkeypatch, tmp_path):
    """Network enabled (default) but usb0 not yet present — the gadget hasn't
    bound the NCM function yet (pre-reboot / no UDC). /state reports the
    kill-switch intent (enabled=True) with iface/carrier absent; this block is
    intentionally simpler than the doctor's compose/bind failure check, so
    iface absent here is reported, not judged."""
    monkeypatch.delenv("JASPER_USB_NETWORK", raising=False)
    _patch_sys_class_net(monkeypatch, tmp_path)

    assert state_aggregate._usb_network_snapshot() == {
        "enabled": True,
        "iface_present": False,
        "carrier": False,
        "address": None,
    }


def test_usb_network_enabled_host_plugged_in_carrier_up(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    iface = tmp_path / "usb0"
    iface.mkdir()
    (iface / "carrier").write_text("1\n")
    _patch_sys_class_net(monkeypatch, tmp_path)

    assert state_aggregate._usb_network_snapshot() == {
        "enabled": True,
        "iface_present": True,
        "carrier": True,
        "address": "10.12.194.1",
    }


def test_usb_network_iface_present_no_carrier(monkeypatch, tmp_path):
    """usb0 composed (ncm.usb0 up) but nothing plugged in right now — the
    interface can exist with carrier down; address is still reported
    since the fixed management IP is bound to the interface regardless
    of link state."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    iface = tmp_path / "usb0"
    iface.mkdir()
    (iface / "carrier").write_text("0\n")
    _patch_sys_class_net(monkeypatch, tmp_path)

    block = state_aggregate._usb_network_snapshot()

    assert block["iface_present"] is True
    assert block["carrier"] is False
    assert block["address"] == "10.12.194.1"


def test_usb_network_kill_switch_is_case_insensitive_exact_literal(
    monkeypatch, tmp_path,
):
    """Only the exact literal 'disabled' (case-insensitive) turns the
    reported `enabled` off; any other value stays enabled — mirrors
    JASPER_SHAIRPORT_SUPERVISOR / JASPER_SYSTEM_SUPERVISOR."""
    _patch_sys_class_net(monkeypatch, tmp_path)

    monkeypatch.setenv("JASPER_USB_NETWORK", "DISABLED")
    assert state_aggregate._usb_network_snapshot()["enabled"] is False

    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled-typo")
    assert state_aggregate._usb_network_snapshot()["enabled"] is True

    monkeypatch.setenv("JASPER_USB_NETWORK", "off")
    assert state_aggregate._usb_network_snapshot()["enabled"] is True

    # Whitespace-decorated near-miss: a stray space breaks the exact-literal
    # match and STAYS enabled, matching jasper-usbgadget-up's raw (untrimmed)
    # comparison so bash and Python never disagree (review core-7). The
    # fail-safe direction: a stray space must not silently drop the fallback
    # network.
    monkeypatch.setenv("JASPER_USB_NETWORK", " disabled ")
    assert state_aggregate._usb_network_snapshot()["enabled"] is True
    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled ")
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
    assert block["address"] == "10.12.194.1"


def test_usb_network_wanted_helper_matches_snapshot_enabled_field(monkeypatch):
    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled")
    assert state_aggregate._usb_network_wanted() is False
    monkeypatch.delenv("JASPER_USB_NETWORK", raising=False)
    assert state_aggregate._usb_network_wanted() is True
