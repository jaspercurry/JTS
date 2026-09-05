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

import io
import shutil
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import _evidence
from jasper.cli.doctor import network as doctor_network
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
    monkeypatch.setattr(doctor.network, "_run", fake_run)

    assert doctor.network._active_wifi_connection("nmcli") == ("Home:2.4G", "wlan0")
    assert "TYPE,DEVICE,NAME" in fake_run.calls[0]


def test_active_wifi_connection_nonzero_returncode(monkeypatch):
    """nmcli failure → (None, None), not a crash."""

    def fake_run(argv, *a, **kw):
        return _completed(argv, returncode=1)

    monkeypatch.setattr(doctor.network, "_run", fake_run)
    assert doctor.network._active_wifi_connection("nmcli") == (None, None)


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

    monkeypatch.setattr(doctor.network, "_run", fake_run)


def test_check_wifi_regdom_ok_when_global_country_valid_and_phy_unlabeled(
    monkeypatch,
):
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
    r = doctor_network.check_wifi_regdom()
    assert r.status == "ok"
    assert r.reason == ""
    # `_format_phy_regdom_detail` has no unit test of its own — this is its
    # only exercise, so the pure-formatting-helper `.detail` exception
    # applies here.
    assert "global country=US" in r.detail
    assert "phy0 country=99" in r.detail
    assert "not actionable by itself" in r.detail


def test_check_wifi_regdom_warns_when_global_country_unset(monkeypatch):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country 00: DFS-UNSET

phy#0
country 99: DFS-UNSET
""",
    )
    r = doctor_network.check_wifi_regdom()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_REGDOM_UNSET


def test_check_wifi_regdom_ok_with_valid_global_and_no_phy(monkeypatch):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country DE: DFS-ETSI
""",
    )
    r = doctor_network.check_wifi_regdom()
    assert r.status == "ok"
    assert r.reason == ""
    assert "global country=DE" in r.detail
    assert "no per-phy regdom reported" in r.detail


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
    """Patch shutil.which to return a path and doctor._run to return
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

    monkeypatch.setattr(doctor.network, "_run", fake_run)


def test_check_wifi_guardian_ok_when_stash_matches_active(
    monkeypatch,
    tmp_path,
):
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            # connection show --active (TYPE,DEVICE,NAME)
            "802-11-wireless:wlan0:Home\n",
            # connection show Home (ssid lookup)
            "802-11-wireless.ssid:Home\n",
        ],
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "ok"
    assert r.reason == ""


def test_check_wifi_guardian_ok_ethernet_only(monkeypatch, tmp_path):
    """No stash and no active WiFi → ethernet-only or never-configured
    Pi. Don't warn — there's nothing to recover and nothing to drift."""
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(tmp_path / "missing.env"))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            # connection show --active → no wifi line (TYPE,DEVICE,NAME)
            "802-3-ethernet:eth0:Wired connection 1\n",
        ],
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_GUARDIAN_NOT_APPLICABLE


def test_check_wifi_guardian_warns_when_stash_missing_but_active(
    monkeypatch,
    tmp_path,
):
    """WiFi works but the stash hasn't been seeded — operator brought
    up wifi via raspi-config or installed before our migration shipped.
    Warn so the dashboard / system check surfaces the recovery gap."""
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(tmp_path / "missing.env"))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "802-11-wireless:wlan0:Home\n",
            "802-11-wireless.ssid:Home\n",
        ],
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_GUARDIAN_STASH_MISSING


def test_check_wifi_guardian_warns_on_ssid_drift(monkeypatch, tmp_path):
    """Stash says Home, NM is on Cafe — operator switched via SSH and
    didn't re-save in the wizard. Warn so the next dirty shutdown
    doesn't recreate the wrong network."""
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "802-11-wireless:wlan0:Cafe\n",
            "802-11-wireless.ssid:Cafe\n",
        ],
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_GUARDIAN_SSID_DRIFT


def test_check_wifi_guardian_matches_colon_ssid(monkeypatch, tmp_path):
    """A profile NAME with a literal colon (e.g. "Home:5G") must be
    matched, not silently treated as "no active WiFi".

    Regression for the same colon-parse bug as C10-1: the guardian check
    used to run its own NAME-first nmcli probe, which mis-split an escaped
    "\\:" and reported a bogus "no recovery stash"/"no WiFi" state for a
    valid profile. It now reuses the colon-safe _active_wifi_connection.
    The SSID value lookup is forced to fail so the check falls back to the
    (unescaped) profile name, pinning the helper's output end-to-end."""
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home:5G\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            # active connection: NAME "Home:5G" arrives colon-escaped from nmcli -t
            "802-11-wireless:wlan0:Home\\:5G\n",
            # ssid value lookup fails → fall back to the unescaped profile name
            _mock_nmcli_proc(returncode=1),
        ],
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "ok"
    assert r.reason == ""
    # Pins the colon-unescape end-to-end (the exact regression this test
    # guards); the reason vocabulary has no code for "which SSID".
    assert "Home:5G" in r.detail


