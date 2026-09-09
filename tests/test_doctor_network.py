# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor network domain.

Covers the Wi-Fi lane (active connection, regdom, guardian stash, link-local
IPv6, avahi, flap-recovery timer), speaker identity coherence, and the USB
management network (usb0, its NetworkManager profile, the device-activated
dnsmasq unit, and a loopback probe of the fallback management URL). The
composite-gadget *function* composition is jasper/cli/doctor/usbsink.py's
concern, pinned in test_doctor_usbsink.py.
"""
from __future__ import annotations

import shutil
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jasper.cli.doctor import _evidence
from jasper.cli.doctor import network as doctor_network
from jasper.cli.doctor import web
from jasper.usb_network import (
    IPv4Observation,
    IPv4ObservationState,
    UsbNetworkPlanError,
    derive_plan,
    render_dnsmasq,
    render_nmconnection,
)

from .doctor_test_support import _registered_check_names, _write_identity_env


def _seed_unit_states(**by_unit):
    """Seed the batched-roster evidence read so `evidence.unit_state(unit)`
    answers from these fields without spawning `systemctl`."""
    fields = ("unit", "load_state", "active_state", "sub_state",
              "unit_file_state", "result", "n_restarts", "main_pid")
    states = {
        unit: {f: overrides.get(f) for f in fields} | {"unit": unit}
        for unit, overrides in by_unit.items()
    }
    _evidence.evidence.seed("units", states)

# -------------------------------------------------- active WiFi connection


def _completed(
    args=("command",),
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _nmcli_active_run(stdout: str):
    """Build a fake `_run` returning ``stdout`` for any nmcli invocation.

    Records the argv it was called with so tests can assert the field
    order requested from nmcli."""
    calls: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))
        return _completed(argv, stdout=stdout)

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def test_active_wifi_connection_requests_the_colon_safe_field_order(monkeypatch):
    """The variable-content NAME field must be requested LAST, so the
    fixed-format TYPE/DEVICE tokens parse unambiguously ahead of an SSID that
    contains its own colon. The parse itself is pinned on the shared function
    (tests/test_wifi_guardian_persistence.py)."""
    fake_run = _nmcli_active_run("802-11-wireless:wlan0:Home\\:2.4G\n")
    monkeypatch.setattr(doctor_network, "_run", fake_run)

    assert doctor_network._active_wifi_connection("nmcli") == ("Home:2.4G", "wlan0")
    assert "TYPE,DEVICE,NAME" in fake_run.calls[0]


def test_active_wifi_connection_nonzero_returncode(monkeypatch):
    """nmcli failure → (None, None), not a crash."""

    def fake_run(argv, *a, **kw):
        return _completed(argv, returncode=1)

    monkeypatch.setattr(doctor_network, "_run", fake_run)
    assert doctor_network._active_wifi_connection("nmcli") == (None, None)


# ---------------------------------------------------- check_wifi_regdom


def _patch_doctor_iw_reg_get(monkeypatch, stdout: str, returncode: int = 0):
    def fake_run(cmd, timeout=5.0):
        assert cmd == ["iw", "reg", "get"]
        return _completed(
            cmd,
            returncode=returncode,
            stdout=stdout,
            stderr="boom" if returncode else "",
        )

    monkeypatch.setattr(doctor_network, "_run", fake_run)


# ---------------------------------------------------- check_wifi_guardian
#
# The check has four happy/warn paths to cover (matches the design
# doc §3.7 (F)):
#   - ok: stash present, active SSID matches
#   - ok: no stash and no active WiFi (Ethernet-only Pi)
#   - warn: WiFi up, no stash -> wizard never saved
#   - warn: stash present, active WiFi on a different SSID -> drift
#   - warn: stash present, no active WiFi -> last guardian failed
# Skip path:
#   - ok with detail "skipped" when nmcli isn't on PATH


def _mock_nmcli_proc(stdout: str = "", returncode: int = 0):
    """Synthesize a CompletedProcess for `_run` to return."""
    return _completed(
        ["nmcli"],
        returncode=returncode,
        stdout=stdout,
    )


def _patch_doctor_nmcli(monkeypatch, response_stack):
    """Patch shutil.which to return a path and doctor_network._run to return
    the next CompletedProcess in response_stack for each call.

    Each entry can be either a string (treated as stdout, rc=0) or
    a CompletedProcess. The check makes 0-2 _run() calls depending
    on the path; over-long stacks are fine, under-long stacks fail
    the call with returncode=1.
    """
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
    )
    responses = iter(response_stack)

    def fake_run(cmd, timeout=5.0):
        try:
            r = next(responses)
        except StopIteration:
            return _mock_nmcli_proc(returncode=1)
        if isinstance(r, str):
            return _mock_nmcli_proc(stdout=r)
        return r

    monkeypatch.setattr(doctor_network, "_run", fake_run)


def test_check_wifi_guardian_registered_in_sync_checks():
    """Make sure the check is actually registered to run (not just
    defined). Mirrors the spirit of the `check_wifi_regdom` registration
    this check sits next to."""
    assert "check_wifi_guardian" in _registered_check_names()


def test_check_wifi_link_local_ipv6_registered_in_sync_checks():
    assert "check_wifi_link_local_ipv6" in _registered_check_names()


def test_check_avahi_jasper_control_ok_on_partial_timeout(monkeypatch):
    """Resolved avahi-browse can hang on stale sibling records after seeing
    the local service. That is still evidence that jasper-control is
    advertised; it should not crash the whole doctor run."""
    monkeypatch.setattr(
        doctor_network.shutil,
        "which",
        lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None,
    )

    def fake_run(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=(
                "+ wlan0 IPv4 JTS jasper-control on jts5 _jasper-control._tcp local\n"
            ),
        )

    monkeypatch.setattr(doctor_network, "_run", fake_run)

    r = doctor_network.check_avahi_jasper_control()

    assert r.status == "ok"
    assert r.reason == doctor_network.REASON_AVAHI_BROWSE_PARTIAL_TIMEOUT


def test_check_avahi_jasper_control_skips_without_avahi_browse(monkeypatch):
    """No avahi-browse means the advertisement was never observed; the three
    fail arms below all rest on a probe that actually ran."""
    monkeypatch.setattr(doctor_network.shutil, "which", lambda _name: None)

    r = doctor_network.check_avahi_jasper_control()

    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_AVAHI_BROWSE_MISSING


def test_check_avahi_jasper_control_fails_on_timeout_without_service(
    monkeypatch,
):
    monkeypatch.setattr(
        doctor_network.shutil,
        "which",
        lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None,
    )

    def fake_run(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(cmd, timeout, output="")

    monkeypatch.setattr(doctor_network, "_run", fake_run)

    r = doctor_network.check_avahi_jasper_control()

    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_AVAHI_BROWSE_TIMEOUT


# ------------------------------------------ check_hostname_avahi_consistency


def _patch_avahi_resolve(
    monkeypatch, *, sys_hostname="jts", resolve=("jts.local 192.168.1.9", 0),
    own_ips="192.168.1.9", have_binary=True,
):
    monkeypatch.setattr(
        doctor_network.shutil,
        "which",
        lambda name: "/usr/bin/avahi-resolve-host-name" if have_binary else None,
    )

    def fake_run(cmd, timeout=5.0):
        if cmd[:2] == ["hostname", "-s"]:
            return _completed(cmd, stdout=sys_hostname)
        if cmd[:2] == ["hostname", "-I"]:
            return _completed(cmd, stdout=own_ips)
        stdout, returncode = resolve
        return _completed(cmd, returncode=returncode, stdout=stdout)

    monkeypatch.setattr(doctor_network, "_run", fake_run)


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        ({}, "ok", ""),
        ({"sys_hostname": ""}, "skipped", "REASON_HOSTNAME_UNREADABLE"),
        ({"have_binary": False}, "skipped", "REASON_AVAHI_RESOLVE_MISSING"),
        ({"resolve": ("", 1)}, "warn", "REASON_AVAHI_RESOLVE_FAILED"),
        (
            {"resolve": ("jts.local", 0)},
            "warn",
            "REASON_AVAHI_RESOLVE_UNEXPECTED_OUTPUT",
        ),
        (
            {"own_ips": "192.168.1.40"},
            "fail",
            "REASON_HOSTNAME_COLLISION",
        ),
    ],
    ids=[
        "resolves-to-us", "no-hostname", "no-avahi-utils", "resolve-failed",
        "unparseable-output", "another-box-owns-the-name",
    ],
)
def test_check_hostname_avahi_consistency_verdicts(
    monkeypatch, kwargs, status, reason
):
    """Two boxes on one name breaks `<hostname>.local` for the whole
    household, so the collision fails; the arms that resolved nothing at all
    (no hostname, no avahi-utils — nothing to observe) skip, while a daemon
    that answered — whether with rc=0 and unparseable stdout, or unable to
    resolve our own name — warns: data arrived, it just isn't the answer we
    wanted (check_avahi_daemon only catches not-found/inactive, not this)."""
    _patch_avahi_resolve(monkeypatch, **kwargs)

    r = doctor_network.check_hostname_avahi_consistency()

    assert r.status == status
    assert r.reason == (getattr(doctor_network, reason) if reason else "")


# ----- check_wifi_recover_timer (Wi-Fi flap recovery timer health) -----


# ------------------------------------------------- check_identity_coherence
#
# The reconciler writes identity.env; the check reads it via
# jasper.identity.identity_state and reports whether the advertised name still matches
# what the operator configured.


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        ({}, "ok", ""),
        # A collision means avahi renamed us: discovery is broken for the
        # household until the name is unique, so a fresh snapshot fails.
        (
            {"collision": "1", "drift": "1", "avahi": "jts3-2.local"},
            "fail",
            doctor_network.REASON_IDENTITY_COLLISION,
        ),
        ({"drift": "1"}, "warn", doctor_network.REASON_IDENTITY_DRIFT),
    ],
    ids=["coherent", "collision", "drift"],
)
def test_check_identity_coherence_verdicts(
    monkeypatch, tmp_path, kwargs, status, reason
):
    _write_identity_env(tmp_path, monkeypatch, **kwargs)

    r = doctor_network.check_identity_coherence()

    assert r.status == status
    assert r.reason == reason


def test_check_identity_coherence_discloses_a_stale_snapshot(monkeypatch, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _write_identity_env(tmp_path, monkeypatch, checked_at=old)

    r = doctor_network.check_identity_coherence()

    assert r.status == "ok"
    assert r.reason == doctor_network.REASON_IDENTITY_SNAPSHOT_STALE


def test_check_identity_coherence_stale_collision_warns(monkeypatch, tmp_path):
    """A collision on a stale snapshot (the reconciler timer may be dead)
    can't be asserted live, so it warns instead of failing — same reason,
    stale note still folded into the detail."""
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _write_identity_env(
        tmp_path, monkeypatch,
        collision="1", avahi="jts3-2.local", checked_at=old,
    )

    r = doctor_network.check_identity_coherence()

    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_IDENTITY_COLLISION
    assert "min old" in r.detail


@pytest.mark.parametrize(
    "reconciler_installed, status, reason",
    [
        (False, "skipped", doctor_network.REASON_IDENTITY_NOT_INSTALLED),
        (True, "warn", doctor_network.REASON_IDENTITY_FILE_MISSING),
    ],
    ids=["off-pi", "reconciler-installed"],
)
def test_check_identity_coherence_absent_file(
    monkeypatch, tmp_path, reconciler_installed, status, reason
):
    """No identity.env off a Pi is nothing to say; with the reconciler
    installed it means the reconcile never ran."""
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(
        doctor_network.os.path,
        "exists",
        lambda p: reconciler_installed
        and p == "/usr/local/sbin/jasper-identity-reconcile",
    )

    r = doctor_network.check_identity_coherence()

    assert r.status == status
    assert r.reason == reason


# ---------------------------------------------------- USB management network

PLAN = derive_plan("10000000abcdef01")


@pytest.fixture(autouse=True)
def _available_usb_role(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network,
        "current_usb_data_role",
        lambda: SimpleNamespace(
            gadget_available=True,
            management_transport_available=True,
            reboot_required=False,
            reason="available",
        ),
    )
    monkeypatch.setattr(doctor_network, "load_usb_network_plan", lambda: PLAN)
    monkeypatch.setattr(
        doctor_network, "attest_usb_network_plan", lambda plan: plan
    )
    monkeypatch.setattr(
        doctor_network,
        "observe_ipv4_cidr",
        lambda _iface: IPv4Observation(
            IPv4ObservationState.OBSERVED, cidr=PLAN.device_cidr
        ),
    )
    pending = tmp_path / "usb-network-pending"
    nm = tmp_path / "jts-usb.nmconnection"
    dnsmasq = tmp_path / "usbnet-dnsmasq.conf"
    nm.write_text(render_nmconnection(PLAN))
    dnsmasq.write_text(render_dnsmasq(PLAN))
    monkeypatch.setattr(doctor_network, "DEFAULT_PENDING_PATH", pending)
    monkeypatch.setattr(doctor_network, "DEFAULT_NM_PATH", nm)
    monkeypatch.setattr(doctor_network, "DEFAULT_DNSMASQ_PATH", dnsmasq)


# ----------------------------------------------------------------------
# check_usbnet_address_plan
# ----------------------------------------------------------------------


def test_usbnet_address_plan_valid_and_consistent_is_ok():
    result = doctor_network.check_usbnet_address_plan()

    assert result.status == "ok"
    assert result.reason == ""
    # Pins that the plan's own derived values (not just a verdict) reach the
    # operator-facing detail — a formatting concern the reason code carries
    # no data for.
    assert PLAN.subnet in result.detail
    assert PLAN.identity_fingerprint in result.detail


def test_usbnet_address_plan_missing_fails_without_blocking_wifi(monkeypatch):
    monkeypatch.setattr(
        doctor_network,
        "load_usb_network_plan",
        lambda: (_ for _ in ()).throw(UsbNetworkPlanError("missing")),
    )

    result = doctor_network.check_usbnet_address_plan()

    assert result.status == "fail"
    assert result.reason == doctor_network.REASON_USBNET_PLAN_INVALID


def test_usbnet_address_plan_attests_current_pi_identity(monkeypatch):
    monkeypatch.setattr(
        doctor_network,
        "attest_usb_network_plan",
        lambda _plan: (_ for _ in ()).throw(
            UsbNetworkPlanError("does not match this Pi CPU serial")
        ),
    )

    result = doctor_network.check_usbnet_address_plan()

    assert result.status == "fail"
    assert result.reason == doctor_network.REASON_USBNET_PLAN_INVALID


def test_usbnet_address_plan_projection_drift_fails(monkeypatch, tmp_path):
    drifted = tmp_path / "jts-usb.nmconnection"
    drifted.write_text("wrong generation\n")
    monkeypatch.setattr(doctor_network, "DEFAULT_NM_PATH", drifted)

    result = doctor_network.check_usbnet_address_plan()

    assert result.status == "fail"
    assert result.reason == doctor_network.REASON_USBNET_PLAN_PROJECTION_DRIFT


def test_usbnet_address_plan_pending_migration_is_ok(monkeypatch, tmp_path):
    pending = tmp_path / "pending"
    pending.write_text("pending\n")
    monkeypatch.setattr(doctor_network, "DEFAULT_PENDING_PATH", pending)

    result = doctor_network.check_usbnet_address_plan()

    assert result.status == "ok"
    assert result.reason == doctor_network.REASON_USBNET_PLAN_PENDING


def _stub_run(monkeypatch, table):
    """Route doctor_network._run calls through a {tuple(cmd_prefix): CompletedProcess}
    lookup by first-two-args prefix match, falling back to a returncode=1
    failure for anything unexpected (so a missing stub fails loudly)."""

    def _run(cmd, timeout=5.0):
        for prefix, result in table.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unstubbed call")

    monkeypatch.setattr(doctor_network, "_run", _run)


# ----------------------------------------------------------------------
# check_usbnet_interface
# ----------------------------------------------------------------------


def test_usbnet_interface_kill_switched_no_iface_is_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled")
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    r = doctor_network.check_usbnet_interface()
    assert r.status == "ok"
    assert r.reason == doctor_network.REASON_USBNET_KILLSWITCHED


def test_usbnet_interface_kill_switched_but_iface_present_is_warn(monkeypatch, tmp_path):
    """Belt-and-suspenders: if usb0 is somehow still up while the kill
    switch is set, that's drift worth a nudge to recompose, not silence."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled")
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    r = doctor_network.check_usbnet_interface()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_USBNET_IFACE_KILLSWITCH_DRIFT


