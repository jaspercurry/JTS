# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's USB management-network checks
(jasper/cli/doctor/network.py's check_usbnet_*).

These verify the *network* side of the composite-gadget intent — the
usb0 interface, its NetworkManager profile, the device-activated dnsmasq
unit, and a loopback probe of the fallback management URL. The
composite-gadget *function* composition (ncm.usb0/uac2.usb0 vs. intent)
is jasper/cli/doctor/usbsink.py's check_usbgadget_composition, covered in
tests/test_doctor_usbsink.py. All hardware-side reads (sysfs, nmcli,
systemctl, urllib) are monkeypatched; Pi-side smoke testing happens via
jasper-doctor itself.
"""
from __future__ import annotations

import io
import subprocess
import urllib.error
from unittest.mock import patch

from jasper.cli import doctor
from jasper.cli.doctor import network as doctor_network


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
    r = doctor.check_usbnet_interface()
    assert r.status == "ok"
    assert "kill-switched" in r.detail.lower()


def test_usbnet_interface_kill_switched_but_iface_present_is_warn(monkeypatch, tmp_path):
    """Belt-and-suspenders: if usb0 is somehow still up while the kill
    switch is set, that's drift worth a nudge to recompose, not silence."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "disabled")
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    r = doctor.check_usbnet_interface()
    assert r.status == "warn"
    assert "disabled" in r.detail.lower()


def test_usbnet_interface_no_udc_pre_reboot_is_ok(monkeypatch, tmp_path):
    """usb0 absent AND no UDC (fresh install pre-reboot / non-gadget hardware):
    the gadget cannot bind, so usb0's absence is expected — ok, not a failure.
    check_usbsink_dtoverlay owns the reboot prompt."""
    monkeypatch.setenv("JASPER_USB_NETWORK", "enabled")
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    # Empty UDC dir (exists but no controller) → no UDC present.
    udc_dir = tmp_path / "udc"
    udc_dir.mkdir()
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc_dir))
    r = doctor.check_usbnet_interface()
    assert r.status == "ok"
    assert "no udc" in r.detail.lower()


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
    r = doctor.check_usbnet_interface()
    assert r.status == "fail"
    assert "compose/bind" in r.detail.lower() or "did not compose" in r.detail.lower()


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
            stdout="3: usb0    inet 10.12.194.1/24 brd 10.12.194.255 scope global usb0\\n",
            stderr="",
        ),
    })
    r = doctor.check_usbnet_interface()
    assert r.status == "ok"
    assert "10.12.194.1" in r.detail
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
            [], 0, stdout="3: usb0    inet 10.12.194.1/24 scope global usb0\\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_interface()
    assert r.status == "ok"
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
    r = doctor.check_usbnet_interface()
    assert r.status == "fail"
    assert "missing 10.12.194.1" in r.detail


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
    r = doctor.check_usbnet_interface()
    assert r.status == "warn"
    assert "ip addr show" in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbnet_nm_profile
# ----------------------------------------------------------------------


def test_usbnet_nm_profile_skips_no_iface(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "ok"
    assert "not present" in r.detail.lower()


def test_usbnet_nm_profile_skips_no_nmcli(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    monkeypatch.setattr(doctor_network.shutil, "which", lambda name: None)
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "ok"
    assert "no nmcli" in r.detail.lower()


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
        ("/usr/bin/nmcli", "-t", "-f", "NAME,DEVICE"): subprocess.CompletedProcess(
            [], 0, stdout="jts-usb:usb0\nHome WiFi:wlan0\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "ok"
    assert "jts-usb active on usb0" in r.detail


def test_usbnet_nm_profile_no_active_connection_on_usb0_is_fail(monkeypatch, tmp_path):
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "NAME,DEVICE"): subprocess.CompletedProcess(
            [], 0, stdout="Home WiFi:wlan0\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "fail"
    assert "no active connection" in r.detail.lower()


def test_usbnet_nm_profile_wrong_profile_on_usb0_is_fail(monkeypatch, tmp_path):
    """A manual nmcli override or install regression bound something
    other than the shipped jts-usb profile to usb0."""
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "NAME,DEVICE"): subprocess.CompletedProcess(
            [], 0, stdout="netplan-usb0-legacy:usb0\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "fail"
    assert "netplan-usb0-legacy" in r.detail
    assert "jts-usb" in r.detail


def test_usbnet_nm_profile_nmcli_failure_is_warn(monkeypatch, tmp_path):
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "NAME,DEVICE"): subprocess.CompletedProcess(
            [], 1, stdout="", stderr="nmcli: command failed",
        ),
    })
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "warn"
    assert "failed" in r.detail.lower()


