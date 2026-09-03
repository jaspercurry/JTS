# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — network domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from jasper.identity import resolve_hostname
from jasper.output_hardware import current_usb_data_role
from jasper.usb_network import (
    DEFAULT_DNSMASQ_PATH,
    DEFAULT_NM_PATH,
    DEFAULT_PENDING_PATH,
    IPv4ObservationState,
    UsbNetworkPlanError,
    attest_plan as attest_usb_network_plan,
    load_plan as load_usb_network_plan,
    observe_ipv4_cidr,
    render_dnsmasq,
    render_nmconnection,
)
from jasper.wifi_guardian_persistence import (
    NMCLI_ACTIVE_WIFI_FIELDS,
    active_wifi_connection,
    nm_unescape as _nm_unescape,
)

from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _run

# Closed vocabulary for this module's `CheckResult.reason` (AGENTS.md: tests
# pin status + reason, never `detail` prose). Named by the fact a consumer
# would branch on; two branches meaning the same thing share one code.
REASON_REGDOM_PROBE_FAILED = "regdom_probe_failed"
REASON_REGDOM_UNPARSEABLE = "regdom_unparseable"
REASON_REGDOM_UNSET = "regdom_unset"

REASON_GUARDIAN_SKIPPED_NO_NMCLI = "guardian_skipped_no_nmcli"
REASON_GUARDIAN_NOT_APPLICABLE = "guardian_not_applicable"
REASON_GUARDIAN_STASH_MISSING = "guardian_stash_missing"
REASON_GUARDIAN_NO_ACTIVE_WIFI = "guardian_no_active_wifi"
REASON_GUARDIAN_SSID_DRIFT = "guardian_ssid_drift"

REASON_IPV6_SKIPPED_NO_NMCLI = "ipv6_skipped_no_nmcli"
REASON_IPV6_NOT_APPLICABLE = "ipv6_not_applicable"
REASON_IPV6_METHOD_UNREADABLE = "ipv6_method_unreadable"
REASON_IPV6_METHOD_DISABLED = "ipv6_method_disabled"
REASON_IPV6_LINK_LOCAL_MISSING = "ipv6_link_local_missing"

REASON_RECOVER_TIMER_SKIPPED_NO_SYSTEMCTL = "recover_timer_skipped_no_systemctl"
REASON_RECOVER_TIMER_NOT_INSTALLED = "recover_timer_not_installed"
REASON_RECOVER_TIMER_DISABLED = "recover_timer_disabled"

REASON_AVAHI_DAEMON_NOT_INSTALLED = "avahi_daemon_not_installed"
REASON_AVAHI_DAEMON_INACTIVE = "avahi_daemon_inactive"

REASON_HOSTNAME_UNREADABLE = "hostname_unreadable"
REASON_AVAHI_RESOLVE_MISSING = "avahi_resolve_missing"
REASON_AVAHI_RESOLVE_FAILED = "avahi_resolve_failed"
REASON_AVAHI_RESOLVE_UNEXPECTED_OUTPUT = "avahi_resolve_unexpected_output"
REASON_HOSTNAME_COLLISION = "hostname_collision"

REASON_AVAHI_BROWSE_MISSING = "avahi_browse_missing"
REASON_AVAHI_BROWSE_PARTIAL_TIMEOUT = "avahi_browse_partial_timeout"
REASON_AVAHI_BROWSE_TIMEOUT = "avahi_browse_timeout"
REASON_AVAHI_BROWSE_FAILED = "avahi_browse_failed"
REASON_AVAHI_SERVICE_NOT_ADVERTISED = "avahi_service_not_advertised"

REASON_IDENTITY_NOT_INSTALLED = "identity_not_installed"
REASON_IDENTITY_FILE_MISSING = "identity_file_missing"
REASON_IDENTITY_COLLISION = "identity_collision"
REASON_IDENTITY_DRIFT = "identity_drift"
REASON_IDENTITY_SNAPSHOT_STALE = "identity_snapshot_stale"

REASON_USBNET_PLAN_INVALID = "usbnet_plan_invalid"
REASON_USBNET_PLAN_PENDING = "usbnet_plan_pending"
REASON_USBNET_PLAN_PROJECTION_UNREADABLE = "usbnet_plan_projection_unreadable"
REASON_USBNET_PLAN_PROJECTION_DRIFT = "usbnet_plan_projection_drift"

REASON_USBNET_NOT_APPLICABLE = "usbnet_not_applicable"
REASON_USBNET_KILLSWITCHED = "usbnet_killswitched"
REASON_USBNET_IFACE_KILLSWITCH_DRIFT = "usbnet_iface_killswitch_drift"
REASON_USBNET_PLAN_UNAVAILABLE = "usbnet_plan_unavailable"
REASON_USBNET_HARDWARE_MISMATCH = "usbnet_hardware_mismatch"
REASON_USBNET_ROLE_CHANGE_PENDING = "usbnet_role_change_pending"
REASON_USBNET_NO_UDC = "usbnet_no_udc"
REASON_USBNET_BIND_FAILED = "usbnet_bind_failed"
REASON_USBNET_ADDR_PROBE_FAILED = "usbnet_addr_probe_failed"
REASON_USBNET_ADDR_MISSING = "usbnet_addr_missing"
REASON_USBNET_ADDR_PENDING = "usbnet_addr_pending"

REASON_USBNET_SKIPPED_NO_NMCLI = "usbnet_skipped_no_nmcli"
REASON_USBNET_NM_QUERY_FAILED = "usbnet_nm_query_failed"
REASON_USBNET_NM_PROFILE_MISSING = "usbnet_nm_profile_missing"
REASON_USBNET_NM_PROFILE_MISMATCH = "usbnet_nm_profile_mismatch"

REASON_USBNET_SKIPPED_NO_SYSTEMCTL = "usbnet_skipped_no_systemctl"
REASON_USBNET_DHCP_NOT_INSTALLED = "usbnet_dhcp_not_installed"
REASON_USBNET_DHCP_IDLE = "usbnet_dhcp_idle"
REASON_USBNET_DHCP_NOT_SERVING = "usbnet_dhcp_not_serving"
REASON_USBNET_DHCP_TEARDOWN_DRIFT = "usbnet_dhcp_teardown_drift"