def test_usbnet_interface_no_udc_pre_reboot_is_ok(monkeypatch, tmp_path):
    """usb0 absent and no UDC on a gadget-capable pre-reboot install: the
    gadget cannot bind, so usb0's absence is expected, not a failure.
    check_usb_data_role owns the reboot prompt."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    # Empty UDC dir (exists but no controller) → no UDC present.
    udc_dir = tmp_path / "udc"
    udc_dir.mkdir()
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))
    r = doctor_network.check_usbnet_interface()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_NO_UDC


def test_usbnet_interface_absent_with_udc_is_fail(monkeypatch, tmp_path):
    """usb0 absent while a UDC IS present and the network is wanted means the
    gadget composed+bind FAILED — u_ether registers usb0 at bind time, so a
    bound NCM gadget always has usb0. This is a real failure (the fallback
    management network is down), not 'nothing plugged in' (review core-3)."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    udc_dir = tmp_path / "udc"
    (udc_dir / "3f980000.usb").mkdir(parents=True)
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))
    r = doctor_network.check_usbnet_interface()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_BIND_FAILED


def test_usbnet_interface_intentional_host_role_is_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net"
    )
    monkeypatch.setattr(
        doctor_network,
        "current_usb_data_role",
        lambda: SimpleNamespace(
            gadget_available=False,
            management_transport_available=False,
            reboot_required=False,
            reason="shared_otg_usb_output_requires_host",
        ),
    )

    result = doctor_network.check_usbnet_interface()

    assert result.status == "skipped"
    assert result.reason == doctor_network.REASON_USBNET_NOT_APPLICABLE