def test_check_wifi_guardian_warns_when_active_wifi_missing(
    monkeypatch,
    tmp_path,
):
    """Stash is configured but no WiFi is currently up. Either the
    guardian's last run failed, or NM was unable to bring up the
    network. Either way the operator should investigate."""
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "",  # no active wifi
        ],
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_GUARDIAN_NO_ACTIVE_WIFI


def test_check_wifi_guardian_skipped_without_nmcli(monkeypatch):
    """Pis without NetworkManager (or running this check in CI) →
    skip cleanly. The guardian itself is no-op on those machines."""
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: None if name == "nmcli" else f"/usr/bin/{name}",
    )
    r = doctor_network.check_wifi_guardian()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_GUARDIAN_SKIPPED_NO_NMCLI


def test_check_wifi_guardian_registered_in_sync_checks():
    """Make sure the check is actually registered to run (not just
    defined). Mirrors the spirit of the `check_wifi_regdom` registration
    this check sits next to."""
    assert "check_wifi_guardian" in _registered_check_names()


def test_check_wifi_link_local_ipv6_ok(monkeypatch):
    # nmcli -t -f TYPE,DEVICE,NAME connection show --active
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "802-11-wireless:wlan0:Home\n",
            "link-local\n",
            "2: wlan0    inet6 fe80::1/64 scope link\n",
        ],
    )
    r = doctor_network.check_wifi_link_local_ipv6()
    assert r.status == "ok"
    assert r.reason == ""


def test_check_wifi_link_local_ipv6_warns_when_profile_ignores_ipv6(monkeypatch):
    """Profile NAME carries a literal colon (e.g. "Home:5G"); it arrives
    escaped as "\\:" in nmcli -t output and must be unescaped into the
    remediation command's shlex.quote-preserved form — a genuine string-
    construction bug class the reason code can't capture, so this keeps the
    pure-formatting-helper `.detail` exception."""
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "802-11-wireless:wlan0:Home\\:5G\n",
            "ignore\n",
        ],
    )
    r = doctor_network.check_wifi_link_local_ipv6()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_IPV6_METHOD_DISABLED
    # Profile resolved with its colon intact (shlex.quote leaves a colon
    # name unquoted — colons need no shell escaping).
    assert "active WiFi profile 'Home:5G'" in r.detail
    assert "nmcli connection modify Home:5G ipv6.method link-local" in r.detail


def test_check_wifi_link_local_ipv6_warns_when_link_local_missing(monkeypatch):
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "802-11-wireless:wlan0:Home\n",
            "auto\n",
            "",
        ],
    )
    r = doctor_network.check_wifi_link_local_ipv6()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_IPV6_LINK_LOCAL_MISSING


def test_check_wifi_link_local_ipv6_registered_in_sync_checks():
    assert "check_wifi_link_local_ipv6" in _registered_check_names()