REASON_USBNET_NGINX_NOT_INSTALLED = "usbnet_nginx_not_installed"
REASON_USBNET_PROBE_IPV4_UNREADABLE = "usbnet_probe_ipv4_unreadable"
REASON_USBNET_PROBE_IFACE_VANISHED = "usbnet_probe_iface_vanished"
REASON_USBNET_PROBE_NO_ADDRESS = "usbnet_probe_no_address"
REASON_USBNET_PROBE_NO_ANSWER = "usbnet_probe_no_answer"
REASON_USBNET_PROBE_HTTP_ERROR = "usbnet_probe_http_error"

def _parse_iw_regdom(stdout: str) -> tuple[str | None, dict[str, str]]:
    """Return the global country plus per-phy countries from `iw reg get`.

    `iw reg get` prints a global section followed by zero or more phy
    sections. On Pi 5 brcmfmac, the phy section may report `country 99`
    even when the global regulatory domain is correctly set; Linux uses
    that alpha2 for driver-built regulatory domains whose specific ISO
    country cannot be determined. The global country is the actionable
    WLAN-country configuration for this doctor check.
    """
    global_country: str | None = None
    phy_countries: dict[str, str] = {}
    current_phy: str | None = None

    for raw in stdout.splitlines():
        line = raw.strip()
        if line == "global":
            current_phy = None
            continue
        if line.startswith("phy#"):
            current_phy = line.removeprefix("phy#")
            continue
        if not line.startswith("country "):
            continue

        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        country = parts[1].rstrip(":")
        if current_phy is None:
            if global_country is None:
                global_country = country
        elif current_phy not in phy_countries:
            phy_countries[current_phy] = country

    return global_country, phy_countries

def _format_phy_regdom_detail(phy_countries: dict[str, str]) -> str:
    if not phy_countries:
        return "no per-phy regdom reported"
    parts: list[str] = []
    for phy, country in sorted(phy_countries.items()):
        detail = f"phy{phy} country={country}"
        if country == "99":
            detail += " (driver custom/unlabeled; not actionable by itself)"
        elif country == "00":
            detail += " (world/unset; not actionable by itself)"
        parts.append(detail)
    return "; ".join(parts)

def _cached_nmcli_active(nmcli: str) -> subprocess.CompletedProcess:
    """``nmcli connection show --active``, memoized: check_wifi_guardian,
    check_wifi_link_local_ipv6 and check_usbnet_nm_profile each want the
    active-connection table, so one process serves all three per run."""
    return evidence.get(
        "nmcli_active",
        lambda: _run(
            [nmcli, "-t", "-f", NMCLI_ACTIVE_WIFI_FIELDS,
             "connection", "show", "--active"],
            timeout=5,
        ),
    )


def _active_wifi_connection(nmcli: str) -> tuple[str | None, str | None]:
    """Return the active Wi-Fi NetworkManager profile name and device."""
    proc = _cached_nmcli_active(nmcli)
    if proc.returncode != 0:
        return None, None
    return active_wifi_connection(proc.stdout)

@doctor_check(order=63, group="network")
def check_wifi_regdom() -> CheckResult:
    """Verify the configured global WLAN regulatory country is known.

    Raspberry Pi OS records the intended WiFi country in cfg80211's
    global regulatory domain (normally via Pi Imager or
    `raspi-config nonint do_wifi_country`). That value controls legal
    channels, transmit power, and 5 GHz availability.

    Do not treat Pi 5 brcmfmac's per-phy `country 99: DFS-UNSET` as a
    product failure by itself. It is common for the Broadcom driver to
    expose a custom/unlabeled per-radio domain while the global country
    is valid. Actual scan suppression is detected by `/wifi/scan` from
    scan failures and kernel `Scanning suppressed: status (4)` logs,
    then repaired via `wifi_scan_repair`."""
    proc = _run(["iw", "reg", "get"], timeout=5)
    if proc.returncode != 0:
        return CheckResult(
            "WiFi reg domain", "warn",
            "iw reg get failed; can't verify WLAN country configuration",
            reason=REASON_REGDOM_PROBE_FAILED,
        )

    global_country, phy_countries = _parse_iw_regdom(proc.stdout)
    if global_country is None:
        return CheckResult(
            "WiFi reg domain", "warn",
            "could not parse global regdom from `iw reg get` "
            "(no WiFi adapter? Ethernet-only Pi is fine)",
            reason=REASON_REGDOM_UNPARSEABLE,
        )
    phy_detail = _format_phy_regdom_detail(phy_countries)
    if global_country in ("99", "00"):
        return CheckResult(
            "WiFi reg domain", "warn",
            f"global regdom is '{global_country}' (unset); set WLAN "
            "country with Pi Imager or `sudo raspi-config nonint "
            f"do_wifi_country <CC>`. {phy_detail}",
            reason=REASON_REGDOM_UNSET,
        )
    return CheckResult(
        "WiFi reg domain", "ok",
        f"global country={global_country}; {phy_detail}",
    )