def test_usbnet_interface_role_change_pending_is_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net"
    )
    monkeypatch.setattr(
        doctor_network,
        "current_usb_data_role",
        lambda: SimpleNamespace(
            gadget_available=False,
            management_transport_available=False,
            reboot_required=True,
            reason="role_change_pending_reboot",
        ),
    )

    r = doctor_network.check_usbnet_interface()
    assert r.status == "ok"
    assert r.reason == doctor_network.REASON_USBNET_ROLE_CHANGE_PENDING


@pytest.mark.parametrize(
    "ip_stdout,pending_exists,role_reboot_required,expected_reason",
    [
        pytest.param(
            "3: usb0    <no address>\n", True, False,
            "REASON_USBNET_ADDR_PENDING", id="addr_pending",
        ),
        pytest.param(
            f"3: usb0    inet {PLAN.device_cidr} brd {PLAN.broadcast_address} "
            "scope global usb0\n",
            False, True,
            "REASON_USBNET_ROLE_CHANGE_PENDING", id="role_change_pending",
        ),
    ],
)
def test_usbnet_interface_pending_rows_are_ok(
    monkeypatch, tmp_path, ip_stdout, pending_exists, role_reboot_required,
    expected_reason,
):
    """Both are boot-order artifacts of a change already applied — the
    preserved legacy generation or an addressed usb0 awaiting a host-role
    reboot — not faults (ADR-0233)."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("ip", "-4", "-o", "addr", "show", "dev", "usb0"):
            subprocess.CompletedProcess([], 0, stdout=ip_stdout, stderr=""),
    })
    pending = tmp_path / ("pending" if pending_exists else "no-such-pending")
    if pending_exists:
        pending.write_text("pending\n")
    monkeypatch.setattr(doctor_network, "DEFAULT_PENDING_PATH", pending)
    monkeypatch.setattr(
        doctor_network,
        "current_usb_data_role",
        lambda: SimpleNamespace(
            gadget_available=True,
            management_transport_available=True,
            reboot_required=role_reboot_required,
            reason="role_change_pending_reboot" if role_reboot_required else "available",
        ),
    )

    r = doctor_network.check_usbnet_interface()
    assert r.status == "ok"
    assert r.reason == getattr(doctor_network, expected_reason)


def test_usbnet_interface_present_with_address_is_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    net_root = tmp_path / "sys-class-net"
    iface = net_root / "usb0"
    iface.mkdir(parents=True)
    (iface / "carrier").write_text("1\n")
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("ip", "-4", "-o", "addr", "show", "dev", "usb0"): subprocess.CompletedProcess(
            [], 0,
            stdout=f"3: usb0    inet {PLAN.device_cidr} brd {PLAN.broadcast_address} scope global usb0\n",
            stderr="",
        ),
    })
    r = doctor_network.check_usbnet_interface()
    assert r.status == "ok"
    assert r.reason == ""
    # The plan-derived address and observed carrier state are the fact this
    # check exists to disclose — data the reason code has no room for.
    assert PLAN.device_cidr in r.detail
    assert "carrier=up" in r.detail


def test_usbnet_interface_present_no_carrier_is_ok(monkeypatch, tmp_path):
    """No carrier (nothing plugged into the composed NCM link at the
    moment) is normal, not an error — usb0 exists at gadget-bind time
    regardless of the cable, so an addressed usb0 with carrier down is the
    ordinary nothing-plugged-in state."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    net_root = tmp_path / "sys-class-net"
    iface = net_root / "usb0"
    iface.mkdir(parents=True)
    (iface / "carrier").write_text("0\n")
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("ip", "-4", "-o", "addr", "show", "dev", "usb0"): subprocess.CompletedProcess(
            [], 0, stdout=f"3: usb0    inet {PLAN.device_cidr} scope global usb0\n", stderr="",
        ),
    })
    r = doctor_network.check_usbnet_interface()
    assert r.status == "ok"
    assert r.reason == ""
    assert "carrier=down" in r.detail


