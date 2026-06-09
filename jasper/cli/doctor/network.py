"""jasper-doctor checks — network domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import os
import shutil
from ._registry import doctor_check
from ._shared import CheckResult, _run

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
        )

    global_country, phy_countries = _parse_iw_regdom(proc.stdout)
    if global_country is None:
        return CheckResult(
            "WiFi reg domain", "warn",
            "could not parse global regdom from `iw reg get` "
            "(no WiFi adapter? Ethernet-only Pi is fine)",
        )
    phy_detail = _format_phy_regdom_detail(phy_countries)
    if global_country in ("99", "00"):
        return CheckResult(
            "WiFi reg domain", "warn",
            f"global regdom is '{global_country}' (unset); set WLAN "
            "country with Pi Imager or `sudo raspi-config nonint "
            f"do_wifi_country <CC>`. {phy_detail}",
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
        return CheckResult(label, "ok", "skipped — no nmcli on PATH")

    # Read the stash via the same module the wizard + tests use. We
    # never log the PSK from doctor; the SSID + key_mgmt are fine.
    from ...wifi_guardian_persistence import (
        DEFAULT_PATH as _STASH_DEFAULT,
        read_stash,
    )
    stash_path = os.environ.get("JASPER_WIFI_STASH_FILE", _STASH_DEFAULT)
    stash = read_stash(stash_path)

    # Probe active SSID via nmcli (same idiom as the guardian itself).
    proc = _run(
        [nmcli, "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
        timeout=5,
    )
    active_name: str | None = None
    if proc.returncode == 0:
        for raw in proc.stdout.splitlines():
            # Naive split: NM doesn't quote single colons in NAME often,
            # but bssid-style fields are filtered out by the field list.
            parts = raw.split(":", 1)
            if len(parts) == 2 and parts[1] in ("802-11-wireless", "wifi"):
                active_name = parts[0]
                break

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
        return CheckResult(label, "ok", "no stash and no active WiFi (Ethernet-only?)")

    if stash is None and active_ssid is not None:
        return CheckResult(
            label, "warn",
            f"WiFi is up on {active_ssid!r} but no recovery stash exists. "
            f"Open http://jts.local/wifi/ and Connect once to seed "
            f"{stash_path} — until then, a dirty-shutdown filesystem loss "
            f"of /etc/NetworkManager/system-connections/ would brick "
            f"network access.",
        )

    if stash is not None and active_ssid is None:
        return CheckResult(
            label, "warn",
            f"stash points at {stash.ssid!r} but no WiFi is currently up. "
            f"Run `sudo /usr/local/sbin/jasper-wifi-guardian --reason manual` "
            f"to retry, or check `journalctl -u jasper-wifi-guardian` for "
            f"the most recent recreate attempt.",
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
    )

@doctor_check(order=65, group="network")
def check_avahi_daemon() -> CheckResult:
    """avahi-daemon is the mDNS *publisher* — without it the speaker
    is invisible to `<hostname>.local` resolution from other devices,
    the dial can't auto-discover via `_jasper-control._tcp`, and any
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
    state = _run(["systemctl", "is-active", "avahi-daemon.service"]).stdout.strip()
    if state == "active":
        return CheckResult(label, "ok", "running (mDNS publishing enabled)")
    # is-active prints "inactive" for both unit-not-found and stopped.
    # Distinguish via `status` exit code: 4 means unit not loaded.
    status = _run(["systemctl", "status", "avahi-daemon.service"])
    if "could not be found" in status.stderr.lower() or status.returncode == 4:
        return CheckResult(
            label, "fail",
            "avahi-daemon NOT installed. Re-run deploy/install.sh — "
            "it now installs the package (2026-05-24+). Without it, "
            "`<hostname>.local` doesn't resolve and the dial can't "
            "auto-discover this Pi.",
        )
    return CheckResult(
        label, "fail",
        f"systemctl is-active = '{state}'. "
        "`sudo systemctl enable --now avahi-daemon` to fix.",
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
        return CheckResult(label, "warn", "could not read system hostname")
    bin_path = shutil.which("avahi-resolve-host-name")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-resolve-host-name missing (apt install avahi-utils)",
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
        )
    parts = proc.stdout.strip().split()
    if len(parts) < 2:
        return CheckResult(
            label, "warn",
            f"unexpected avahi-resolve output: {proc.stdout.strip()!r}",
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
    )

@doctor_check(order=66, group="network")
def check_avahi_jasper_control() -> CheckResult:
    """Verify avahi is advertising `_jasper-control._tcp` so the dial
    can find us via mDNS-SD. avahi-browse with -t (terminate after a
    few seconds) keeps this check fast even if no service is found."""
    label = "avahi: _jasper-control._tcp"
    bin_path = shutil.which("avahi-browse")
    if bin_path is None:
        return CheckResult(
            label, "warn",
            "avahi-browse missing (apt install avahi-utils) — can't "
            "verify the service is being advertised. Dial may still "
            "find us if avahi-daemon is publishing it.",
        )
    proc = _run([bin_path, "-rt", "_jasper-control._tcp"], timeout=4.0)
    if proc.returncode != 0:
        return CheckResult(
            label, "fail",
            f"avahi-browse exited {proc.returncode}. Is avahi-daemon "
            f"running? (`systemctl status avahi-daemon`).",
        )
    if "_jasper-control._tcp" not in proc.stdout:
        return CheckResult(
            label, "fail",
            "service not being advertised. Check that "
            "/etc/avahi/services/jasper-control.service exists and "
            "avahi-daemon was reloaded — re-run install.sh, or "
            "`sudo systemctl reload avahi-daemon`.",
        )
    return CheckResult(
        label, "ok",
        "advertised — dials can auto-discover via mDNS-SD",
    )