@doctor_check(order=64, group="network")
def check_wifi_guardian() -> CheckResult:
    """Verify the WiFi profile guardian stash matches the active
    NetworkManager profile.

    The guardian (deploy/bin/jasper-wifi-guardian, run at boot via
    jasper-wifi-guardian.service) recreates a lost
    /etc/NetworkManager/system-connections/*.nmconnection from the
    wizard-owned stash at /var/lib/jasper/wifi_guardian.env. If the
    stash is missing or stale, the recovery contract is broken — even
    though WiFi is currently working. This check surfaces that drift.

    States:
      ok    — stash exists, SSID matches what NM is currently on
      warn  — stash absent while WiFi is up (open the /wifi/ wizard and
              save once to seed); OR stash present but SSID drifted from
              the active profile (operator likely connected via SSH); OR
              stash present and no WiFi is up (last guardian run failed
              to recreate, or NM also failed)
      (the check is informational — guardian status is never fail-
       blocking. The Pi is currently online or not regardless of the
       stash state; the stash exists to help the *next* boot.)

    Skipped silently when nmcli is missing — the guardian is no-op on
    those machines anyway (no NM, nothing to recover)."""
    label = "WiFi profile guardian"
    nmcli = shutil.which("nmcli")
    if nmcli is None:
        # No NetworkManager → guardian isn't applicable. Don't warn;
        # this is the headless-Ethernet-only Pi case.
        return CheckResult(
            label, "skipped", "no nmcli on PATH",
            reason=REASON_GUARDIAN_SKIPPED_NO_NMCLI,
        )

    # Read the stash via the same module the wizard + tests use. We
    # never log the PSK from doctor; the SSID + key_mgmt are fine.
    from ...wifi_guardian_persistence import (
        DEFAULT_PATH as _STASH_DEFAULT,
        read_stash,
    )
    stash_path = os.environ.get("JASPER_WIFI_STASH_FILE", _STASH_DEFAULT)
    stash = read_stash(stash_path)

    # Resolve the active Wi-Fi profile name colon-safely via the shared
    # helper (TYPE-first field order + `\:` unescape). A previous inline
    # NAME-first probe here silently missed profiles whose name contained a
    # literal colon (real SSIDs like "Home:5G"), making the guardian check
    # falsely report "no active WiFi" / stash drift for a valid profile.
    active_name, _device = _active_wifi_connection(nmcli)

    active_ssid: str | None = None
    if active_name:
        ssid_proc = _run(
            [nmcli, "-t", "-f", "802-11-wireless.ssid",
             "connection", "show", active_name],
            timeout=5,
        )
        if ssid_proc.returncode == 0:
            for raw in ssid_proc.stdout.splitlines():
                if raw.startswith("802-11-wireless.ssid:"):
                    val = raw.split(":", 1)[1]
                    if val:
                        active_ssid = val
                    break
        if active_ssid is None:
            active_ssid = active_name  # fallback

    if stash is None and active_ssid is None:
        # Both absent: fresh install on Ethernet, or WiFi off / never
        # configured. Nothing to recover from; nothing to warn about.
        return CheckResult(
            label, "skipped", "no stash and no active WiFi (Ethernet-only?)",
            reason=REASON_GUARDIAN_NOT_APPLICABLE,
        )

    if stash is None and active_ssid is not None:
        return CheckResult(
            label, "warn",
            f"WiFi is up on {active_ssid!r} but no recovery stash exists. "
            f"Open http://jts.local/wifi/ and Connect once to seed "
            f"{stash_path} — until then, a dirty-shutdown filesystem loss "
            f"of /etc/NetworkManager/system-connections/ would brick "
            f"network access.",
            reason=REASON_GUARDIAN_STASH_MISSING,
        )

    if stash is not None and active_ssid is None:
        return CheckResult(
            label, "warn",
            f"stash points at {stash.ssid!r} but no WiFi is currently up. "
            f"Run `sudo /usr/local/sbin/jasper-wifi-guardian --reason manual` "
            f"to retry, or check `journalctl -u jasper-wifi-guardian` for "
            f"the most recent recreate attempt.",
            reason=REASON_GUARDIAN_NO_ACTIVE_WIFI,
        )

    # Both present: compare. Stash SSID drift from active SSID means the
    # operator likely switched networks via SSH (`nmcli dev wifi connect`)
    # and didn't re-save in the wizard. WiFi works today; recovery is
    # pointed at a network that may not be in range when needed.
    assert stash is not None and active_ssid is not None
    if stash.ssid == active_ssid:
        return CheckResult(
            label, "ok",
            f"stash matches active SSID ({active_ssid})",
        )
    return CheckResult(
        label, "warn",
        f"stash points at {stash.ssid!r} but WiFi is on {active_ssid!r}. "
        f"Re-save at http://jts.local/wifi/ to update the recovery stash; "
        f"otherwise a future dirty shutdown would recreate the wrong "
        f"network.",
        reason=REASON_GUARDIAN_SSID_DRIFT,
    )

@doctor_check(order=64.2, group="network")
def check_wifi_link_local_ipv6() -> CheckResult:
    """Active Wi-Fi profiles must keep link-local IPv6 enabled for mDNS.

    Apple clients commonly resolve `.local` with IPv6 mDNS before falling
    back to IPv4. A NetworkManager profile with `ipv6.method=ignore` can
    make `jts.local` navigations wait several seconds even though IPv4 and
    Avahi are otherwise healthy. JTS only needs link-local IPv6 here, not
    routed IPv6.
    """
    label = "WiFi link-local IPv6"
    nmcli = shutil.which("nmcli")
    if nmcli is None:
        return CheckResult(
            label, "skipped", "no nmcli on PATH",
            reason=REASON_IPV6_SKIPPED_NO_NMCLI,
        )

    profile, device = _active_wifi_connection(nmcli)
    if profile is None:
        return CheckResult(
            label, "skipped", "no active WiFi profile",
            reason=REASON_IPV6_NOT_APPLICABLE,
        )
    if device is None:
        device = "wlan0"

    method_proc = _run(
        [nmcli, "-g", "ipv6.method", "connection", "show", profile],
        timeout=5,
    )
    method = method_proc.stdout.strip().splitlines()[0] if method_proc.stdout.strip() else ""
    if method_proc.returncode != 0 or not method:
        return CheckResult(
            label, "warn",
            f"could not read ipv6.method for active WiFi profile {profile!r}",
            reason=REASON_IPV6_METHOD_UNREADABLE,
        )
    if method in {"ignore", "disabled"}:
        quoted_profile = shlex.quote(profile)
        quoted_device = shlex.quote(device)
        return CheckResult(
            label, "warn",
            f"active WiFi profile {profile!r} has ipv6.method={method}; "
            "Apple clients may stall resolving `<hostname>.local`. Fix: "
            f"`sudo nmcli connection modify {quoted_profile} ipv6.method link-local` "
            f"then `sudo nmcli dev reapply {quoted_device}`.",
            reason=REASON_IPV6_METHOD_DISABLED,
        )

    addr_proc = _run(["ip", "-6", "addr", "show", "dev", device, "scope", "link"], timeout=5)
    if "inet6 fe80:" not in addr_proc.stdout:
        quoted_device = shlex.quote(device)
        return CheckResult(
            label, "warn",
            f"active WiFi profile {profile!r} uses ipv6.method={method}, but "
            f"{device} has no link-local IPv6 address; `.local` may resolve "
            f"slowly. Try `sudo nmcli dev reapply {quoted_device}`.",
            reason=REASON_IPV6_LINK_LOCAL_MISSING,
        )
    return CheckResult(
        label, "ok",
        f"{profile} keeps link-local IPv6 on {device} (ipv6.method={method})",
    )