def test_usbnet_interface_present_missing_address_is_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    net_root = tmp_path / "sys-class-net"
    iface = net_root / "usb0"
    iface.mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("ip", "-4", "-o", "addr", "show", "dev", "usb0"): subprocess.CompletedProcess(
            [], 0, stdout="3: usb0    <no address>\\n", stderr="",
        ),
    })
    r = doctor_network.check_usbnet_interface()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_ADDR_MISSING


def test_usbnet_interface_ip_command_failure_is_warn(monkeypatch, tmp_path):
    """sysfs already confirmed usb0 exists; `ip addr show` failing on it is
    the interface vanishing mid-probe — the same event the sibling
    management-probe row keeps as warn, not a missing evidence channel."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("ip", "-4", "-o", "addr", "show", "dev", "usb0"): subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Device \"usb0\" does not exist.",
        ),
    })
    r = doctor_network.check_usbnet_interface()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_USBNET_ADDR_PROBE_FAILED


# ----------------------------------------------------------------------
# check_usbnet_nm_profile
# ----------------------------------------------------------------------


def test_usbnet_nm_profile_skips_no_iface(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_NOT_APPLICABLE


def test_usbnet_nm_profile_skips_no_nmcli(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    monkeypatch.setattr(doctor_network.shutil, "which", lambda name: None)
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_SKIPPED_NO_NMCLI


def _with_usb0_and_nmcli(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/usr/bin/nmcli",
    )


def test_usbnet_nm_profile_active_matches_is_ok(monkeypatch, tmp_path):
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "TYPE,DEVICE,NAME"): subprocess.CompletedProcess(
            [], 0,
            stdout="tun:usb0:jts-usb\n802-11-wireless:wlan0:Home WiFi\n",
            stderr="",
        ),
    })
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "ok"
    assert r.reason == ""


def test_usbnet_nm_profile_no_active_connection_on_usb0_is_fail(monkeypatch, tmp_path):
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "TYPE,DEVICE,NAME"): subprocess.CompletedProcess(
            [], 0, stdout="802-11-wireless:wlan0:Home WiFi\n", stderr="",
        ),
    })
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_NM_PROFILE_MISSING


def test_usbnet_nm_profile_wrong_profile_on_usb0_is_fail(monkeypatch, tmp_path):
    """A manual nmcli override or install regression bound something
    other than the shipped jts-usb profile to usb0."""
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "TYPE,DEVICE,NAME"): subprocess.CompletedProcess(
            [], 0, stdout="tun:usb0:netplan-usb0-legacy\n", stderr="",
        ),
    })
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_NM_PROFILE_MISMATCH


def test_usbnet_nm_profile_nmcli_failure_is_warn(monkeypatch, tmp_path):
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "TYPE,DEVICE,NAME"): subprocess.CompletedProcess(
            [], 1, stdout="", stderr="nmcli: command failed",
        ),
    })
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_USBNET_NM_QUERY_FAILED


def test_usbnet_nm_profile_colon_bearing_name_unescaped(monkeypatch, tmp_path):
    """A profile NAME containing a literal colon (nmcli escapes it as
    \\:) must still parse correctly. NAME is requested LAST
    (TYPE,DEVICE,NAME) and split greedily (maxsplit=2, the same
    colon-safe shape `active_wifi_connection` uses), so an escaped colon
    inside it is never mistaken for a field separator; `_nm_unescape`
    reverses the escape for the reported name. Also confirms this
    differently-named profile is correctly reported as a mismatch rather
    than being misparsed into a false match."""
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "TYPE,DEVICE,NAME"): subprocess.CompletedProcess(
            [], 0, stdout="tun:usb0:legacy\\:profile\n", stderr="",
        ),
    })
    r = doctor_network.check_usbnet_nm_profile()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_NM_PROFILE_MISMATCH
    # Pins the colon-unescape correctness end-to-end; the reason code has no
    # room for which profile name was actually resolved.
    assert "legacy:profile" in r.detail


# ----------------------------------------------------------------------
# check_usbnet_dhcp_unit
# ----------------------------------------------------------------------


def test_usbnet_dhcp_unit_skips_no_systemctl(monkeypatch):
    monkeypatch.setattr(doctor_network.shutil, "which", lambda name: None)
    r = doctor_network.check_usbnet_dhcp_unit()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_SKIPPED_NO_SYSTEMCTL


def test_usbnet_dhcp_unit_skips_not_installed(monkeypatch):
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    _seed_unit_states(**{
        doctor_network.USBNET_DHCP_UNIT: {"load_state": "not-found"},
    })
    r = doctor_network.check_usbnet_dhcp_unit()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_DHCP_NOT_INSTALLED


def test_usbnet_dhcp_unit_active_with_iface_present_is_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _seed_unit_states(**{
        doctor_network.USBNET_DHCP_UNIT: {
            "load_state": "loaded", "active_state": "active",
        },
    })
    r = doctor_network.check_usbnet_dhcp_unit()
    assert r.status == "ok"
    assert r.reason == ""


def test_usbnet_dhcp_unit_inactive_with_iface_absent_is_ok(monkeypatch, tmp_path):
    """Zero-cost: usb0 absent (the NCM gadget is not composed — kill-switched
    or no UDC), dnsmasq correctly not started by the device activation."""
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    _seed_unit_states(**{
        doctor_network.USBNET_DHCP_UNIT: {
            "load_state": "loaded", "active_state": "inactive",
        },
    })
    r = doctor_network.check_usbnet_dhcp_unit()
    assert r.status == "ok"
    assert r.reason == doctor_network.REASON_USBNET_DHCP_IDLE


def test_usbnet_dhcp_unit_iface_present_but_unit_inactive_is_fail(monkeypatch, tmp_path):
    """usb0 exists because NCM is composed, but dnsmasq never started, so a
    host that connects will not get a DHCP lease."""
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _seed_unit_states(**{
        doctor_network.USBNET_DHCP_UNIT: {
            "load_state": "loaded", "active_state": "inactive",
        },
    })
    r = doctor_network.check_usbnet_dhcp_unit()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_DHCP_NOT_SERVING


def test_usbnet_dhcp_unit_iface_absent_but_unit_active_is_warn(monkeypatch, tmp_path):
    """The mirror case: the unit is still active after usb0 disappeared. This
    is device-activation teardown drift, not a live link failure because no USB
    network interface remains to serve."""
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    _seed_unit_states(**{
        doctor_network.USBNET_DHCP_UNIT: {
            "load_state": "loaded", "active_state": "active",
        },
    })
    r = doctor_network.check_usbnet_dhcp_unit()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_USBNET_DHCP_TEARDOWN_DRIFT


# ----------------------------------------------------------------------
# check_usbnet_management_probe
# ----------------------------------------------------------------------


def test_usbnet_probe_skips_no_iface(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    r = doctor_network.check_usbnet_management_probe()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_NOT_APPLICABLE


def test_usbnet_probe_skips_no_nginx_site(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    monkeypatch.setattr(web, "NGINX_SITE", tmp_path / "absent.conf")
    r = doctor_network.check_usbnet_management_probe()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_NGINX_NOT_INSTALLED


def _iface_and_nginx(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    site = tmp_path / "jasper.conf"
    site.write_text("# nginx site\n")
    monkeypatch.setattr(web, "NGINX_SITE", site)


@pytest.mark.parametrize(
    "check,address,http_reason,no_answer_reason",
    [
        (web.check_management_surface, "127.0.0.1",
         web.REASON_MANAGEMENT_HTTP_ERROR, web.REASON_MANAGEMENT_NO_ANSWER),
        (doctor_network.check_usbnet_management_probe, PLAN.device_address,
         doctor_network.REASON_USBNET_PROBE_HTTP_ERROR,
         doctor_network.REASON_USBNET_PROBE_NO_ANSWER),
    ],
    ids=["loopback", "usb"],
)
@pytest.mark.parametrize(
    "outcome", [200, 403, 502, 503, "bodyless", "refused", "timeout"],
)
def test_management_probes(
    monkeypatch, tmp_path, check, address, http_reason, no_answer_reason, outcome,
):
    _iface_and_nginx(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    url = f"http://{address}/system/data.json"
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    response.read.return_value = b"{}"
    failure = None
    if isinstance(outcome, int) and outcome != 200:
        failure = urllib.error.HTTPError(url, outcome, "failed", None, response)
    elif outcome == "bodyless":
        failure = urllib.error.HTTPError(url, 502, "failed", None, None)
    elif outcome == "refused":
        failure = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    elif outcome == "timeout":
        failure = TimeoutError()

    with patch("urllib.request.urlopen", return_value=response, side_effect=failure) as opened:
        result = check()

    expected_reason = (
        "" if outcome == 200 else
        no_answer_reason if outcome in ("refused", "timeout") else http_reason
    )
    assert (result.status, result.reason, result.speaker_silent) == (
        "ok" if outcome == 200 else "fail", expected_reason, False,
    )
    req = opened.call_args.args[0]
    assert req.full_url == url
    assert req.get_header("Host") == "jts3.local"
    assert opened.call_args.kwargs == {"timeout": 6.0}
    if isinstance(outcome, int):
        response.read.assert_called_once_with(512)
    else:
        response.read.assert_not_called()


def test_usbnet_probe_ipv4_inspection_error_fails_loudly(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doctor_network,
        "observe_ipv4_cidr",
        lambda _iface: IPv4Observation(
            IPv4ObservationState.ERROR, error="inspection denied"
        ),
    )

    result = doctor_network.check_usbnet_management_probe()

    assert result.status == "fail"
    assert result.reason == doctor_network.REASON_USBNET_PROBE_IPV4_UNREADABLE


def test_usbnet_probe_existing_interface_without_ipv4_fails(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doctor_network,
        "observe_ipv4_cidr",
        lambda _iface: IPv4Observation(IPv4ObservationState.NO_ADDRESS),
    )

    result = doctor_network.check_usbnet_management_probe()

    assert result.status == "fail"
    assert result.reason == doctor_network.REASON_USBNET_PROBE_NO_ADDRESS


# ===========================================================================
# check_wifi_regdom / check_wifi_guardian / check_wifi_link_local_ipv6 /
# check_wifi_recover_timer — one seed/patch setup per behavior, returning the
# CheckResult, with one status+reason (/ detail substring) assertion tail
# (AGENTS.md: one altitude per behavior, prefer one parametrized test over an
# example cluster). Test ids equal the old per-behavior function names so
# `pytest -k` and CI history keep working.
# ===========================================================================


def _wifi_case_regdom_ok_unlabeled_phy(monkeypatch, tmp_path):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country US: DFS-FCC
\t(2400 - 2472 @ 40), (N/A, 30), (N/A)

phy#0
country 99: DFS-UNSET
\t(2402 - 2482 @ 40), (6, 20), (N/A)
""",
    )
    return doctor_network.check_wifi_regdom()