def test_check_avahi_jasper_control_ok_on_partial_timeout(monkeypatch):
    """Resolved avahi-browse can hang on stale sibling records after seeing
    the local service. That is still evidence that jasper-control is
    advertised; it should not crash the whole doctor run."""
    monkeypatch.setattr(
        doctor.network.shutil,
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

    monkeypatch.setattr(doctor.network, "_run", fake_run)

    r = doctor_network.check_avahi_jasper_control()

    assert r.status == "ok"
    assert r.reason == doctor_network.REASON_AVAHI_BROWSE_PARTIAL_TIMEOUT


def test_check_avahi_jasper_control_fails_on_timeout_without_service(
    monkeypatch,
):
    monkeypatch.setattr(
        doctor.network.shutil,
        "which",
        lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None,
    )

    def fake_run(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(cmd, timeout, output="")

    monkeypatch.setattr(doctor.network, "_run", fake_run)

    r = doctor_network.check_avahi_jasper_control()

    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_AVAHI_BROWSE_TIMEOUT


# ----- check_wifi_recover_timer (Wi-Fi flap recovery timer health) -----


def test_check_wifi_recover_timer_enabled_ok(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    _seed_unit_states(**{
        "jasper-wifi-recover.timer": {
            "load_state": "loaded", "unit_file_state": "enabled",
        },
    })
    r = doctor_network.check_wifi_recover_timer()
    assert r.status == "ok"
    assert r.reason == ""


def test_check_wifi_recover_timer_disabled_warns(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    _seed_unit_states(**{
        "jasper-wifi-recover.timer": {
            "load_state": "loaded", "unit_file_state": "disabled",
        },
    })
    r = doctor_network.check_wifi_recover_timer()
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_RECOVER_TIMER_DISABLED


def test_check_wifi_recover_timer_not_installed_skips(monkeypatch):
    """A dev box with systemctl but no JTS units: skip, don't warn."""
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    _seed_unit_states(**{"jasper-wifi-recover.timer": {"load_state": "not-found"}})
    r = doctor_network.check_wifi_recover_timer()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_RECOVER_TIMER_NOT_INSTALLED


def test_check_wifi_recover_timer_no_systemctl_skips(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: None)
    r = doctor_network.check_wifi_recover_timer()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_RECOVER_TIMER_SKIPPED_NO_SYSTEMCTL


# ------------------------------------------------- check_identity_coherence
#
# The reconciler writes identity.env; the check reads it via
# jasper.identity_state and reports whether the advertised name still matches
# what the operator configured.


@pytest.mark.parametrize(
    "kwargs, status, reason",
    [
        ({}, "ok", ""),
        # A collision means avahi renamed us: name the reachable address.
        (
            {"collision": "1", "drift": "1", "avahi": "jts3-2.local"},
            "warn",
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


def test_check_identity_coherence_warns_on_a_stale_snapshot(monkeypatch, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _write_identity_env(tmp_path, monkeypatch, checked_at=old)

    r = doctor_network.check_identity_coherence()

    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_IDENTITY_SNAPSHOT_STALE


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


def test_usbnet_address_plan_pending_migration_is_visible_warn(monkeypatch, tmp_path):
    pending = tmp_path / "pending"
    pending.write_text("pending\n")
    monkeypatch.setattr(doctor_network, "DEFAULT_PENDING_PATH", pending)

    result = doctor_network.check_usbnet_address_plan()

    assert result.status == "warn"
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


def test_usbnet_interface_role_change_pending_is_warn(monkeypatch, tmp_path):
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
    assert r.status == "warn"
    assert r.reason == doctor_network.REASON_USBNET_ROLE_CHANGE_PENDING


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
    monkeypatch.setattr(doctor.web, "NGINX_SITE", tmp_path / "absent.conf")
    r = doctor_network.check_usbnet_management_probe()
    assert r.status == "skipped"
    assert r.reason == doctor_network.REASON_USBNET_NGINX_NOT_INSTALLED


def _iface_and_nginx(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    site = tmp_path / "jasper.conf"
    site.write_text("# nginx site\n")
    monkeypatch.setattr(doctor.web, "NGINX_SITE", site)


class _Resp:
    def __init__(self, status: int):
        self.status = status

    def read(self, n=-1):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_usbnet_probe_200_is_ok(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    with patch("urllib.request.urlopen", return_value=_Resp(200)) as m:
        r = doctor_network.check_usbnet_management_probe()
    assert r.status == "ok"
    assert r.reason == ""
    # The observed address and Host header are the fact this probe exists
    # to disclose — data the reason code has no room for.
    assert PLAN.device_address in r.detail
    assert "jts3.local" in r.detail
    req = m.call_args[0][0]
    assert req.full_url == f"http://{PLAN.device_address}/system/data.json"
    assert req.get_header("Host") == "jts3.local"


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


def test_usbnet_probe_403_fails_with_guard_hint(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        f"http://{PLAN.device_address}/system/data.json", 403, "Forbidden", None,
        io.BytesIO(b'{"error": "host_not_allowed"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_network.check_usbnet_management_probe()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_PROBE_HTTP_ERROR
    # The 403/502/other statuses all share one reason code; the remediation
    # hint text is the only thing that discriminates which one fired, so it
    # stays a pure-formatting-helper `.detail` check.
    assert "host_not_allowed" in r.detail
    assert "test_http_security" in r.detail


def test_usbnet_probe_502_fails(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        f"http://{PLAN.device_address}/system/data.json", 502, "Bad Gateway", None,
        io.BytesIO(b'{"error": "jasper-control unreachable"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_network.check_usbnet_management_probe()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_PROBE_HTTP_ERROR


def test_usbnet_probe_connection_refused_fails_naming_nginx(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    err = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_network.check_usbnet_management_probe()
    assert r.status == "fail"
    assert r.reason == doctor_network.REASON_USBNET_PROBE_NO_ANSWER