@doctor_check(order=64.5, group="network")
def check_wifi_recover_timer() -> CheckResult:
    """The Wi-Fi flap recovery timer must stay enabled so the Wi-Fi-down
    nudge (brcmfmac scan-suppression repair + guardian activation) is live.

    jasper-wifi-recover.timer fires periodically with no resident RAM; a
    healthy tick is one NetworkManager read that exits silently. If the timer
    is disabled or masked, a scan-suppression wedge after a network flap has
    no automatic recovery and the operator is back to a power cycle (the
    2026-06-19 incident). Informational only — never fail-blocking; the box
    is online or not regardless of this timer.

    Skipped on dev hosts: no systemctl, or the unit was never installed."""
    label = "WiFi recover timer"
    if shutil.which("systemctl") is None:
        return CheckResult(
            label, "skipped", "no systemctl",
            reason=REASON_RECOVER_TIMER_SKIPPED_NO_SYSTEMCTL,
        )
    unit_state = evidence.unit_state("jasper-wifi-recover.timer")
    if unit_state is None or unit_state.get("load_state") == "not-found":
        return CheckResult(
            label, "skipped", "timer not installed",
            reason=REASON_RECOVER_TIMER_NOT_INSTALLED,
        )
    state = unit_state.get("unit_file_state") or "unknown"
    if state == "enabled":
        return CheckResult(label, "ok", "jasper-wifi-recover.timer enabled")
    return CheckResult(
        label, "warn",
        f"jasper-wifi-recover.timer is '{state}', not enabled — Wi-Fi can't "
        f"auto-recover from a brcmfmac scan-suppression wedge until it is. "
        f"Fix: `sudo systemctl enable --now jasper-wifi-recover.timer`.",
        reason=REASON_RECOVER_TIMER_DISABLED,
    )

@doctor_check(order=65, group="network")
def check_avahi_daemon() -> CheckResult:
    """avahi-daemon is the mDNS *publisher* — without it the speaker
    is invisible to `<hostname>.local` resolution from other devices,
    control clients can't discover `_jasper-control._tcp`, and any
    user-facing mention of "visit http://jts.local/" silently fails.

    Pi OS Lite Trixie ships `libnss-mdns` (resolution-side) but does
    NOT pre-install or enable avahi-daemon. install.sh added the
    package starting 2026-05-24; on Pis bootstrapped before that this
    check flags the gap so the operator knows to re-run install.sh.

    Fires BEFORE check_avahi_jasper_control so the operator sees the
    package/daemon failure first, not the indirect "service not
    advertised" message.
    """
    label = "avahi-daemon"
    unit_state = evidence.unit_state("avahi-daemon.service")
    state = (unit_state or {}).get("active_state") or "unknown"
    if state == "active":
        return CheckResult(label, "ok", "running (mDNS publishing enabled)")
    # is-active prints "inactive" for both unit-not-found and stopped;
    # LoadState from the same batched read distinguishes them.
    if (unit_state or {}).get("load_state") == "not-found":
        return CheckResult(
            label, "fail",
            "avahi-daemon NOT installed. Re-run deploy/install.sh — "
            "it now installs the package (2026-05-24+). Without it, "
            "`<hostname>.local` doesn't resolve and control clients can't "
            "discover this Pi.",
            reason=REASON_AVAHI_DAEMON_NOT_INSTALLED,
        )
    return CheckResult(
        label, "fail",
        f"systemctl is-active = '{state}'. "
        "`sudo systemctl enable --now avahi-daemon` to fix.",
        reason=REASON_AVAHI_DAEMON_INACTIVE,
    )

@doctor_check(order=67, group="network")
def check_hostname_avahi_consistency() -> CheckResult:
    """Detect Avahi's silent hostname suffix-resolve on collision.

    When two devices on the same LAN both claim the same hostname,
    Avahi's conflict-resolution renames the loser to `<hostname>-2`,
    `<hostname>-3`, etc. — the OS-level `/etc/hostname` stays as the
    user configured it, but `avahi-resolve` and outbound mDNS replies
    use the suffixed form. The user has no UI surface that tells
    them this happened; they just notice "my second speaker isn't
    reachable as jts.local — what's going on?".

    Approach: resolve `<sys_hostname>.local` via `avahi-resolve-host-name`
    and compare the result to one of our own interface IPs. If the
    name we configured resolves to someone *else's* IP, another
    device on the LAN won the claim and we got suffix-resolved.
    Decoupled from `_jasper-control._tcp` so it works before
    jasper-control is up.
    """
    label = "hostname ↔ avahi consistency"
    sys_hostname = _run(["hostname", "-s"]).stdout.strip()
    if not sys_hostname:
        return CheckResult(
            label, "warn", "could not read system hostname",
            reason=REASON_HOSTNAME_UNREADABLE,
        )
    bin_path = shutil.which("avahi-resolve-host-name")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-resolve-host-name missing (apt install avahi-utils)",
            reason=REASON_AVAHI_RESOLVE_MISSING,
        )
    # -4: IPv4 only. Output is one line: `<hostname>.local <IP>`.
    proc = _run([bin_path, "-4", f"{sys_hostname}.local"], timeout=4.0)
    if proc.returncode != 0:
        # Don't fail — check_avahi_daemon already reports the root
        # cause if the daemon isn't running.
        return CheckResult(
            label, "warn",
            f"avahi-resolve-host-name {sys_hostname}.local exited "
            f"{proc.returncode}. Likely avahi-daemon not yet "
            f"advertising us — check_avahi_daemon reports the cause.",
            reason=REASON_AVAHI_RESOLVE_FAILED,
        )
    parts = proc.stdout.strip().split()
    if len(parts) < 2:
        return CheckResult(
            label, "warn",
            f"unexpected avahi-resolve output: {proc.stdout.strip()!r}",
            reason=REASON_AVAHI_RESOLVE_UNEXPECTED_OUTPUT,
        )
    resolved_ip = parts[1]
    # `hostname -I` prints space-separated IPs for all up interfaces.
    own_ips = set(_run(["hostname", "-I"]).stdout.split())
    if resolved_ip in own_ips:
        return CheckResult(
            label, "ok",
            f"`{sys_hostname}.local` resolves to us ({resolved_ip})",
        )
    return CheckResult(
        label, "warn",
        f"`{sys_hostname}.local` resolves to {resolved_ip}, but this "
        f"Pi's IPs are {sorted(own_ips)}. Another device on the LAN "
        f"is using your hostname; Avahi suffix-resolved us to "
        f"`{sys_hostname}-N.local`. Pick a unique hostname: "
        f"`sudo hostnamectl set-hostname <new>` then reboot.",
        reason=REASON_HOSTNAME_COLLISION,
    )