def _wifi_case_regdom_warns_country_unset(monkeypatch, tmp_path):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country 00: DFS-UNSET

phy#0
country 99: DFS-UNSET
""",
    )
    return doctor_network.check_wifi_regdom()


def _wifi_case_regdom_skips(stdout, returncode):
    def _case(monkeypatch, tmp_path):
        _patch_doctor_iw_reg_get(monkeypatch, stdout, returncode=returncode)
        return doctor_network.check_wifi_regdom()

    return _case


def _wifi_case_regdom_ok_no_phy(monkeypatch, tmp_path):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country DE: DFS-ETSI
""",
    )
    return doctor_network.check_wifi_regdom()


def _wifi_case_guardian_ok_stash_matches_active(monkeypatch, tmp_path):
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(
        monkeypatch,
        ["802-11-wireless:wlan0:Home\n", "802-11-wireless.ssid:Home\n"],
    )
    return doctor_network.check_wifi_guardian()


def _wifi_case_guardian_ok_ethernet_only(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(tmp_path / "missing.env"))
    _patch_doctor_nmcli(monkeypatch, ["802-3-ethernet:eth0:Wired connection 1\n"])
    return doctor_network.check_wifi_guardian()


def _wifi_case_guardian_warns_stash_missing_but_active(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(tmp_path / "missing.env"))
    _patch_doctor_nmcli(monkeypatch, ["802-11-wireless:wlan0:Home\n", "802-11-wireless.ssid:Home\n"])
    return doctor_network.check_wifi_guardian()


