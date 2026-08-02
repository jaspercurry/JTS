# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor network domain."""

import subprocess
from types import SimpleNamespace


from jasper.cli import doctor


from .doctor_test_support import (
    _registered_check_names,
)

# -------------------------------------------------- active WiFi connection


def _nmcli_active_run(stdout: str):
    """Build a fake `_run` returning ``stdout`` for any nmcli invocation.

    Records the argv it was called with so tests can assert the field
    order requested from nmcli."""
    calls: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))

        class FakeRun:
            returncode = 0
            stdout = ""

        FakeRun.stdout = stdout
        return FakeRun()

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def test_active_wifi_connection_simple(monkeypatch):
    """Plain SSID with no colon resolves to (name, device)."""
    # nmcli -t -f TYPE,DEVICE,NAME connection show --active
    stdout = "802-11-wireless:wlan0:HomeWiFi\n"
    monkeypatch.setattr(doctor.network, "_run", _nmcli_active_run(stdout))

    name, device = doctor.network._active_wifi_connection("nmcli")

    assert name == "HomeWiFi"
    assert device == "wlan0"


def test_active_wifi_connection_handles_colon_in_ssid(monkeypatch):
    """An SSID containing a literal colon must still be matched.

    Real-world SSIDs like ``Home:2.4G`` / ``AT&T:5G`` appear in
    nmcli -t output with the colon escaped as ``\\:``. With the old
    NAME-first field order (``NAME,TYPE,DEVICE``) this row mis-parsed —
    the first ``\\:`` was treated as a field boundary, TYPE landed on
    ``2.4G`` (not a wifi type), and the active connection was silently
    missed, returning (None, None) for a valid profile. This test pins
    the colon-safe TYPE,DEVICE,NAME order + unescape and FAILS on the
    old order."""
    # As emitted by `nmcli -t -f TYPE,DEVICE,NAME connection show --active`:
    # the NAME field's literal colon is backslash-escaped.
    stdout = "802-11-wireless:wlan0:Home\\:2.4G\n"
    fake_run = _nmcli_active_run(stdout)
    monkeypatch.setattr(doctor.network, "_run", fake_run)

    name, device = doctor.network._active_wifi_connection("nmcli")

    assert name == "Home:2.4G", "colon-containing SSID must be unescaped, not dropped"
    assert device == "wlan0"
    # The variable-content NAME field must be requested last so fixed-format
    # TYPE/DEVICE tokens parse unambiguously.
    assert "TYPE,DEVICE,NAME" in fake_run.calls[0]


def test_active_wifi_connection_no_wifi_row(monkeypatch):
    """Only a non-wifi (ethernet) active connection → (None, None)."""
    stdout = "802-3-ethernet:eth0:Wired connection 1\n"
    monkeypatch.setattr(doctor.network, "_run", _nmcli_active_run(stdout))

    assert doctor.network._active_wifi_connection("nmcli") == (None, None)


def test_active_wifi_connection_nonzero_returncode(monkeypatch):
    """nmcli failure → (None, None), not a crash."""

    def fake_run(argv, *a, **kw):
        class FakeRun:
            returncode = 1
            stdout = ""

        return FakeRun()

    monkeypatch.setattr(doctor.network, "_run", fake_run)
    assert doctor.network._active_wifi_connection("nmcli") == (None, None)


# ---------------------------------------------------- check_wifi_regdom


def _patch_doctor_iw_reg_get(monkeypatch, stdout: str, returncode: int = 0):
    def fake_run(cmd, timeout=5.0):
        assert cmd == ["iw", "reg", "get"]
        return subprocess.CompletedProcess(
            cmd,
            returncode,
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
    r = doctor.check_wifi_regdom()
    assert r.status == "ok"
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
    r = doctor.check_wifi_regdom()
    assert r.status == "warn"
    assert "global regdom is '00'" in r.detail
    assert "do_wifi_country <CC>" in r.detail


def test_check_wifi_regdom_ok_with_valid_global_and_no_phy(monkeypatch):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country DE: DFS-ETSI
""",
    )
    r = doctor.check_wifi_regdom()
    assert r.status == "ok"
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
    import subprocess

    return subprocess.CompletedProcess(
        args=["nmcli"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
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
        doctor.shutil,
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
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"
    assert "matches" in r.detail.lower() or "home" in r.detail.lower()


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
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"


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
    r = doctor.check_wifi_guardian()
    assert r.status == "warn"
    assert "stash" in r.detail.lower()
    assert "/wifi/" in r.detail  # actionable: tells operator where to go


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
    r = doctor.check_wifi_guardian()
    assert r.status == "warn"
    assert "Home" in r.detail and "Cafe" in r.detail


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
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"
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
    r = doctor.check_wifi_guardian()
    assert r.status == "warn"
    assert "Home" in r.detail
    assert "guardian" in r.detail.lower()


def test_check_wifi_guardian_skipped_without_nmcli(monkeypatch):
    """Pis without NetworkManager (or running this check in CI) →
    skip cleanly. The guardian itself is no-op on those machines."""
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: None if name == "nmcli" else f"/usr/bin/{name}",
    )
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"
    assert "skipped" in r.detail


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
    r = doctor.check_wifi_link_local_ipv6()
    assert r.status == "ok"
    assert "link-local IPv6" in r.detail


def test_check_wifi_link_local_ipv6_warns_when_profile_ignores_ipv6(monkeypatch):
    # Profile NAME carries a literal colon (e.g. "Home:5G"); it arrives
    # escaped as "\:" in nmcli -t output and must be unescaped, not dropped.
    _patch_doctor_nmcli(
        monkeypatch,
        [
            "802-11-wireless:wlan0:Home\\:5G\n",
            "ignore\n",
        ],
    )
    r = doctor.check_wifi_link_local_ipv6()
    assert r.status == "warn"
    assert "ipv6.method=ignore" in r.detail
    assert "Apple clients" in r.detail
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
    r = doctor.check_wifi_link_local_ipv6()
    assert r.status == "warn"
    assert "no link-local IPv6" in r.detail


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

    r = doctor.check_avahi_jasper_control()

    assert r.status == "ok"
    assert "stale peer" in r.detail


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

    r = doctor.check_avahi_jasper_control()

    assert r.status == "fail"
    assert "timed out" in r.detail


# ----- check_wifi_recover_timer (Wi-Fi flap recovery timer health) -----


def test_check_wifi_recover_timer_enabled_ok(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    monkeypatch.setattr(
        doctor.network,
        "_run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="enabled\n", stderr=""),
    )
    r = doctor.check_wifi_recover_timer()
    assert r.status == "ok"
    assert "enabled" in r.detail


def test_check_wifi_recover_timer_disabled_warns(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    monkeypatch.setattr(
        doctor.network,
        "_run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="disabled\n", stderr=""),
    )
    r = doctor.check_wifi_recover_timer()
    assert r.status == "warn"
    assert "enable --now jasper-wifi-recover.timer" in r.detail


def test_check_wifi_recover_timer_not_installed_skips(monkeypatch):
    """A dev box with systemctl but no JTS units: skip, don't warn."""
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl")
    monkeypatch.setattr(
        doctor.network,
        "_run",
        lambda *a, **k: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Failed to get unit file state ...: No such file or directory\n",
        ),
    )
    r = doctor.check_wifi_recover_timer()
    assert r.status == "ok"
    assert "not installed" in r.detail


def test_check_wifi_recover_timer_no_systemctl_skips(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: None)
    r = doctor.check_wifi_recover_timer()
    assert r.status == "ok"
    assert "no systemctl" in r.detail