@doctor_check(order=66, group="network")
def check_avahi_jasper_control() -> CheckResult:
    """Verify avahi is advertising `_jasper-control._tcp` so supported
    clients can find us via mDNS-SD. avahi-browse with -t (terminate after a
    few seconds) keeps this check fast even if no service is found."""
    label = "avahi: _jasper-control._tcp"
    bin_path = shutil.which("avahi-browse")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-browse missing (apt install avahi-utils) — can't "
            "verify the service is being advertised. Clients may still "
            "find us if avahi-daemon is publishing it.",
            reason=REASON_AVAHI_BROWSE_MISSING,
        )
    try:
        proc = _run([bin_path, "-rt", "_jasper-control._tcp"], timeout=4.0)
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if "_jasper-control._tcp" in stdout:
            return CheckResult(
                label, "ok",
                "advertised — browse timed out resolving one or more stale "
                "peer records, but a jasper-control service was visible",
                reason=REASON_AVAHI_BROWSE_PARTIAL_TIMEOUT,
            )
        return CheckResult(
            label, "fail",
            "avahi-browse timed out before any `_jasper-control._tcp` "
            "service appeared. Check avahi-daemon and local service XML.",
            reason=REASON_AVAHI_BROWSE_TIMEOUT,
        )
    if proc.returncode != 0:
        return CheckResult(
            label, "fail",
            f"avahi-browse exited {proc.returncode}. Is avahi-daemon "
            f"running? (`systemctl status avahi-daemon`).",
            reason=REASON_AVAHI_BROWSE_FAILED,
        )
    if "_jasper-control._tcp" not in proc.stdout:
        return CheckResult(
            label, "fail",
            "service not being advertised. Check that "
            "/etc/avahi/services/jasper-control.service exists and "
            "avahi-daemon was reloaded — re-run install.sh, or "
            "`sudo systemctl reload avahi-daemon`.",
            reason=REASON_AVAHI_SERVICE_NOT_ADVERTISED,
        )
    return CheckResult(
        label, "ok",
        "advertised — rooms and LAN automation can discover the control service",
    )


@doctor_check(order=67.5, group="network")
def check_identity_coherence() -> CheckResult:
    """The speaker's three names must agree: OS hostname, Avahi's
    effective mDNS name, and the configured JASPER_HOSTNAME.

    jasper-identity-reconcile (boot + 5-min timer) snapshots them into
    /var/lib/jasper/identity.env; this check reads that snapshot. A
    `collision` means Avahi suffix-renamed us (another LAN device owns
    our hostname) — the management allowlist self-heals from the same
    file so the UI stays reachable at the renamed address, but the
    household should pick a unique name. A `drift` means
    JASPER_HOSTNAME no longer matches what the LAN resolves (stale env
    after a manual hostnamectl rename). Complements
    check_hostname_avahi_consistency, which probes live avahi-resolve:
    this one also covers the configured-identity layer and flags a
    stopped reconciler timer via snapshot staleness."""
    from datetime import datetime, timedelta, timezone

    from ... import identity_state

    label = "identity coherence"
    snap = identity_state.snapshot()
    if snap["status"] == "absent":
        # Fresh checkout / pre-first-run. On a Pi the installer starts
        # the reconciler, so absent there means the unit never ran.
        if not os.path.exists("/usr/local/sbin/jasper-identity-reconcile"):
            return CheckResult(
                label, "skipped", "reconciler not installed",
                reason=REASON_IDENTITY_NOT_INSTALLED,
            )
        return CheckResult(
            label, "warn",
            "identity.env missing — run: "
            "sudo systemctl start jasper-identity-reconcile",
            reason=REASON_IDENTITY_FILE_MISSING,
        )
    stale_note = ""
    raw_ts = snap.get("checked_at", "")
    try:
        checked = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - checked
        if age > timedelta(minutes=15):
            stale_note = (
                f" (snapshot {int(age.total_seconds() // 60)} min old — "
                "is jasper-identity-reconcile.timer running?)"
            )
    except ValueError:
        stale_note = f" (unparseable checked_at: {raw_ts!r})"
    names = (
        f"os={snap['os_hostname']} avahi={snap['avahi_hostname']} "
        f"configured={snap['configured_hostname']}"
    )
    if snap["status"] == "collision":
        return CheckResult(
            label, "warn",
            f"Avahi renamed this speaker: {names}. Another device on the "
            "LAN is using your hostname. The management UI stays "
            f"reachable at http://{snap['avahi_hostname']}/ ; pick a "
            "unique name with scripts/rename-speaker.sh." + stale_note,
            reason=REASON_IDENTITY_COLLISION,
        )
    if snap["status"] == "drift":
        return CheckResult(
            label, "warn",
            f"JASPER_HOSTNAME disagrees with the advertised name: {names}. "
            "Spoken URLs, OAuth bounce, and the TLS cert derive from "
            "JASPER_HOSTNAME — converge with scripts/rename-speaker.sh "
            "or update /etc/jasper/jasper.env." + stale_note,
            reason=REASON_IDENTITY_DRIFT,
        )
    if stale_note:
        return CheckResult(
            label, "warn", names + stale_note, reason=REASON_IDENTITY_SNAPSHOT_STALE
        )
    return CheckResult(label, "ok", names)


# ----------------------------------------------------------------------
# USB management network — the NCM link on
# usb0 when the resolved hardware role supports a gadget. jasper/cli/doctor/usbsink.py owns whether the composite
# gadget's *functions* match intent (ncm.usb0 / uac2.usb0 presence); the
# checks below own the *network* side of that same intent — the usb0
# interface, its NetworkManager profile, the device-activated dnsmasq
# unit, and a loopback probe of the management UI over the fallback IP.
# ----------------------------------------------------------------------

USBNET_IFACE = "usb0"
USBNET_NM_PROFILE = "jts-usb"
USBNET_DHCP_UNIT = "jasper-usbnet-dhcp.service"
USBNET_SYS_CLASS_NET = Path("/sys/class/net")