def _wifi_case_guardian_warns_ssid_drift(monkeypatch, tmp_path):
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(monkeypatch, ["802-11-wireless:wlan0:Cafe\n", "802-11-wireless.ssid:Cafe\n"])
    return doctor_network.check_wifi_guardian()


def _wifi_case_guardian_matches_colon_ssid(monkeypatch, tmp_path):
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home:5G\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            # active connection NAME "Home:5G" arrives colon-escaped from nmcli -t
            "802-11-wireless:wlan0:Home\\:5G\n",
            # ssid value lookup fails -> fall back to the unescaped profile name
            _mock_nmcli_proc(returncode=1),
        ],
    )
    return doctor_network.check_wifi_guardian()


def _wifi_case_guardian_warns_active_wifi_missing(monkeypatch, tmp_path):
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(monkeypatch, [""])
    return doctor_network.check_wifi_guardian()


def _wifi_case_guardian_skipped_without_nmcli(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "nmcli" else f"/usr/bin/{name}")
    return doctor_network.check_wifi_guardian()


def _wifi_case_link_local_ipv6_ok(monkeypatch, tmp_path):
    _patch_doctor_nmcli(
        monkeypatch,
        ["802-11-wireless:wlan0:Home\n", "link-local\n", "2: wlan0    inet6 fe80::1/64 scope link\n"],
    )
    return doctor_network.check_wifi_link_local_ipv6()