def test_usbnet_nm_profile_colon_bearing_name_unescaped(monkeypatch, tmp_path):
    """A profile NAME containing a literal colon (nmcli escapes it as
    \\:) must still parse correctly — DEVICE never contains a colon, so
    an rsplit(":", 1) isolates it regardless of escaped colons inside
    NAME, and _nm_unescape reverses the escape for the reported name.
    Also confirms this differently-named profile is correctly reported
    as a mismatch rather than being misparsed into a false match."""
    _with_usb0_and_nmcli(monkeypatch, tmp_path)
    _stub_run(monkeypatch, {
        ("/usr/bin/nmcli", "-t", "-f", "NAME,DEVICE"): subprocess.CompletedProcess(
            [], 0, stdout=r"legacy\:profile:usb0" + "\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_nm_profile()
    assert r.status == "fail"
    assert "legacy:profile" in r.detail
    assert "jts-usb" in r.detail


# ----------------------------------------------------------------------
# check_usbnet_dhcp_unit
# ----------------------------------------------------------------------


def test_usbnet_dhcp_unit_skips_no_systemctl(monkeypatch):
    monkeypatch.setattr(doctor_network.shutil, "which", lambda name: None)
    r = doctor.check_usbnet_dhcp_unit()
    assert r.status == "ok"
    assert "no systemctl" in r.detail.lower()


def test_usbnet_dhcp_unit_skips_not_installed(monkeypatch):
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    _stub_run(monkeypatch, {
        ("systemctl", "is-active"): subprocess.CompletedProcess(
            [], 3, stdout="inactive\n",
            stderr="Unit jasper-usbnet-dhcp.service could not be found.",
        ),
    })
    r = doctor.check_usbnet_dhcp_unit()
    assert r.status == "ok"
    assert "not installed" in r.detail.lower()


def test_usbnet_dhcp_unit_active_with_iface_present_is_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("systemctl", "is-active"): subprocess.CompletedProcess(
            [], 0, stdout="active\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_dhcp_unit()
    assert r.status == "ok"
    assert "active" in r.detail.lower()


def test_usbnet_dhcp_unit_inactive_with_iface_absent_is_ok(monkeypatch, tmp_path):
    """Zero-cost: usb0 absent (the NCM gadget is not composed — kill-switched
    or no UDC), dnsmasq correctly not started by the device activation."""
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    _stub_run(monkeypatch, {
        ("systemctl", "is-active"): subprocess.CompletedProcess(
            [], 3, stdout="inactive\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_dhcp_unit()
    assert r.status == "ok"
    assert "no cost" in r.detail.lower()


def test_usbnet_dhcp_unit_iface_present_but_unit_inactive_is_fail(monkeypatch, tmp_path):
    """usb0 exists (a host is plugged in) but dnsmasq never started —
    the plugged-in host won't get a DHCP lease."""
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    _stub_run(monkeypatch, {
        ("systemctl", "is-active"): subprocess.CompletedProcess(
            [], 3, stdout="inactive\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_dhcp_unit()
    assert r.status == "fail"
    assert "won't get a dhcp lease" in r.detail.lower()


def test_usbnet_dhcp_unit_iface_absent_but_unit_active_is_warn(monkeypatch, tmp_path):
    """The mirror case: unit still active after usb0 disappeared — a
    device-activation teardown drift, not a functional problem since
    nothing is plugged in to use it."""
    monkeypatch.setattr(
        doctor_network.shutil, "which", lambda name: "/bin/systemctl",
    )
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    _stub_run(monkeypatch, {
        ("systemctl", "is-active"): subprocess.CompletedProcess(
            [], 0, stdout="active\n", stderr="",
        ),
    })
    r = doctor.check_usbnet_dhcp_unit()
    assert r.status == "warn"
    assert "drift" in r.detail.lower()


# ----------------------------------------------------------------------
# check_usbnet_management_probe
# ----------------------------------------------------------------------


def test_usbnet_probe_skips_no_iface(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor_network, "USBNET_SYS_CLASS_NET", tmp_path / "sys-class-net",
    )
    r = doctor.check_usbnet_management_probe()
    assert r.status == "ok"
    assert "not present" in r.detail.lower()


def test_usbnet_probe_skips_no_nginx_site(monkeypatch, tmp_path):
    net_root = tmp_path / "sys-class-net"
    (net_root / "usb0").mkdir(parents=True)
    monkeypatch.setattr(doctor_network, "USBNET_SYS_CLASS_NET", net_root)
    monkeypatch.setattr(doctor.web, "NGINX_SITE", tmp_path / "absent.conf")
    r = doctor.check_usbnet_management_probe()
    assert r.status == "ok"
    assert "nginx site not installed" in r.detail.lower()


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
        r = doctor.check_usbnet_management_probe()
    assert r.status == "ok"
    assert "10.12.194.1" in r.detail
    assert "jts3.local" in r.detail
    req = m.call_args[0][0]
    assert req.full_url == "http://10.12.194.1/system/data.json"
    assert req.get_header("Host") == "jts3.local"


def test_usbnet_probe_403_fails_with_guard_hint(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        doctor_network.USBNET_PROBE_URL, 403, "Forbidden", None,
        io.BytesIO(b'{"error": "host_not_allowed"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor.check_usbnet_management_probe()
    assert r.status == "fail"
    assert "host_not_allowed" in r.detail
    assert "test_http_security" in r.detail


def test_usbnet_probe_502_fails_naming_control(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        doctor_network.USBNET_PROBE_URL, 502, "Bad Gateway", None,
        io.BytesIO(b'{"error": "jasper-control unreachable"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor.check_usbnet_management_probe()
    assert r.status == "fail"
    assert "jasper-control" in r.detail


def test_usbnet_probe_connection_refused_fails_naming_nginx(monkeypatch, tmp_path):
    _iface_and_nginx(monkeypatch, tmp_path)
    err = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor.check_usbnet_management_probe()
    assert r.status == "fail"
    assert "nginx" in r.detail.lower()