def _usb_network_wanted() -> bool:
    """Mirror ``jasper-usbgadget-up``'s network kill-switch read.

    Duplicated (rather than imported) from
    ``jasper.cli.doctor.usbsink._network_wanted`` on purpose — no other
    pair of domain modules in this package imports checks/helpers from
    each other (each reads its own env directly), and the predicate is a
    single two-line string comparison, so a cross-module import would add
    coupling for no real de-duplication. Kept byte-identical in
    intent: unless the kill switch is the exact literal ``disabled``
    (case-insensitive), network is wanted — same convention as
    ``JASPER_SHAIRPORT_SUPERVISOR`` / ``JASPER_SYSTEM_SUPERVISOR``. NOT
    stripped, to match ``jasper-usbgadget-up``'s raw (untrimmed) comparison so
    a whitespace-decorated ``" disabled"`` stays enabled in both (review
    core-7) — a stray space must never silently drop the fallback network."""
    raw = os.environ.get("JASPER_USB_NETWORK", "enabled")
    return raw.lower() != "disabled"


def _usbnet_iface_present() -> bool:
    return (USBNET_SYS_CLASS_NET / USBNET_IFACE).is_dir()


def _udc_present() -> bool:
    """True iff a USB Device Controller exists under ``/sys/class/udc``.

    Mirrors ``jasper-usbgadget-up``/``-wanted``'s UDC probe (env-overridable
    ``JASPER_UDC_CLASS_DIR`` for the same reason the gadget scripts allow it).
    A missing UDC is the fresh-install-pre-reboot case: the dtoverlay is set
    but the OTG controller isn't in peripheral mode yet, so the gadget cannot
    bind and ``usb0`` legitimately does not exist. Once a UDC is present and
    the network is wanted, the gadget composes ``ncm.usb0`` and binds, and
    ``u_ether`` registers the ``usb0`` netdev at bind time — regardless of
    whether a host cable is attached (carrier reflects the cable, existence
    reflects the bind)."""
    udc_dir = Path(os.environ.get("JASPER_UDC_CLASS_DIR", "/sys/class/udc"))
    try:
        return udc_dir.is_dir() and any(udc_dir.iterdir())
    except OSError:
        return False


@doctor_check(order=67.55, group="network")
def check_usbnet_address_plan() -> CheckResult:
    """The persisted plan and its two installed projections must agree."""

    label = "USB network address plan"
    try:
        plan = attest_usb_network_plan(load_usb_network_plan())
    except UsbNetworkPlanError as exc:
        return CheckResult(
            label,
            "fail",
            f"missing or invalid plan ({exc}); USB gadget start is blocked, "
            "while NetworkManager/Wi-Fi remain available. Re-run deploy/install.sh.",
            reason=REASON_USBNET_PLAN_INVALID,
        )
    try:
        pending = DEFAULT_PENDING_PATH.exists()
    except OSError:
        pending = False
    if pending:
        return CheckResult(
            label,
            "warn",
            f"version={plan.version} fingerprint={plan.identity_fingerprint} "
            f"desired={plan.device_cidr}; legacy live generation is preserved "
            "until next boot",
            reason=REASON_USBNET_PLAN_PENDING,
        )
    expected = (
        (DEFAULT_NM_PATH, render_nmconnection(plan)),
        (DEFAULT_DNSMASQ_PATH, render_dnsmasq(plan)),
    )
    for path, content in expected:
        try:
            observed = path.read_text(encoding="utf-8")
        except OSError as exc:
            return CheckResult(
                label, "fail", f"cannot read {path}: {exc}",
                reason=REASON_USBNET_PLAN_PROJECTION_UNREADABLE,
            )
        if observed != content:
            return CheckResult(
                label,
                "fail",
                f"{path} does not match plan version={plan.version} "
                f"fingerprint={plan.identity_fingerprint}; the gadget boot "
                "gate will refuse an incoherent projection. Re-run install.",
                reason=REASON_USBNET_PLAN_PROJECTION_DRIFT,
            )
    return CheckResult(
        label,
        "ok",
        f"version={plan.version} fingerprint={plan.identity_fingerprint} "
        f"subnet={plan.subnet} device={plan.device_address}",
    )