def _wifi_case_link_local_ipv6_warns_ignores_ipv6(monkeypatch, tmp_path):
    _patch_doctor_nmcli(monkeypatch, ["802-11-wireless:wlan0:Home\\:5G\n", "ignore\n"])
    return doctor_network.check_wifi_link_local_ipv6()


def _wifi_case_link_local_ipv6_warns_link_local_missing(monkeypatch, tmp_path):
    _patch_doctor_nmcli(monkeypatch, ["802-11-wireless:wlan0:Home\n", "auto\n", ""])
    return doctor_network.check_wifi_link_local_ipv6()


def _wifi_case_recover_timer_enabled_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    _seed_unit_states(**{"jasper-wifi-recover.timer": {"load_state": "loaded", "unit_file_state": "enabled"}})
    return doctor_network.check_wifi_recover_timer()


def _wifi_case_recover_timer_disabled_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    _seed_unit_states(**{"jasper-wifi-recover.timer": {"load_state": "loaded", "unit_file_state": "disabled"}})
    return doctor_network.check_wifi_recover_timer()


def _wifi_case_recover_timer_not_installed_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    _seed_unit_states(**{"jasper-wifi-recover.timer": {"load_state": "not-found"}})
    return doctor_network.check_wifi_recover_timer()


def _wifi_case_recover_timer_no_systemctl_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_network.shutil, "which", lambda _x: None)
    return doctor_network.check_wifi_recover_timer()