@doctor_check(order=67.6, group="network")
def check_usbnet_interface() -> CheckResult:
    """The usb0 NCM interface must use its plan-derived management /30.

    ``jasper-usbgadget-up`` composes ``ncm.usb0`` whenever
    ``JASPER_USB_NETWORK`` is not the literal ``disabled``
    (jasper.cli.doctor.usbsink.check_usbgadget_composition already
    verifies the ConfigFS function itself). This check verifies the
    *network* consequence: ``u_ether`` registers the ``usb0`` netdev at
    gadget-BIND time (not host-attach time), so on a bound gadget ``usb0``
    exists regardless of whether a laptop is plugged in, and NetworkManager's
    ``jts-usb`` profile (see check_usbnet_nm_profile) should have put the
    plan-derived /30 on it. A missing ``usb0`` while the network is wanted AND a
    UDC exists therefore means the compose/bind FAILED — a real problem, not
    "nothing plugged in". No carrier on an existing ``usb0`` is the normal
    nothing-plugged-in state and reports ok."""
    label = "USB management network (usb0)"
    if not _usb_network_wanted():
        if _usbnet_iface_present():
            return CheckResult(
                label, "warn",
                f"{USBNET_IFACE} present but JASPER_USB_NETWORK=disabled — "
                "restart jasper-usbgadget.service to recompose without the "
                "network function.",
                reason=REASON_USBNET_IFACE_KILLSWITCH_DRIFT,
            )
        return CheckResult(
            label, "ok", "network kill-switched (disabled)",
            reason=REASON_USBNET_KILLSWITCHED,
        )
    try:
        plan = attest_usb_network_plan(load_usb_network_plan())
    except UsbNetworkPlanError as exc:
        return CheckResult(
            label,
            "fail",
            f"cannot determine desired address from USB network plan: {exc}",
            reason=REASON_USBNET_PLAN_UNAVAILABLE,
        )
    usb_role = current_usb_data_role()
    if not usb_role.management_transport_available:
        if _usbnet_iface_present():
            return CheckResult(
                label,
                "fail",
                f"{USBNET_IFACE} is present while the resolved USB hardware "
                f"management transport is unavailable ({usb_role.reason}); stop "
                "jasper-usbgadget.service.",
                reason=REASON_USBNET_HARDWARE_MISMATCH,
            )
        detail = (
            f"{USBNET_IFACE} intentionally absent; USB management unavailable "
            f"({usb_role.reason})"
        )
        if usb_role.reboot_required:
            return CheckResult(
                label, "warn", detail,
                reason=REASON_USBNET_ROLE_CHANGE_PENDING,
            )
        return CheckResult(
            label, "skipped", detail, reason=REASON_USBNET_NOT_APPLICABLE,
        )
    if not _usbnet_iface_present():
        if not _udc_present():
            # No UDC: fresh install pre-reboot (peripheral role selected but the OTG
            # controller isn't peripheral yet), or non-gadget hardware. The
            # gadget cannot bind, so usb0's absence is expected — the USB-role
            # check (jasper.cli.doctor.usbsink.check_usb_data_role) owns
            # that gap.
            return CheckResult(
                label, "skipped",
                f"{USBNET_IFACE} absent, no UDC present — fresh install "
                "pre-reboot or non-gadget hardware (see check_usb_data_role)",
                reason=REASON_USBNET_NO_UDC,
            )
        # A UDC exists and the network is wanted, so the gadget should have
        # composed ncm.usb0 and bound → usb0 should exist. Its absence is a
        # compose/bind failure, not a "nothing plugged in" state.
        return CheckResult(
            label, "fail",
            f"{USBNET_IFACE} missing but a UDC is present and the network is "
            "wanted — jasper-usbgadget did not compose/bind ncm.usb0. Check "
            "`systemctl status jasper-usbgadget` and "
            "check_usbgadget_composition; the fallback management network is "
            "down until it composes.",
            reason=REASON_USBNET_BIND_FAILED,
        )
    addr_proc = _run(["ip", "-4", "-o", "addr", "show", "dev", USBNET_IFACE])
    if addr_proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"{USBNET_IFACE} present but `ip addr show` failed: "
            f"{addr_proc.stderr.strip() or 'no output'}",
            reason=REASON_USBNET_ADDR_PROBE_FAILED,
        )
    if f"inet {plan.device_cidr}" not in addr_proc.stdout:
        try:
            pending = DEFAULT_PENDING_PATH.exists()
        except OSError:
            pending = False
        if pending:
            return CheckResult(
                label,
                "warn",
                f"{USBNET_IFACE} still has the preserved legacy generation; "
                f"desired={plan.device_cidr}, observed: "
                f"{addr_proc.stdout.strip() or '(no address)'}. Next boot "
                "promotes both network projections before the gadget starts.",
                reason=REASON_USBNET_ADDR_PENDING,
            )
        return CheckResult(
            label, "fail",
            f"{USBNET_IFACE} present but missing {plan.device_cidr} — "
            f"observed: {addr_proc.stdout.strip() or '(no address)'}. Check "
            f"`nmcli connection show {USBNET_NM_PROFILE}` and "
            "check_usbnet_nm_profile.",
            reason=REASON_USBNET_ADDR_MISSING,
        )
    carrier_path = USBNET_SYS_CLASS_NET / USBNET_IFACE / "carrier"
    try:
        carrier = carrier_path.read_text().strip() == "1"
    except OSError:
        carrier = None
    detail = f"{USBNET_IFACE} has {plan.device_cidr}" + (
        f" (carrier={'up' if carrier else 'down'})" if carrier is not None else ""
    )
    if usb_role.reboot_required:
        return CheckResult(
            label, "warn",
            detail + "; retained only until the pending host-role reboot",
            reason=REASON_USBNET_ROLE_CHANGE_PENDING,
        )
    return CheckResult(label, "ok", detail)


@doctor_check(order=67.7, group="network")
def check_usbnet_nm_profile() -> CheckResult:
    """The ``jts-usb`` NetworkManager profile must be the one bound to
    usb0 — not some other/ad-hoc profile — whenever usb0 exists.

    NetworkManager is the box's single network owner for usb0 (no
    systemd-networkd, no dispatcher scripts); the profile is shipped
    from the persisted plan. A different active connection on usb0 means either a
    manual `nmcli` override or a profile-install regression — either way
    the plan-derived address is not guaranteed. Skips (ok) when
    usb0 doesn't exist yet or nmcli isn't on PATH (dev host)."""
    label = "USB network NM profile"
    if not _usbnet_iface_present():
        return CheckResult(
            label, "skipped", f"{USBNET_IFACE} not present",
            reason=REASON_USBNET_NOT_APPLICABLE,
        )
    nmcli = shutil.which("nmcli")
    if nmcli is None:
        return CheckResult(
            label, "skipped", "no nmcli on PATH",
            reason=REASON_USBNET_SKIPPED_NO_NMCLI,
        )
    proc = _cached_nmcli_active(nmcli)
    if proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"`nmcli connection show --active` failed: "
            f"{proc.stderr.strip() or 'no output'}",
            reason=REASON_USBNET_NM_QUERY_FAILED,
        )
    active_on_usb0: str | None = None
    for raw in proc.stdout.splitlines():
        # TYPE,DEVICE,NAME (NMCLI_ACTIVE_WIFI_FIELDS) — NAME is requested
        # LAST and split greedily so an escaped colon inside it (an SSID
        # like "Home\:5G") never gets mistaken for the DEVICE separator.
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[1] == USBNET_IFACE:
            active_on_usb0 = _nm_unescape(parts[2]) or None
            break
    if active_on_usb0 is None:
        return CheckResult(
            label, "fail",
            f"{USBNET_IFACE} exists but NetworkManager has no active "
            f"connection on it — expected {USBNET_NM_PROFILE!r}. Check "
            f"`nmcli connection up {USBNET_NM_PROFILE}`.",
            reason=REASON_USBNET_NM_PROFILE_MISSING,
        )
    if active_on_usb0 != USBNET_NM_PROFILE:
        return CheckResult(
            label, "fail",
            f"{USBNET_IFACE} is bound to {active_on_usb0!r}, not the "
            f"shipped {USBNET_NM_PROFILE!r} profile — a manual override or "
            "install regression. The plan-derived address is not "
            "guaranteed under a different profile.",
            reason=REASON_USBNET_NM_PROFILE_MISMATCH,
        )
    return CheckResult(label, "ok", f"{USBNET_NM_PROFILE} active on {USBNET_IFACE}")


@doctor_check(order=67.8, group="network")
def check_usbnet_dhcp_unit() -> CheckResult:
    """jasper-usbnet-dhcp.service's active state must be coherent with
    whether usb0 exists.

    The unit is device-activated (``BindsTo=sys-subsystem-net-devices-
    usb0.device``) so it should be active exactly when usb0 is present
    and inactive otherwise — that's the whole point of the device
    activation (zero cost when no host is plugged in). A mismatch in
    either direction means the device-activation binding itself is
    broken, not just a transient timing gap (bounded by
    ``BindsTo=``, so this check tolerates a brief window right at
    plug/unplug by treating a still-starting unit as ok)."""
    label = "USB network DHCP (jasper-usbnet-dhcp)"
    if shutil.which("systemctl") is None:
        return CheckResult(
            label, "skipped", "no systemctl",
            reason=REASON_USBNET_SKIPPED_NO_SYSTEMCTL,
        )
    unit_state = evidence.unit_state(USBNET_DHCP_UNIT)
    if unit_state is None or unit_state.get("load_state") == "not-found":
        return CheckResult(
            label, "skipped", "unit not installed",
            reason=REASON_USBNET_DHCP_NOT_INSTALLED,
        )
    state = unit_state.get("active_state") or "unknown"
    iface_present = _usbnet_iface_present()
    if iface_present and state in ("active", "activating"):
        return CheckResult(label, "ok", f"{USBNET_DHCP_UNIT} {state}, {USBNET_IFACE} present")
    if not iface_present and state in ("inactive", "deactivating"):
        return CheckResult(
            label, "ok",
            f"{USBNET_DHCP_UNIT} {state}, {USBNET_IFACE} absent — no cost "
            "while the NCM gadget is not composed (kill-switched or no UDC)",
            reason=REASON_USBNET_DHCP_IDLE,
        )
    if iface_present:
        return CheckResult(
            label, "fail",
            f"{USBNET_IFACE} is present but {USBNET_DHCP_UNIT} is {state} — "
            "a plugged-in host won't get a DHCP lease. Check "
            f"`systemctl status {USBNET_DHCP_UNIT}`.",
            reason=REASON_USBNET_DHCP_NOT_SERVING,
        )
    return CheckResult(
        label, "warn",
        f"{USBNET_IFACE} is absent but {USBNET_DHCP_UNIT} is {state} — "
        "device-activation binding may not have torn down cleanly "
        "(RAM drift, not a functional failure since nothing is plugged in).",
        reason=REASON_USBNET_DHCP_TEARDOWN_DRIFT,
    )


@doctor_check(order=67.9, group="network")
def check_usbnet_management_probe() -> CheckResult:
    """The management UI must answer over the USB fallback address.

    Mirrors ``check_management_surface`` (jasper/cli/doctor/web.py) but
    probes the interface's observed IPv4 address (the same endpoint the
    deploy-time management-surface verification hits) with
    the resolved speaker hostname as ``Host``, not nginx's loopback IPv4 —
    the exact path a plugged-in laptop with no WiFi exercises when it falls
    back from ``http://<hostname>.local/`` to the raw fallback IP. Pins both the
    guard's acceptance of a private-IP Host/source (see
    tests/test_http_security.py) and that nginx is actually listening on
    usb0's address, without needing hardware. Skips when usb0 doesn't
    exist (nothing to probe) or nginx isn't installed (dev host)."""
    import urllib.error
    import urllib.request

    from .web import MANAGEMENT_502_HINT, NGINX_SITE

    label = "USB management network probe"
    if not _usbnet_iface_present():
        return CheckResult(
            label, "skipped", f"{USBNET_IFACE} not present",
            reason=REASON_USBNET_NOT_APPLICABLE,
        )
    if not NGINX_SITE.exists():
        return CheckResult(
            label, "skipped", "nginx site not installed",
            reason=REASON_USBNET_NGINX_NOT_INSTALLED,
        )
    observation = observe_ipv4_cidr(USBNET_IFACE)
    if observation.state is IPv4ObservationState.ERROR:
        return CheckResult(
            label,
            "fail",
            f"could not inspect {USBNET_IFACE}'s IPv4 address: "
            f"{observation.error}",
            reason=REASON_USBNET_PROBE_IPV4_UNREADABLE,
        )
    if observation.state is IPv4ObservationState.ABSENT:
        return CheckResult(
            label,
            "warn",
            f"{USBNET_IFACE} disappeared while its IPv4 address was inspected",
            reason=REASON_USBNET_PROBE_IFACE_VANISHED,
        )
    if observation.state is IPv4ObservationState.NO_ADDRESS:
        return CheckResult(
            label, "fail", f"{USBNET_IFACE} is present but has no IPv4 address",
            reason=REASON_USBNET_PROBE_NO_ADDRESS,
        )
    observed_cidr = observation.cidr
    assert observed_cidr is not None
    address = observed_cidr.split("/", 1)[0]
    probe_url = f"http://{address}/system/data.json"
    host = resolve_hostname()
    req = urllib.request.Request(probe_url, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            status = resp.status
            body = resp.read(512)
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read(512) if e.fp else b""
    except (urllib.error.URLError, OSError) as e:
        return CheckResult(
            label, "fail",
            f"no answer from nginx on {address} for Host: {host} "
            f"({e}) — is nginx bound to {USBNET_IFACE}?",
            reason=REASON_USBNET_PROBE_NO_ANSWER,
        )
    if status == 200:
        return CheckResult(label, "ok", f"200 via {address} as Host: {host}")
    detail = body.decode("utf-8", "replace").strip()[:120]
    if status == 403:
        hint = (
            " — the management-host guard rejected the fallback address; "
            "check tests/test_http_security.py's private-address acceptance "
            "and `journalctl -u jasper-control | grep event=http.reject`"
        )
    elif status == 502:
        hint = MANAGEMENT_502_HINT
    else:
        hint = ""
    return CheckResult(
        label, "fail", f"HTTP {status} ({detail}){hint}",
        reason=REASON_USBNET_PROBE_HTTP_ERROR,
    )