_N = doctor_network


@pytest.mark.parametrize(
    "setup, expected_status, expected_reason, extra",
    [
        pytest.param(_wifi_case_regdom_ok_unlabeled_phy, "ok", "", {"detail_contains": ("global country=US", "phy0 country=99", "not actionable by itself")}, id="test_check_wifi_regdom_ok_when_global_country_valid_and_phy_unlabeled"),
        pytest.param(_wifi_case_regdom_warns_country_unset, "warn", _N.REASON_REGDOM_UNSET, None, id="test_check_wifi_regdom_warns_when_global_country_unset"),
        pytest.param(_wifi_case_regdom_skips("", 1), "skipped", _N.REASON_REGDOM_PROBE_FAILED, None, id="test_check_wifi_regdom_skips_when_no_country_was_observed[probe-failed]"),
        pytest.param(_wifi_case_regdom_skips("global\n", 0), "skipped", _N.REASON_REGDOM_UNPARSEABLE, None, id="test_check_wifi_regdom_skips_when_no_country_was_observed[no-global-country]"),
        pytest.param(_wifi_case_regdom_ok_no_phy, "ok", "", {"detail_contains": ("global country=DE", "no per-phy regdom reported")}, id="test_check_wifi_regdom_ok_with_valid_global_and_no_phy"),
        pytest.param(_wifi_case_guardian_ok_stash_matches_active, "ok", "", None, id="test_check_wifi_guardian_ok_when_stash_matches_active"),
        pytest.param(_wifi_case_guardian_ok_ethernet_only, "skipped", _N.REASON_GUARDIAN_NOT_APPLICABLE, None, id="test_check_wifi_guardian_ok_ethernet_only"),
        pytest.param(_wifi_case_guardian_warns_stash_missing_but_active, "warn", _N.REASON_GUARDIAN_STASH_MISSING, None, id="test_check_wifi_guardian_warns_when_stash_missing_but_active"),
        pytest.param(_wifi_case_guardian_warns_ssid_drift, "warn", _N.REASON_GUARDIAN_SSID_DRIFT, None, id="test_check_wifi_guardian_warns_on_ssid_drift"),
        pytest.param(_wifi_case_guardian_matches_colon_ssid, "ok", "", {"detail_contains": ("Home:5G",)}, id="test_check_wifi_guardian_matches_colon_ssid"),
        pytest.param(_wifi_case_guardian_warns_active_wifi_missing, "warn", _N.REASON_GUARDIAN_NO_ACTIVE_WIFI, None, id="test_check_wifi_guardian_warns_when_active_wifi_missing"),
        pytest.param(_wifi_case_guardian_skipped_without_nmcli, "skipped", _N.REASON_GUARDIAN_SKIPPED_NO_NMCLI, None, id="test_check_wifi_guardian_skipped_without_nmcli"),
        pytest.param(_wifi_case_link_local_ipv6_ok, "ok", "", None, id="test_check_wifi_link_local_ipv6_ok"),
        pytest.param(_wifi_case_link_local_ipv6_warns_ignores_ipv6, "warn", _N.REASON_IPV6_METHOD_DISABLED, {"detail_contains": ("active WiFi profile 'Home:5G'", "nmcli connection modify Home:5G ipv6.method link-local")}, id="test_check_wifi_link_local_ipv6_warns_when_profile_ignores_ipv6"),
        pytest.param(_wifi_case_link_local_ipv6_warns_link_local_missing, "warn", _N.REASON_IPV6_LINK_LOCAL_MISSING, None, id="test_check_wifi_link_local_ipv6_warns_when_link_local_missing"),
        pytest.param(_wifi_case_recover_timer_enabled_ok, "ok", "", None, id="test_check_wifi_recover_timer_enabled_ok"),
        pytest.param(_wifi_case_recover_timer_disabled_warns, "warn", _N.REASON_RECOVER_TIMER_DISABLED, None, id="test_check_wifi_recover_timer_disabled_warns"),
        pytest.param(_wifi_case_recover_timer_not_installed_skips, "skipped", _N.REASON_RECOVER_TIMER_NOT_INSTALLED, None, id="test_check_wifi_recover_timer_not_installed_skips"),
        pytest.param(_wifi_case_recover_timer_no_systemctl_skips, "skipped", _N.REASON_RECOVER_TIMER_SKIPPED_NO_SYSTEMCTL, None, id="test_check_wifi_recover_timer_no_systemctl_skips"),
    ],
)
def test_check_wifi_status(monkeypatch, tmp_path, setup, expected_status, expected_reason, extra):
    r = setup(monkeypatch, tmp_path)

    assert r.status == expected_status
    assert r.reason == expected_reason
    for substr in (extra or {}).get("detail_contains", ()):
        assert substr in r.detail
