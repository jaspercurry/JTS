"""/wifi/ — Wi-Fi network management.

Phone-Settings-style page:
  - Current network card at top (always visible).
  - Available networks list in the middle (Scan button + tap-to-connect).
  - Manual "join by network name" fallback for scan-suppressed radios
    and hidden SSIDs.
  - Saved networks in a collapse section at the bottom (with Forget).

Backed entirely by `nmcli` subprocess calls — NetworkManager is RPi OS
Trixie's default network stack and install.sh already shells out to
nmcli for the WiFi-power-save tweak, so the dependency is free.

Why nmcli and not D-Bus: the action surface here is small and
synchronous. User clicks Scan → results. User clicks Connect →
success or failure. No live signal subscriptions are needed (unlike
the Bluetooth panel's device-add/remove stream). nmcli saves ~400
lines of NM D-Bus client glue vs the dbus-next approach the
bluetooth/ engine uses.

Routes (nginx strips /wifi/):
  GET  /          landing HTML
  GET  /state     current connection + radio + adapter + saved + lockout-risk
  POST /scan      {} → {networks: [...], scan: {...}} (triggers rescan first)
  POST /connect   {ssid, password?, hidden?} | {name} → connect, rolls back on failure
  POST /forget    {name} → delete saved profile
  POST /radio     {on: bool} → toggle wifi radio

Lockout safety:
  - `nmcli dev wifi connect` is invoked with `--wait 30`; on non-zero
    exit we explicitly bring the previously-active wifi profile back
    up. nmcli's own auto-rollback is not reliable enough to skip this.
  - If the new connect created a brand-new (broken) profile, we delete
    it so the saved-networks list doesn't accumulate garbage.
  - The radio-off toggle fires a confirm() dialog with a stark
    lockout warning when the Pi has no Ethernet path. Saved Forget on
    the currently-connected SSID gets an extra-loud warning.

Security:
  - PSKs ride argv into nmcli (briefly visible in /proc to root) and
    are persisted by NetworkManager itself under /etc/NetworkManager/
    system-connections/ at mode 0600 — we never touch those files.
  - PSKs are NEVER logged: the subprocess wrapper scrubs `password ***`.
  - HTTP, not HTTPS — matches the rest of the JTS wizard surface. The
    PSK is the most sensitive thing we transmit; LAN-only deployment
    posture is documented in the PR.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import wifi_guardian_persistence, wifi_scan_repair
from ._common import (
    NAV_BACK_CSS,
    NAV_BACK_HTML,
    TOGGLE_CSS,
    begin_request,
    csrf_fetch_helpers_js,
    csrf_meta_html,
    reject_csrf,
    send_html_response,
    verify_csrf,
)

logger = logging.getLogger(__name__)


# Stash file path: wizard owns this on every successful save. Match the
# guardian script's default. Override for tests via env var.
_STASH_PATH = os.environ.get(
    "JASPER_WIFI_STASH_FILE", wifi_guardian_persistence.DEFAULT_PATH,
)


# Most nmcli reads are sub-second. Scans block until results are ready;
# default `nmcli dev wifi list` after a rescan returns within 6-10 s on
# a Pi 5 + Realtek 8821CU. 15 s is the comfortable ceiling.
_DEFAULT_NMCLI_TIMEOUT = 10
_SCAN_TIMEOUT = 20
# `nmcli --wait N` blocks until the connection activates OR N seconds
# pass. 30 s is generous enough for slow APs without keeping the user
# guessing too long. The HTTP request times out a few seconds later.
_CONNECT_WAIT = 30
_CONNECT_TIMEOUT = 45
# Rollback: shorter — if the previous network is in range, NM brings
# it up in 5-8 s. 20 s is the ceiling before we admit defeat.
_ROLLBACK_WAIT = 20
_ROLLBACK_TIMEOUT = 30
_SCAN_HEALTH_JOURNAL_LINES = 120
_SCAN_REPAIR_IFACE = os.environ.get("JASPER_WIFI_SCAN_REPAIR_IFACE", "wlan0")
_SCAN_REPAIR_RETRY_DELAYS = (2.0, 3.0)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# The backend can compute that a scan is known-bad, but product behavior
# stays conservative while we validate the Pi 5 brcmfmac failure mode live.
# Set this to 1 in a lab/operator build to actually hide the Scan button
# after a driver-suppressed scan is detected.
_HIDE_SCAN_WHEN_SUPPRESSED = _env_truthy(
    "JASPER_WIFI_HIDE_SCAN_WHEN_SUPPRESSED",
)


# ============================================================
# nmcli subprocess wrappers
# ============================================================


def _run_nmcli(
    cmd: list[str],
    *,
    timeout: float = _DEFAULT_NMCLI_TIMEOUT,
    log_argv: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an nmcli command. Returns the CompletedProcess; callers
    inspect returncode + stdout/stderr.

    `log_argv=False` is used by callers that pass a PSK on the
    command line — they pre-scrub and log a redacted version
    themselves via _scrub_argv(). Other callers can log the full
    argv safely (nmcli takes no other secret args we use)."""
    if log_argv:
        logger.info("nmcli: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("nmcli timed out after %ss: %s", timeout, " ".join(cmd))
        # Synthesize a CompletedProcess so callers don't have to
        # special-case TimeoutExpired in addition to non-zero returns.
        return subprocess.CompletedProcess(
            args=cmd, returncode=124,
            stdout=(e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr="Timed out waiting for nmcli",
        )


def _scrub_argv(cmd: list[str]) -> list[str]:
    """Return a copy of cmd with the value after `password` (and any
    other known secret-introducing arg) replaced with ***. Used for
    log lines around `nmcli dev wifi connect ... password <psk>`."""
    scrubbed = list(cmd)
    for marker in ("password",):
        try:
            idx = scrubbed.index(marker)
        except ValueError:
            continue
        if idx + 1 < len(scrubbed):
            scrubbed[idx + 1] = "***"
    return scrubbed


def _run_nmcli_secret(
    cmd: list[str],
    *,
    timeout: float = _DEFAULT_NMCLI_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Same as _run_nmcli but logs a redacted argv (PSK → ***).
    Use for any command that has a PSK on the command line."""
    logger.info("nmcli: %s", " ".join(_scrub_argv(cmd)))
    return _run_nmcli(cmd, timeout=timeout, log_argv=False)


def _parse_terse(line: str) -> list[str]:
    """Split an `nmcli -t` colon-separated line, respecting `\\:`
    escapes embedded in field values (BSSIDs use them — `AA\\:BB\\:CC...`,
    and SSIDs occasionally too)."""
    fields: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
        elif c == ":":
            fields.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    fields.append("".join(cur))
    return fields


# ============================================================
# State probes
# ============================================================


def _has_wifi_adapter() -> bool:
    """True if NetworkManager sees at least one WiFi device. Returns
    False on Pis with no wlan hardware (Pi 4 sans-wifi SKU, etc.) so
    the page can say so cleanly instead of showing an empty radio
    toggle that does nothing."""
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "TYPE", "device", "status"],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if fields and fields[0] == "wifi":
            return True
    return False


def _has_ethernet() -> bool:
    """True if any wired (ethernet) device is in the connected state.
    Drives the lockout-risk classification — Ethernet present means
    we can change WiFi state without locking the user out."""
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "TYPE,STATE", "device", "status"],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if len(fields) >= 2 and fields[0] == "ethernet" and fields[1] == "connected":
            return True
    return False


def _radio_on() -> bool:
    """Read the WiFi radio kill-switch state."""
    proc = _run_nmcli(["nmcli", "radio", "wifi"], timeout=5, log_argv=False)
    if proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "enabled"


def _current_wifi() -> dict[str, Any] | None:
    """Return details about the currently-active WiFi connection, or
    None if no WiFi connection is up."""
    # `nmcli -t -f NAME,UUID,TYPE,DEVICE connection show --active` to find
    # the active wifi profile + its NM-side display name.
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE",
         "connection", "show", "--active"],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return None
    profile_name = None
    device = None
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if len(fields) >= 4 and fields[2] in ("802-11-wireless", "wifi"):
            profile_name = fields[0]
            device = fields[3]
            break
    if not profile_name:
        return None

    # Pull the SSID, security mode, and IPv4 address from the profile +
    # device. SSID lives on the connection profile; security details
    # on the wireless-security sub-key; IP on the device.
    ssid = profile_name  # fallback if 802-11-wireless.ssid is empty
    security = "Open"
    proc2 = _run_nmcli(
        ["nmcli", "-t", "-f",
         "802-11-wireless.ssid,802-11-wireless-security.key-mgmt",
         "connection", "show", profile_name],
        timeout=5, log_argv=False,
    )
    if proc2.returncode == 0:
        for line in proc2.stdout.splitlines():
            fields = _parse_terse(line)
            if len(fields) < 2:
                continue
            key, val = fields[0], fields[1]
            if key == "802-11-wireless.ssid" and val:
                ssid = val
            elif key == "802-11-wireless-security.key-mgmt" and val:
                security = _pretty_security(val)

    # Signal strength from the live scan list (IN-USE flag matches).
    signal = None
    proc3 = _run_nmcli(
        ["nmcli", "-t", "-f", "IN-USE,SIGNAL,SSID", "device", "wifi", "list",
         "ifname", device or ""],
        timeout=5, log_argv=False,
    )
    if proc3.returncode == 0:
        for line in proc3.stdout.splitlines():
            fields = _parse_terse(line)
            if len(fields) >= 3 and fields[0] == "*":
                try:
                    signal = int(fields[1])
                except ValueError:
                    pass
                break

    # IPv4 address on the wifi device.
    ip = None
    if device:
        proc4 = _run_nmcli(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device],
            timeout=5, log_argv=False,
        )
        if proc4.returncode == 0:
            for line in proc4.stdout.splitlines():
                fields = _parse_terse(line)
                if len(fields) >= 2 and fields[0].startswith("IP4.ADDRESS") and fields[1]:
                    # Format is "192.168.1.42/24" — strip the netmask.
                    ip = fields[1].split("/", 1)[0]
                    break

    return {
        "ssid": ssid,
        "profileName": profile_name,
        "device": device,
        "security": security,
        "signal": signal,
        "ip": ip,
    }


def _list_saved() -> list[dict[str, Any]]:
    """Return the list of saved WiFi connection profiles (sorted by
    SSID).

    NM connection profiles have a NAME (what we use for activate /
    delete operations) and a separate 802-11-wireless.ssid field
    (what the user actually recognizes as their network). For
    profiles created by `nmcli dev wifi connect <ssid>`, the two
    are the same. But profiles seeded by netplan (which is what
    Pi Imager's WiFi setup writes) get a generated NAME like
    `netplan-wlan0-<ssid>` — without this lookup, "Saved networks"
    on the wizard shows the operator-hostile generated string
    rather than the SSID the user picked. We surface both so the
    UI shows SSID while the API still operates on NAME."""
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT",
         "connection", "show"],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return []
    saved: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if len(fields) < 4 or fields[2] not in ("802-11-wireless", "wifi"):
            continue
        name, uuid, _ctype, autoconnect = fields[0], fields[1], fields[2], fields[3]
        # Look up the actual SSID for display. Falls back to the
        # profile name if the lookup fails (hidden network, future
        # NM versions changing field names, etc.).
        ssid = name
        ssid_proc = _run_nmcli(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid",
             "connection", "show", name],
            timeout=5, log_argv=False,
        )
        if ssid_proc.returncode == 0:
            for sline in ssid_proc.stdout.splitlines():
                sfields = _parse_terse(sline)
                if (len(sfields) >= 2
                        and sfields[0] == "802-11-wireless.ssid"
                        and sfields[1]):
                    ssid = sfields[1]
                    break
        saved.append({
            "name": name,
            "ssid": ssid,
            "uuid": uuid,
            "autoconnect": autoconnect.lower() == "yes",
        })
    saved.sort(key=lambda p: p["ssid"].lower())
    return saved


# ============================================================
# Scan
# ============================================================


# Cap signal strength at 100; nmcli sometimes reports 101+ on saturated
# meters and we treat those identically.
_SIGNAL_MAX = 100


def _parse_scan_list(stdout: str) -> list[dict[str, Any]]:
    """Parse `nmcli -t ... device wifi list` output into UI rows.

    Hidden networks (those broadcasting with empty SSID) are filtered
    out — users can still join them through the manual-entry flow."""
    by_ssid: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        fields = _parse_terse(line)
        if len(fields) < 8:
            continue
        in_use, bssid, ssid, _mode, chan, _rate, signal_s, security = fields[:8]
        if not ssid:  # hidden — skip from the scan list
            continue
        try:
            signal = min(int(signal_s), _SIGNAL_MAX)
        except ValueError:
            signal = 0
        entry = {
            "ssid": ssid,
            "bssid": bssid,
            "channel": chan,
            "signal": signal,
            "security": _pretty_security(security or "--"),
            "secured": security not in ("", "--"),
            "inUse": in_use == "*",
        }
        # Dedup by SSID. Prefer the active BSSID so the current network
        # stays classified as in-use even when a stronger AP with the
        # same SSID is visible; otherwise keep the strongest signal.
        prev = by_ssid.get(ssid)
        if (
            prev is None
            or entry["inUse"]
            or (not prev.get("inUse") and entry["signal"] > prev["signal"])
        ):
            by_ssid[ssid] = entry

    networks = list(by_ssid.values())
    networks.sort(key=lambda n: (-n["signal"], n["ssid"].lower()))
    return networks


def _filter_available_networks(
    networks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hide the currently-connected SSID from the connectable list."""
    return [network for network in networks if not network.get("inUse")]


def _text_mentions_scan_suppression(*chunks: str | None) -> bool:
    return wifi_scan_repair.text_mentions_scan_suppression(*chunks)


def _recent_kernel_scan_suppressed() -> bool | None:
    """Best-effort read of recent kernel logs for the Pi 5 brcmfmac
    scan-suppression signature.

    Returns True/False when journalctl is available and readable, None
    when the probe itself is unavailable. This function must never make
    `/scan` fail: scan health is diagnostic, not a dependency."""
    try:
        proc = subprocess.run(
            [
                "journalctl", "-k", "-b", "-n",
                str(_SCAN_HEALTH_JOURNAL_LINES), "--no-pager",
            ],
            check=False,
            timeout=2,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _text_mentions_scan_suppression(proc.stdout, proc.stderr)


def _scan_report(
    *,
    raw_networks: list[dict[str, Any]],
    rescan_proc: subprocess.CompletedProcess[str],
    list_proc: subprocess.CompletedProcess[str],
    recent_suppression_log: bool | None,
    repair: dict[str, Any] | None = None,
) -> dict[str, Any]:
    available_networks = _filter_available_networks(raw_networks)
    only_current = (
        len(raw_networks) == 1 and bool(raw_networks[0].get("inUse"))
    )
    nmcli_mentions_suppression = _text_mentions_scan_suppression(
        rescan_proc.stdout, rescan_proc.stderr,
        list_proc.stdout, list_proc.stderr,
    )
    driver_suppressed = nmcli_mentions_suppression or (
        recent_suppression_log is True
        and (only_current or rescan_proc.returncode != 0 or list_proc.returncode != 0)
    )

    reason: str | None = None
    if driver_suppressed:
        reason = "driver_scan_suppressed"
    elif list_proc.returncode != 0:
        reason = "nmcli_scan_failed"
    elif rescan_proc.returncode != 0:
        reason = "nmcli_rescan_failed"

    degraded = reason is not None
    scan = {
        "ok": not degraded,
        "degraded": degraded,
        "suspect": only_current and not degraded,
        "reason": reason,
        "hideScanButton": bool(
            _HIDE_SCAN_WHEN_SUPPRESSED
            and reason == "driver_scan_suppressed"
        ),
        "debug": {
            "rescanReturncode": rescan_proc.returncode,
            "listReturncode": list_proc.returncode,
            "networkCount": len(available_networks),
            "rawNetworkCount": len(raw_networks),
            "filteredCurrentCount": len(raw_networks) - len(available_networks),
            "onlyCurrentNetwork": only_current,
            "recentSuppressionLog": recent_suppression_log,
        },
    }
    if repair is not None:
        scan["repair"] = repair
    return {"networks": available_networks, "scan": scan}


def _scan_networks_report_once(
    *,
    repair: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trigger a fresh scan once and return networks plus scan health.

    The health block deliberately distinguishes definite driver
    suppression from the softer "we only saw the current network" hint.
    Some homes really do only have one SSID in range."""
    # rescan request — fire and proceed; the subsequent list call below
    # is what blocks for results. Without the rescan, the kernel-side
    # cache can be stale by minutes on a quiet network.
    rescan_proc = _run_nmcli(
        ["nmcli", "device", "wifi", "rescan"],
        timeout=_SCAN_TIMEOUT, log_argv=False,
    )
    # Brief settle delay so the rescan has results to surface. Without
    # this, the list call sometimes returns the pre-rescan cache.
    time.sleep(1.5)

    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,MODE,CHAN,RATE,SIGNAL,SECURITY",
         "device", "wifi", "list"],
        timeout=_SCAN_TIMEOUT, log_argv=False,
    )
    if proc.returncode != 0:
        raw_networks: list[dict[str, Any]] = []
    else:
        raw_networks = _parse_scan_list(proc.stdout)
    return _scan_report(
        raw_networks=raw_networks,
        rescan_proc=rescan_proc,
        list_proc=proc,
        recent_suppression_log=_recent_kernel_scan_suppressed(),
        repair=repair,
    )


def scan_networks_report(*, allow_repair: bool = True) -> dict[str, Any]:
    """Trigger a fresh scan and optionally repair Pi 5 scan suppression."""
    report = _scan_networks_report_once()
    if not allow_repair or report["scan"].get("reason") != "driver_scan_suppressed":
        return report

    repair = wifi_scan_repair.maybe_repair_scan_suppression(_SCAN_REPAIR_IFACE)
    repair_dict = repair.to_dict()
    if not (repair.attempted and repair.ack):
        report["scan"]["repair"] = repair_dict
        return report

    last_report = report
    for delay_s in _SCAN_REPAIR_RETRY_DELAYS:
        time.sleep(delay_s)
        last_report = _scan_networks_report_once(repair=repair_dict)
        if not last_report["scan"].get("degraded"):
            return last_report
    return last_report


def scan_networks() -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need rows."""
    return scan_networks_report()["networks"]


def _pretty_security(raw: str) -> str:
    """Map nmcli security strings to short display labels.

    `nmcli dev wifi list` SECURITY column: '', '--', 'WPA1', 'WPA2',
    'WPA1 WPA2', 'WPA3', 'WPA2 802.1X', 'WEP'.
    `key-mgmt` from `nmcli connection show`: 'none', 'wpa-psk',
    'wpa-eap', 'sae' (WPA3), 'wpa-psk wpa-eap'."""
    if not raw or raw in ("--", "none"):
        return "Open"
    raw_upper = raw.upper()
    if "802.1X" in raw_upper or "EAP" in raw_upper:
        return "WPA-Enterprise"  # we don't support connecting to these in v1
    if "SAE" in raw_upper or "WPA3" in raw_upper:
        return "WPA3"
    if "WPA-PSK" in raw_upper or "WPA2" in raw_upper or "WPA1" in raw_upper:
        return "WPA2"
    if "WEP" in raw_upper:
        return "WEP"
    return raw


# ============================================================
# Connect / Forget / Radio
# ============================================================


def _profile_exists(name: str) -> bool:
    """True if a connection profile with this name already exists.
    Drives whether we clean up a broken profile after a failed connect:
    if the profile didn't exist before our attempt, the new (broken)
    one we created is safe to delete."""
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if fields and fields[0] == name:
            return True
    return False


def _connect_wifi_command(
    ssid: str,
    password: str | None,
    *,
    hidden: bool = False,
) -> list[str]:
    cmd = ["nmcli", "--wait", str(_CONNECT_WAIT),
           "device", "wifi", "connect", ssid]
    if password:
        cmd.extend(["password", password])
    if hidden:
        cmd.extend(["hidden", "yes"])
    return cmd


def _readable_nmcli_error(proc: subprocess.CompletedProcess[str]) -> str:
    err = (proc.stderr or proc.stdout or "").strip() or "Connection failed"
    # Trim nmcli's "Error: " prefix and the verbose "Connection activation
    # failed: (NN) " wrapper so the message that lands in the UI is
    # actually readable.
    err = re.sub(r"^Error:\s*", "", err)
    err = re.sub(r"^Connection activation failed:\s*\(\d+\)\s*", "", err)
    return err.splitlines()[0] if err else "Connection failed"


def _looks_like_ssid_lookup_failure(message: str) -> bool:
    msg = message.lower()
    return any(
        marker in msg
        for marker in (
            "no network with ssid",
            "ssid not found",
            "no wifi network",
            "no wi-fi network",
            "not found",
        )
    )


def _resolve_key_mgmt(profile_name: str) -> str:
    """Look up `802-11-wireless-security.key-mgmt` for an existing
    NM connection profile. Returns one of:
      - ``wpa-psk`` / ``sae`` / ``wpa-eap`` — exact NM value, lower-case
      - ``none`` — open network OR the field is missing/empty

    Used to populate the guardian stash's ``key_mgmt`` field after a
    successful connect so the boot-time recreate knows whether to pass
    ``password ARG`` to nmcli. ``wpa-eap`` triggers the wizard to
    skip the stash entirely (enterprise is out of scope per
    ``docs/HANDOFF-resilience.md``)."""
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt",
         "connection", "show", profile_name],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return "none"
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if (len(fields) >= 2
                and fields[0] == "802-11-wireless-security.key-mgmt"
                and fields[1]):
            return fields[1].lower()
    return "none"


def _read_profile_secrets(profile_name: str) -> tuple[str, str, str] | None:
    """Pull ``(ssid, psk, key_mgmt)`` for a saved NM profile by name.

    Uses ``nmcli -s`` (show secrets — requires root) to read the PSK
    out of NetworkManager's own keyfile. The caller (``_stash_after_saved``)
    only invokes this after a successful ``connection up``, so the
    profile is known to exist.

    Returns None on any nmcli failure — the stash refresh skips
    silently rather than the connect failing."""
    proc = _run_nmcli(
        ["nmcli", "-s", "-t", "-f",
         "802-11-wireless.ssid,"
         "802-11-wireless-security.psk,"
         "802-11-wireless-security.key-mgmt",
         "connection", "show", profile_name],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return None
    ssid = ""
    psk = ""
    key_mgmt = "none"
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if len(fields) < 2:
            continue
        key, val = fields[0], fields[1]
        if key == "802-11-wireless.ssid":
            ssid = val
        elif key == "802-11-wireless-security.psk":
            psk = val
        elif key == "802-11-wireless-security.key-mgmt":
            key_mgmt = (val or "none").lower()
    if not ssid:
        return None
    return ssid, psk, key_mgmt


def _stash_after_saved(profile_name: str) -> None:
    """Refresh the stash from an existing NM profile after a successful
    ``connection up <name>``. Symmetric with ``_stash_after_connect`` but
    pulls the PSK out of NM's own keyfile rather than from the wizard
    request body (the saved-network flow never sees the user's PSK on
    the wire)."""
    try:
        secrets = _read_profile_secrets(profile_name)
        if secrets is None:
            logger.info(
                "event=wifi_guardian.stash_skip profile=%s reason=secrets_unavailable",
                profile_name,
            )
            return
        ssid, psk, key_mgmt = secrets
        if key_mgmt == "wpa-eap":
            logger.info(
                "event=wifi_guardian.stash_skip ssid=%s reason=enterprise",
                ssid,
            )
            return
        wifi_guardian_persistence.write_stash(
            _STASH_PATH, ssid, psk, key_mgmt,
        )
        logger.info(
            "event=wifi_guardian.stash_written ssid=%s key_mgmt=%s",
            ssid, key_mgmt,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=wifi_guardian.stash_write_failed profile=%s err=%r",
            profile_name, e,
        )


def _stash_after_connect(ssid: str, password: str | None) -> None:
    """Update the guardian stash to reflect a just-successful connect.

    Best-effort: failure here MUST NOT fail the connect (the user's WiFi
    just came up; the stash is a recovery aid, not a blocker). We log a
    warning and rely on doctor to surface the drift on the next check.

    Skips silently for WPA-Enterprise — the wizard doesn't support it,
    the guardian can't recreate it (no cert/identity in our stash),
    and writing a stash we'd refuse to act on is just confusing."""
    try:
        key_mgmt = _resolve_key_mgmt(ssid)
        if key_mgmt == "wpa-eap":
            logger.info(
                "event=wifi_guardian.stash_skip ssid=%s reason=enterprise",
                ssid,
            )
            return
        wifi_guardian_persistence.write_stash(
            _STASH_PATH, ssid, password or "", key_mgmt,
        )
        # PSK never appears in the log line — only the SSID and key_mgmt.
        logger.info(
            "event=wifi_guardian.stash_written ssid=%s key_mgmt=%s",
            ssid, key_mgmt,
        )
    except Exception as e:  # noqa: BLE001
        # Wrap-all because this is a recovery aid path. Anything that
        # raises here (full disk, permission flip, nmcli timeout in
        # _resolve_key_mgmt) should not block the user's successful
        # connect from returning.
        logger.warning(
            "event=wifi_guardian.stash_write_failed ssid=%s err=%r",
            ssid, e,
        )


def _stash_clear_if_matches(ssid: str) -> None:
    """If the stash currently points at ``ssid``, clear it. Used on
    Forget — the operator is explicitly telling us the network is gone;
    we shouldn't try to recreate it at next boot.

    A Forget on a DIFFERENT SSID than the stashed one leaves the stash
    alone — the operator might be forgetting a guest-network profile
    while their home network (which the stash points at) stays valid."""
    try:
        existing = wifi_guardian_persistence.read_stash(_STASH_PATH)
        if existing is None:
            return
        if existing.ssid != ssid:
            return
        wifi_guardian_persistence.clear_stash(_STASH_PATH)
        logger.info("event=wifi_guardian.stash_cleared ssid=%s", ssid)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "event=wifi_guardian.stash_clear_failed ssid=%s err=%r",
            ssid, e,
        )


def connect_new(
    ssid: str,
    password: str | None,
    *,
    hidden: bool = False,
) -> tuple[bool, str]:
    """Connect to an SSID. If `password` is given the network is
    treated as secured; otherwise as open.

    On failure, we attempt to bring back up the previously-active
    WiFi profile so the user isn't left disconnected. If the connect
    created a brand-new (broken) profile, we delete it so the saved
    list doesn't accumulate garbage.

    Returns (ok, human-readable message)."""
    prev = _current_wifi()
    prev_profile = prev["profileName"] if prev else None
    existed_before = _profile_exists(ssid)

    cmd = _connect_wifi_command(ssid, password, hidden=hidden)
    proc = _run_nmcli_secret(cmd, timeout=_CONNECT_TIMEOUT)
    err = _readable_nmcli_error(proc)

    # Manual entry has two useful recovery modes:
    #   1. true hidden SSIDs (`hidden yes` is required), and
    #   2. Pi 5 brcmfmac scan-suppressed radios whose scan cache can't
    #      find a visible SSID even though directed association may work.
    # Retry with `hidden yes` only for SSID-lookup failures so wrong
    # passwords don't get a confusing second path.
    if (
        proc.returncode != 0
        and not hidden
        and _looks_like_ssid_lookup_failure(err)
    ):
        hidden_cmd = _connect_wifi_command(ssid, password, hidden=True)
        hidden_proc = _run_nmcli_secret(
            hidden_cmd, timeout=_CONNECT_TIMEOUT,
        )
        if hidden_proc.returncode == 0:
            proc = hidden_proc
        else:
            proc = hidden_proc
            err = _readable_nmcli_error(hidden_proc)

    if proc.returncode == 0:
        # Guardian stash refresh — best-effort, never blocks the
        # connect success. Sees the PSK on the wire here; this is
        # the canonical point to capture it. See `_stash_after_connect`
        # for the failure-mode contract.
        _stash_after_connect(ssid, password)
        return True, f"Connected to {ssid}"

    # Clean up the broken NEW profile so it doesn't sit in saved networks.
    # Only delete if the SSID didn't already exist as a saved profile —
    # otherwise we'd wipe a previously-working profile that just failed
    # to reactivate (could be temporary signal loss, etc.).
    if not existed_before:
        _run_nmcli(
            ["nmcli", "connection", "delete", ssid],
            timeout=10, log_argv=False,
        )

    # Rollback: restore the previously-active profile if there was one.
    if prev_profile and prev_profile != ssid:
        rb = _run_nmcli(
            ["nmcli", "--wait", str(_ROLLBACK_WAIT),
             "connection", "up", prev_profile],
            timeout=_ROLLBACK_TIMEOUT,
        )
        if rb.returncode == 0:
            return False, f"{err}. Restored previous network ({prev_profile})."
        return False, (
            f"{err}. Rollback to previous network ({prev_profile}) "
            "also failed; you may need to reconnect manually."
        )

    return False, err


def connect_saved(name: str) -> tuple[bool, str]:
    """Activate a previously-saved connection profile by name."""
    proc = _run_nmcli(
        ["nmcli", "--wait", str(_CONNECT_WAIT), "connection", "up", name],
        timeout=_CONNECT_TIMEOUT,
    )
    if proc.returncode == 0:
        # Refresh the stash so the guardian's saved intent matches what
        # the operator just activated. The PSK comes out of NM's own
        # keyfile via `nmcli -s` (we don't see it on the wire here).
        _stash_after_saved(name)
        return True, f"Connected to {name}"
    err = (proc.stderr or proc.stdout or "").strip() or "Activation failed"
    err = re.sub(r"^Error:\s*", "", err).splitlines()[0]
    return False, err


def _ssid_for_profile(profile_name: str) -> str | None:
    """Look up a profile's 802-11-wireless.ssid. Returns None if the
    profile is missing, can't be queried, or has no SSID field set.

    Distinct from `_read_profile_secrets` — no `nmcli -s`, no PSK touch.
    Used by `forget` to decide whether the guardian stash should be
    cleared, without leaking the PSK through a doomed code path."""
    proc = _run_nmcli(
        ["nmcli", "-t", "-f", "802-11-wireless.ssid",
         "connection", "show", profile_name],
        timeout=5, log_argv=False,
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if (len(fields) >= 2
                and fields[0] == "802-11-wireless.ssid"
                and fields[1]):
            return fields[1]
    return None


def forget(name: str) -> tuple[bool, str]:
    """Delete a saved connection profile. If it's currently active,
    nmcli takes the device down with it."""
    # Resolve the SSID before the delete so we still have it to compare
    # against the guardian stash after the profile is gone. We DO NOT
    # touch the stash on a failed forget — only the user-visible "yes,
    # this is gone" path clears the recovery intent.
    ssid_to_forget = _ssid_for_profile(name)

    proc = _run_nmcli(
        ["nmcli", "connection", "delete", name],
        timeout=10,
    )
    if proc.returncode == 0:
        # Clear the stash ONLY if it points at the same SSID the user
        # just forgot. Forgetting a different profile (e.g. a stale
        # guest network) must not invalidate the recovery intent for
        # the household network. The profile NAME and SSID may differ
        # (netplan-seeded profiles); we always compare on SSID.
        if ssid_to_forget:
            _stash_clear_if_matches(ssid_to_forget)
        return True, f"Forgot {name}"
    err = (proc.stderr or proc.stdout or "").strip() or "Delete failed"
    return False, err


def set_radio(on: bool) -> tuple[bool, str]:
    """Toggle the WiFi radio kill switch. Off = `nmcli radio wifi off`
    which immediately drops any active wifi connection."""
    proc = _run_nmcli(
        ["nmcli", "radio", "wifi", "on" if on else "off"],
        timeout=10,
    )
    if proc.returncode == 0:
        return True, "Radio on" if on else "Radio off"
    err = (proc.stderr or proc.stdout or "").strip() or "Radio toggle failed"
    return False, err


# ============================================================
# State aggregator
# ============================================================


def gather_state() -> dict[str, Any]:
    """One-shot snapshot for /state. Combines current + radio + adapter
    + saved + lockout-risk classification.

    Lockout risk:
      "low"  → ethernet is connected; we have a fallback path.
      "high" → no ethernet; any wifi change risks locking the user out.
    """
    adapter_present = _has_wifi_adapter()
    has_eth = _has_ethernet()
    radio = _radio_on() if adapter_present else False
    current = _current_wifi() if adapter_present and radio else None
    saved = _list_saved() if adapter_present else []
    risk = "low" if has_eth else "high"
    return {
        "adapterPresent": adapter_present,
        "radioOn": radio,
        "hasEthernet": has_eth,
        "lockoutRisk": risk,
        "current": current,
        "saved": saved,
    }


# ============================================================
# HTML
# ============================================================


_PAGE_STYLE = TOGGLE_CSS + """
  :root {
    --green: #1db954; --red: #c44; --grey: #999; --soft: #666;
    --bg: #fafafa; --card: #fff; --border: #e6e6e6; --warn: #fff3cd;
    --warn-border: #f0d090; --warn-text: #5a4500;
    --danger-bg: #ffe8e8; --danger-border: #d99;
  }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui,
         sans-serif; max-width: 720px; margin: 2em auto; padding: 0 1em;
         color: #222; background: var(--bg); }
  h1 { margin-bottom: 0.25em; } h2 { margin-top: 2em; }
  .sub { color: var(--soft); margin-top: 0; }
  button {
    background: var(--green); color: white; border: 0;
    padding: 0.5em 1em; border-radius: 4px; font-size: 0.95em;
    cursor: pointer;
  }
  button[disabled] { background: #bbb; cursor: not-allowed; }
  button.secondary { background: #4a4a4a; }
  button.danger { background: transparent; color: var(--red);
                  border: 1px solid var(--red); }
  button.danger:hover { background: var(--red); color: white; }
  button:hover:not([disabled]) { filter: brightness(1.1); }
  #scan-btn { min-width: 7.5em; }
  #scan-btn.scanning { background: #4a4a4a; }
  #scan-health { clear: both; margin: 0.4em 0 0.6em; }
  #scan-health .scan-note {
    background: #eef6ff; border: 1px solid #b8d8f5; color: #173b5f;
    border-radius: 6px; padding: 0.55em 0.75em; font-size: 0.9em;
    line-height: 1.4;
  }
  #scan-health .scan-note.warn {
    background: var(--warn); border-color: var(--warn-border);
    color: var(--warn-text);
  }
  .btn-spinner {
    display: inline-block; width: 0.85em; height: 0.85em;
    border: 2px solid rgba(255,255,255,0.35);
    border-top-color: white;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: -0.15em; margin-right: 0.45em;
  }

  /* Current-network card at the top. */
  .current-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 1em 1.2em; margin-bottom: 1em;
  }
  .current-card.disconnected {
    background: #f7f7f7; color: var(--soft);
  }
  .current-card .ssid {
    font-size: 1.2em; font-weight: 600; margin-bottom: 0.4em;
  }
  .current-card .meta {
    color: var(--soft); font-size: 0.9em; line-height: 1.5;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .current-card .meta .row { display: flex; gap: 0.5em; }
  .current-card .meta .key { color: #888; min-width: 5.5em; }
  .current-card .meta .val { color: #222; }

  /* Radio toggle row inside the current card. */
  .radio-row {
    display: flex; align-items: center; justify-content: space-between;
    padding-top: 0.9em; margin-top: 0.9em;
    border-top: 1px solid var(--border);
  }
  .radio-row .label { font-weight: 600; }
  /* Network list rows (available + saved). */
  .net-list { background: var(--card); border: 1px solid var(--border);
              border-radius: 8px; overflow: hidden; margin: 0.5em 0; }
  .net-list .empty { color: var(--soft); padding: 1em 1.2em;
                     font-style: italic; }
  .net-row {
    padding: 0.8em 1.2em;
    border-bottom: 1px solid var(--border);
  }
  .net-row:last-child { border-bottom: 0; }
  .net-row .head {
    display: flex; align-items: center; gap: 1em; cursor: pointer;
    user-select: none; -webkit-user-select: none;
  }
  .net-row .info { flex: 1; }
  .net-row .ssid { font-weight: 600; }
  .net-row .meta {
    color: var(--soft); font-size: 0.85em; margin-top: 0.1em;
  }
  .net-row .badge {
    display: inline-block; padding: 0.1em 0.5em; border-radius: 10px;
    font-size: 0.75em; font-weight: 600; margin-left: 0.3em;
    background: #def9d9; color: #163;
  }
  .net-row .signal {
    color: var(--soft); font-size: 0.9em; min-width: 3em; text-align: right;
    letter-spacing: 0.04em;
  }
  .net-row .actions { display: flex; gap: 0.4em; }

  /* Inline expansion panel beneath a tapped network row — used both
     for the "Connect" form and for "Forget" confirmations. */
  .panel {
    background: #fff8db; border: 1px solid #d9c97a;
    padding: 0.9em 1.2em; border-radius: 8px;
    margin: 0.7em 0 0.2em;
  }
  .panel.danger { background: var(--danger-bg);
                  border-color: var(--danger-border); color: #832; }
  .panel label { display: block; font-weight: 600; margin: 0.5em 0 0.3em; }
  .panel input[type=password], .panel input[type=text] {
    width: 100%; padding: 0.5em; border: 1px solid #bbb;
    border-radius: 4px; font-size: 1em; box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .panel .show-pw {
    color: var(--soft); font-size: 0.85em; margin-top: 0.3em;
    cursor: pointer; display: inline-block;
  }
  .panel .show-pw:hover { color: #222; }
  .panel .btns {
    display: flex; gap: 0.5em; margin-top: 0.9em;
  }
  .manual-panel {
    background: var(--card); border-color: var(--border);
    margin-top: 0.5em;
  }
  .manual-panel .field-row { margin-bottom: 0.65em; }
  .manual-panel .check-row {
    display: flex; gap: 0.45em; align-items: center;
    color: var(--soft); font-size: 0.9em; margin-top: 0.45em;
  }
  .manual-panel .check-row input { margin: 0; }
  #manual-result:empty { display: none; }

  /* Lockout warnings — used by Connect panels (when no ethernet) and
     by the radio-off confirm dialog. */
  .warn {
    background: var(--warn); border: 1px solid var(--warn-border);
    border-radius: 6px; padding: 0.6em 0.8em; margin: 0.6em 0;
    color: var(--warn-text); font-size: 0.92em; line-height: 1.45;
  }
  .warn.kill {
    background: var(--danger-bg); border-color: var(--danger-border);
    color: #832; font-weight: 600;
  }
  .warn .lead { font-weight: 700; margin-right: 0.3em; }

  /* Result banner inside a panel (post-connect / post-forget). */
  .result { padding: 0.5em 0.7em; border-radius: 4px;
            font-size: 0.92em; margin-top: 0.5em; }
  .result.ok { background: #e6f9ec; border: 1px solid #1db954;
               color: #14542a; }
  .result.err { background: var(--danger-bg);
                border: 1px solid var(--danger-border); color: #832; }

  .spinner {
    display: inline-block; width: 1em; height: 1em;
    border: 2px solid #ddd; border-top-color: var(--green);
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Top-level disclosures (Saved networks at the bottom). Mirrors
     the rules in jasper.web._common.PAGE_STYLE so collapse sections
     look consistent across the whole settings UI. */
  details.disclosure { margin-top: 1.4em; }
  details.disclosure > summary {
    list-style: none;
    cursor: pointer;
    user-select: none; -webkit-user-select: none;
    padding: 0.85em 2.4em 0.85em 1em;
    background: #f4f4f4;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-weight: 600;
    color: #222;
    position: relative;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  details.disclosure > summary:hover {
    background: #f0fff4; border-color: var(--green);
  }
  details.disclosure[open] > summary {
    border-bottom-left-radius: 0; border-bottom-right-radius: 0;
    border-bottom-color: transparent;
  }
  details.disclosure > summary::-webkit-details-marker { display: none; }
  details.disclosure > summary::after {
    content: "▸"; position: absolute;
    right: 1em; top: 50%; transform: translateY(-50%);
    color: #888;
    transition: transform 0.15s ease, color 0.15s ease;
  }
  details.disclosure > summary:hover::after,
  details.disclosure[open] > summary::after { color: var(--green); }
  details.disclosure[open] > summary::after {
    transform: translateY(-50%) rotate(90deg);
  }
  details.disclosure > .disclosure-body {
    padding: 0.6em 1em 1em;
    border: 1px solid var(--border); border-top: none;
    border-bottom-left-radius: 8px; border-bottom-right-radius: 8px;
    background: var(--card);
  }
""" + NAV_BACK_CSS


def _landing_html(csrf_token: str = "") -> bytes:
    body = """
<p class="sub">Switch the speaker's Wi-Fi network or manage saved
networks. Changes take effect immediately.</p>

<div id="current"></div>

<h2>Available networks
  <button id="scan-btn" onclick="rescan()"
          style="float:right;font-size:0.8em;padding:0.3em 0.8em;">Scan</button>
</h2>
<div id="scan-health"></div>
<div class="net-list" id="avail-list">
  <div class="empty">Tap Scan to look for nearby networks.</div>
</div>

<h2>Join by name</h2>
<div class="panel manual-panel">
  <div class="field-row">
    <label for="manual-ssid">Network name</label>
    <input id="manual-ssid" type="text" autocomplete="off"
           autocapitalize="off" spellcheck="false">
  </div>
  <div class="field-row">
    <label for="manual-password">Password</label>
    <input id="manual-password" type="password" autocomplete="off"
           autocapitalize="off" spellcheck="false">
    <span class="show-pw" onclick="toggleManualPw()">Show password</span>
    <label class="check-row" for="manual-hidden">
      <input id="manual-hidden" type="checkbox">
      Hidden network
    </label>
  </div>
  <div id="manual-result"></div>
  <div class="btns">
    <button id="manual-connect-btn" onclick="submitManualConnect()">Connect</button>
  </div>
</div>

<details class="disclosure">
  <summary>Saved networks <span id="saved-count" style="color:#888;font-weight:400"></span></summary>
  <div class="disclosure-body">
    <div class="net-list" id="saved-list" style="margin:0">
      <div class="empty">Loading…</div>
    </div>
  </div>
</details>

<script>
// State + DOM helpers ------------------------------------------------
let state = { adapterPresent: true, radioOn: false, hasEthernet: false,
              lockoutRisk: "high", current: null, saved: [] };
let scanResults = [];
let scanHealth = null;
let scanning = false;
let hasScanned = false;
let autoScanned = false;
let openSsid = null;     // available-list inline panel currently open
let openSavedName = null;// saved-list inline panel currently open
let stateTimer = null;
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
{csrf_fetch_helpers_js}
function cssIdSafe(s) { return String(s).replace(/[^a-zA-Z0-9]/g, '_'); }
function signalBars(sig) {
  if (sig == null) return '';
  if (sig >= 70) return '●●●●';
  if (sig >= 50) return '●●●○';
  if (sig >= 30) return '●●○○';
  if (sig >= 10) return '●○○○';
  return '○○○○';
}

// State fetch + render -----------------------------------------------
async function fetchState() {
  try {
    const r = await fetch('./state', { cache: 'no-store' });
    state = await r.json();
    renderCurrent();
    renderScanHealth();
    renderSaved();
    maybeAutoScan();
  } catch (e) {
    document.getElementById('current').innerHTML =
      '<div class="current-card disconnected">' +
      '<div class="ssid">Status unavailable</div>' +
      '<div class="meta">Could not reach the Wi-Fi backend.</div>' +
      '</div>';
  }
}

function maybeAutoScan() {
  if (autoScanned || scanning || !state.adapterPresent || !state.radioOn) return;
  autoScanned = true;
  rescan();
}

function schedulePoll(ms) {
  if (stateTimer !== null) clearInterval(stateTimer);
  stateTimer = setInterval(fetchState, ms);
}

function renderCurrent() {
  const wrap = document.getElementById('current');
  if (!state.adapterPresent) {
    wrap.innerHTML = '<div class="current-card disconnected">' +
      '<div class="ssid">No Wi-Fi adapter detected</div>' +
      '<div class="meta">This Pi has no wireless interface ' +
      'NetworkManager can drive.</div></div>';
    return;
  }

  const cur = state.current;
  const cardClass = cur ? 'current-card' : 'current-card disconnected';

  let inner = '';
  if (cur) {
    const bars = cur.signal != null ? signalBars(cur.signal) : '';
    inner += '<div class="ssid">' + escapeHtml(cur.ssid) +
             '  <span style="font-weight:400;color:#666;font-size:0.85em">' +
             bars + '</span></div>';
    inner += '<div class="meta">';
    if (cur.ip) {
      inner += '<div class="row"><span class="key">IP</span>' +
               '<span class="val">' + escapeHtml(cur.ip) + '</span></div>';
    }
    inner += '<div class="row"><span class="key">Security</span>' +
             '<span class="val">' + escapeHtml(cur.security) + '</span></div>';
    if (cur.signal != null) {
      inner += '<div class="row"><span class="key">Signal</span>' +
               '<span class="val">' + cur.signal + ' / 100</span></div>';
    }
    inner += '</div>';
  } else if (!state.radioOn) {
    inner += '<div class="ssid">Wi-Fi is off</div>';
    inner += '<div class="meta">Turn Wi-Fi on to scan and connect.</div>';
  } else {
    inner += '<div class="ssid">Not connected</div>';
    inner += '<div class="meta">No active Wi-Fi connection.</div>';
  }

  // Radio toggle row. No always-visible warning copy — the lockout
  // warning ONLY appears in the confirm() dialog that fires when the
  // user actually tries to turn the radio off (see toggleRadio()).
  // Persistent red copy here just spooks people who weren't going to
  // touch it.
  const checked = state.radioOn ? ' checked' : '';
  inner += '<div class="radio-row">' +
           '  <div class="label">Wi-Fi radio</div>' +
           '  <label class="toggle">' +
           '    <input type="checkbox" id="radio-toggle" ' +
                  'aria-label="Wi-Fi radio"' + checked + '>' +
           '    <span class="track"></span>' +
           '  </label>' +
           '</div>';

  wrap.className = '';
  wrap.innerHTML = '<div class="' + cardClass + '">' + inner + '</div>';
  const radioToggle = document.getElementById('radio-toggle');
  if (radioToggle) radioToggle.addEventListener('change', toggleRadio);
}

function renderSaved() {
  const list = document.getElementById('saved-list');
  const countEl = document.getElementById('saved-count');
  const saved = state.saved || [];
  countEl.textContent = saved.length ? '(' + saved.length + ')' : '(none)';
  if (!saved.length) {
    list.innerHTML = '<div class="empty">No saved networks yet.</div>';
    return;
  }
  const curName = state.current ? state.current.profileName : null;
  list.innerHTML = saved.map(p => {
    const isCurrent = p.name === curName;
    const idsafe = cssIdSafe(p.name);
    const badge = isCurrent
      ? '<span class="badge">In use</span>' : '';
    // Display the SSID (what the user knows the network as); the
    // profile NAME goes through the API as the operate-on key.
    return '<div class="net-row" id="sv-' + idsafe + '">' +
      '<div class="head">' +
      '  <div class="info">' +
      '    <div class="ssid">' + escapeHtml(p.ssid || p.name) + badge + '</div>' +
      '  </div>' +
      '  <div class="actions">' +
      '    <button class="danger" data-action="open-forget" ' +
             'data-name="' + escapeHtml(p.name) + '">Forget</button>' +
      '  </div>' +
      '</div>' +
      '<div id="sv-panel-' + idsafe + '"></div>' +
      '</div>';
  }).join('');

  // Re-open any panel that was open before this render so the user's
  // in-flight Forget confirmation isn't yanked by a poll.
  if (openSavedName) {
    openForget(openSavedName, /*keepOpen*/true);
  }
}

// Available networks list --------------------------------------------
function renderScanHealth() {
  const box = document.getElementById('scan-health');
  const btn = document.getElementById('scan-btn');
  if (!box) return;
  if (btn) {
    btn.style.display = scanHealth && scanHealth.hideScanButton ? 'none' : '';
  }
  if (!scanHealth) {
    box.innerHTML = '';
    return;
  }
  const debug = scanHealth.debug || {};
  if (scanHealth.degraded) {
    let msg = 'Wi-Fi scanning looks degraded. ';
    if (scanHealth.reason === 'driver_scan_suppressed') {
      msg += 'The Pi radio is reporting scan suppression, so nearby networks may not appear.';
    } else {
      msg += 'The scan command did not complete cleanly.';
    }
    msg += ' Join by name still works and keeps rollback enabled.';
    box.innerHTML = '<div class="scan-note warn">' + escapeHtml(msg) + '</div>';
    return;
  }
  if (scanHealth.suspect || debug.onlyCurrentNetwork) {
    box.innerHTML = '<div class="scan-note">Scan only found the current network. Join by name is available below.</div>';
    return;
  }
  box.innerHTML = '';
}

function renderAvail() {
  const list = document.getElementById('avail-list');
  if (!scanResults.length) {
    let msg = 'Tap Scan to look for nearby networks.';
    if (scanning) {
      msg = 'Scanning…';
    } else if (hasScanned && scanHealth && scanHealth.degraded) {
      msg = 'Scan degraded. Join by name is available below.';
    } else if (hasScanned) {
      msg = 'No other networks found.';
    }
    list.innerHTML = '<div class="empty">' + escapeHtml(msg) + '</div>';
    return;
  }
  list.innerHTML = scanResults.map(n => {
    const idsafe = cssIdSafe(n.ssid);
    const lock = n.secured ? ' 🔒' : '';
    const inUseBadge = n.inUse ? '<span class="badge">Connected</span>' : '';
    return '<div class="net-row" id="av-' + idsafe + '">' +
      '<div class="head" data-action="open-connect" ' +
           'data-ssid="' + escapeHtml(n.ssid) + '">' +
      '  <div class="info">' +
      '    <div class="ssid">' + escapeHtml(n.ssid) + lock + inUseBadge + '</div>' +
      '    <div class="meta">' + escapeHtml(n.security) +
              ' · ch ' + escapeHtml(n.channel) + '</div>' +
      '  </div>' +
      '  <div class="signal">' + signalBars(n.signal) + '</div>' +
      '</div>' +
      '<div id="av-panel-' + idsafe + '"></div>' +
      '</div>';
  }).join('');
  // Re-open any panel that was open before this render.
  if (openSsid) {
    openConnect(openSsid, /*keepOpen*/true);
  }
}

// Scan ---------------------------------------------------------------
async function rescan() {
  if (scanning) return;
  if (!state.radioOn) {
    alert('Turn Wi-Fi on first.');
    return;
  }
  scanning = true;
  const btn = document.getElementById('scan-btn');
  btn.classList.add('scanning');
  btn.innerHTML = '<span class="btn-spinner"></span>Scanning';
  btn.disabled = true;
  renderAvail();
  try {
    const r = await fetch('./scan', {
      method: 'POST',
      headers: jsonHeaders(),
      body: '{}',
    });
    const data = await r.json();
    scanResults = data.networks || [];
    scanHealth = data.scan || null;
  } catch (e) {
    scanResults = [];
    scanHealth = {
      degraded: true,
      reason: 'request_failed',
      hideScanButton: false,
      debug: {},
    };
  } finally {
    hasScanned = true;
    scanning = false;
    btn.classList.remove('scanning');
    btn.innerHTML = 'Scan';
    btn.disabled = false;
    renderScanHealth();
    renderAvail();
  }
}

// Connect panel ------------------------------------------------------
function connectRiskWarningHtml() {
  return (state.lockoutRisk === 'high' && state.current)
    ? '<div class="warn"><span class="lead">⚠ Lockout risk:</span>' +
      ' You\\'re reaching this page over Wi-Fi and the Pi has no ' +
      'Ethernet fallback. If the new network fails, the Pi will try ' +
      'to reconnect to ' + escapeHtml(state.current.ssid) +
      ' automatically (90s timeout). If that also fails you\\'ll need ' +
      'physical access to recover.</div>'
    : (state.current
        ? '<div class="warn">Switching from ' +
          escapeHtml(state.current.ssid) + '. Connection will ' +
          'drop briefly — page will reload.</div>'
        : '');
}

function confirmManualLockoutRisk(ssid) {
  if (!(state.lockoutRisk === 'high' && state.current)) return true;
  return window.confirm(
    'You are reaching this page over Wi-Fi and the Pi has no Ethernet fallback.\\n\\n' +
    'It will try to connect to "' + ssid + '". If that fails, it will roll back to "' +
    state.current.ssid + '". If rollback also fails, you may need physical access.\\n\\n' +
    'Continue?',
  );
}

function openConnect(ssid, keepOpen) {
  // Close any other open connect panel.
  if (openSsid && openSsid !== ssid && !keepOpen) {
    const prev = document.getElementById('av-panel-' + cssIdSafe(openSsid));
    if (prev) prev.innerHTML = '';
  }
  openSsid = ssid;
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (!slot) return;
  // Don't trash a panel that's mid-flight (showing a spinner / result).
  if (slot.dataset.locked === '1') return;

  const net = scanResults.find(n => n.ssid === ssid);
  if (!net) { slot.innerHTML = ''; return; }

  const idsafe = cssIdSafe(ssid);
  const warn = connectRiskWarningHtml();

  let pwBlock = '';
  if (net.secured) {
    pwBlock =
      '<label for="pw-' + idsafe + '">Password</label>' +
      '<input id="pw-' + idsafe + '" type="password" autocomplete="off" autocapitalize="off" spellcheck="false">' +
      '<span class="show-pw" data-action="toggle-pw" ' +
           'data-ssid="' + escapeHtml(ssid) + '">' +
      'Show password</span>';
  } else {
    pwBlock = '<div class="meta" style="margin:0.4em 0">Open network — no password required.</div>';
  }

  slot.innerHTML =
    '<div class="panel" id="panel-' + idsafe + '">' +
    warn +
    pwBlock +
    '<div class="btns">' +
    '  <button data-action="submit-connect" ' +
          'data-ssid="' + escapeHtml(ssid) + '" ' +
          'data-secured="' + (net.secured ? 'true' : 'false') + '">Connect</button>' +
    '  <button class="secondary" data-action="close-connect" ' +
          'data-ssid="' + escapeHtml(ssid) + '">Cancel</button>' +
    '</div>' +
    '</div>';
}

function togglePw(ssid) {
  const input = document.getElementById('pw-' + cssIdSafe(ssid));
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
}

function closeConnect(ssid) {
  if (openSsid === ssid) openSsid = null;
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (slot && slot.dataset.locked !== '1') slot.innerHTML = '';
}

async function submitConnect(ssid, secured) {
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (!slot) return;
  let password = null;
  if (secured) {
    const input = document.getElementById('pw-' + cssIdSafe(ssid));
    password = input ? input.value : '';
    if (!password) {
      alert('Enter the password first.');
      return;
    }
  }
  slot.dataset.locked = '1';
  slot.innerHTML =
    '<div class="panel"><div><span class="spinner"></span> ' +
    'Connecting to ' + escapeHtml(ssid) + '… ' +
    '<span style="color:#888;font-size:0.85em">' +
    '(up to 90s including rollback)</span></div></div>';
  try {
    const r = await fetch('./connect', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(password === null ? {ssid: ssid} : {ssid: ssid, password: password}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      slot.innerHTML =
        '<div class="panel"><div class="result ok">✓ ' +
        escapeHtml(data.message || 'Connected') + '</div></div>';
      // Force a state refresh so the current-network card updates.
      setTimeout(fetchState, 500);
      // Clear the lock after a moment so the user can dismiss.
      setTimeout(function() {
        slot.dataset.locked = '';
        openSsid = null;
        slot.innerHTML = '';
      }, 3000);
    } else {
      slot.innerHTML =
        '<div class="panel"><div class="result err">' +
        escapeHtml(data.message || data.error || 'Connection failed') +
        '</div><div class="btns"><button class="secondary" ' +
        'data-action="dismiss-connect" ' +
        'data-ssid="' + escapeHtml(ssid) + '">Dismiss</button>' +
        '</div></div>';
      slot.dataset.locked = '1';
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    slot.innerHTML =
      '<div class="panel"><div class="result err">' +
      'Network error talking to the Wi-Fi backend.' +
      '</div><div class="btns"><button class="secondary" ' +
      'data-action="dismiss-connect" ' +
      'data-ssid="' + escapeHtml(ssid) + '">Dismiss</button>' +
      '</div></div>';
    slot.dataset.locked = '1';
  }
}

function dismissPanel(ssid) {
  const slot = document.getElementById('av-panel-' + cssIdSafe(ssid));
  if (slot) {
    slot.dataset.locked = '';
    slot.innerHTML = '';
  }
  if (openSsid === ssid) openSsid = null;
}

// Manual join --------------------------------------------------------
function toggleManualPw() {
  const input = document.getElementById('manual-password');
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
}

async function submitManualConnect() {
  if (!state.radioOn) {
    alert('Turn Wi-Fi on first.');
    return;
  }
  const ssidEl = document.getElementById('manual-ssid');
  const pwEl = document.getElementById('manual-password');
  const hiddenEl = document.getElementById('manual-hidden');
  const result = document.getElementById('manual-result');
  const btn = document.getElementById('manual-connect-btn');
  const ssid = (ssidEl ? ssidEl.value : '').trim();
  const password = pwEl ? pwEl.value : '';
  const hidden = hiddenEl ? hiddenEl.checked : false;
  if (!ssid) {
    alert('Enter the network name first.');
    return;
  }
  if (!confirmManualLockoutRisk(ssid)) return;

  const payload = {ssid: ssid, hidden: hidden};
  if (password) payload.password = password;
  if (btn) btn.disabled = true;
  result.innerHTML =
    '<div><span class="spinner"></span> Connecting to ' +
    escapeHtml(ssid) + '… <span style="color:#888;font-size:0.85em">' +
    '(up to 90s including rollback)</span></div>';
  try {
    const r = await fetch('./connect', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      result.innerHTML = '<div class="result ok">✓ ' +
        escapeHtml(data.message || 'Connected') + '</div>';
      setTimeout(fetchState, 500);
    } else {
      result.innerHTML = '<div class="result err">' +
        escapeHtml(data.message || data.error || 'Connection failed') +
        '</div>';
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    result.innerHTML =
      '<div class="result err">Network error talking to the Wi-Fi backend.</div>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Forget panel -------------------------------------------------------
function openForget(name, keepOpen) {
  if (openSavedName && openSavedName !== name && !keepOpen) {
    const prev = document.getElementById('sv-panel-' + cssIdSafe(openSavedName));
    if (prev) prev.innerHTML = '';
  }
  openSavedName = name;
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (!slot) return;
  if (slot.dataset.locked === '1') return;

  const isCurrent = state.current && state.current.profileName === name;
  // Look up the SSID for the panel copy — name is the NM profile name
  // which can be a hostile string for netplan-seeded profiles.
  const profile = (state.saved || []).find(p => p.name === name);
  const displayName = (profile && profile.ssid) || name;
  const extra = isCurrent
    ? '<div class="warn kill">⚠ This is the network the Pi is ' +
      'currently using. Forgetting it will disconnect Wi-Fi.' +
      (state.hasEthernet
        ? ' (Ethernet is connected so the Pi stays reachable.)'
        : ' Pi has no Ethernet fallback — you may lose access.') +
      '</div>'
    : '';

  slot.innerHTML =
    '<div class="panel">' + extra +
    '<div>Forget <strong>' + escapeHtml(displayName) + '</strong>? ' +
    'You\\'ll need the password again to reconnect.</div>' +
    '<div class="btns">' +
    '  <button class="danger" data-action="submit-forget" ' +
          'data-name="' + escapeHtml(name) + '">Forget</button>' +
    '  <button class="secondary" data-action="close-forget" ' +
          'data-name="' + escapeHtml(name) + '">Cancel</button>' +
    '</div></div>';
}

function closeForget(name) {
  if (openSavedName === name) openSavedName = null;
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (slot && slot.dataset.locked !== '1') slot.innerHTML = '';
}

async function submitForget(name) {
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (!slot) return;
  slot.dataset.locked = '1';
  slot.innerHTML = '<div class="panel"><div><span class="spinner"></span> ' +
                   'Forgetting…</div></div>';
  try {
    const r = await fetch('./forget', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({name: name}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      slot.innerHTML = '<div class="panel"><div class="result ok">✓ ' +
                       escapeHtml(data.message || 'Forgotten') + '</div></div>';
      setTimeout(function() {
        slot.dataset.locked = '';
        openSavedName = null;
        fetchState();
      }, 800);
    } else {
      slot.innerHTML =
        '<div class="panel"><div class="result err">' +
        escapeHtml(data.message || data.error || 'Failed') + '</div>' +
        '<div class="btns"><button class="secondary" ' +
        'data-action="dismiss-forget" ' +
        'data-name="' + escapeHtml(name) + '">Dismiss</button>' +
        '</div></div>';
    }
  } catch (e) {
    slot.innerHTML =
      '<div class="panel"><div class="result err">' +
      'Network error talking to the Wi-Fi backend.</div>' +
      '<div class="btns"><button class="secondary" ' +
      'data-action="dismiss-forget" ' +
      'data-name="' + escapeHtml(name) + '">Dismiss</button>' +
      '</div></div>';
  }
}

function dismissForget(name) {
  const slot = document.getElementById('sv-panel-' + cssIdSafe(name));
  if (slot) { slot.dataset.locked = ''; slot.innerHTML = ''; }
  if (openSavedName === name) openSavedName = null;
}

// Radio toggle -------------------------------------------------------
async function toggleRadio() {
  const input = document.getElementById('radio-toggle');
  const previous = !!state.radioOn;
  const target = input ? !!input.checked : !previous;
  function restoreToggle() {
    if (input) input.checked = previous;
  }
  if (target === previous) return;
  // Off path: the kill warning. We block in two places: when there's
  // no ethernet (existential — the user loses access), and otherwise
  // a milder confirm (annoying but recoverable).
  if (!target) {
    if (!state.hasEthernet) {
      const ok = window.confirm(
        '⚠ TURNING WI-FI OFF WILL DISCONNECT THIS PI.\\n\\n' +
        'You are reaching this page over Wi-Fi and the Pi has no ' +
        'Ethernet plugged in. As soon as Wi-Fi turns off, this page ' +
        'will stop responding and the ONLY way to turn it back on ' +
        'will be to physically access the Pi (plug in Ethernet or ' +
        'use a keyboard and monitor).\\n\\n' +
        'Continue?',
      );
      if (!ok) {
        restoreToggle();
        return;
      }
    } else {
      const ok = window.confirm(
        'Turn Wi-Fi off? The Pi will stay reachable on Ethernet, ' +
        'but any Wi-Fi-only renderers (AirPlay from a phone, etc.) ' +
        'will disconnect.',
      );
      if (!ok) {
        restoreToggle();
        return;
      }
    }
  }
  try {
    const r = await fetch('./radio', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      restoreToggle();
      alert('Radio toggle failed: ' + (data.message || data.error || r.status));
    }
  } catch (e) {
    // If we just turned off Wi-Fi and there's no ethernet, the fetch
    // never returns — that's expected. Don't alert.
    if (target || state.hasEthernet) {
      restoreToggle();
      alert('Network error talking to the Wi-Fi backend.');
    }
  }
  setTimeout(fetchState, 600);
}

// Bootstrap ----------------------------------------------------------
document.addEventListener('click', function(e) {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'open-connect') openConnect(el.dataset.ssid || '');
  if (action === 'toggle-pw') togglePw(el.dataset.ssid || '');
  if (action === 'submit-connect') {
    submitConnect(el.dataset.ssid || '', el.dataset.secured === 'true');
  }
  if (action === 'close-connect') closeConnect(el.dataset.ssid || '');
  if (action === 'dismiss-connect') dismissPanel(el.dataset.ssid || '');
  if (action === 'open-forget') openForget(el.dataset.name || '');
  if (action === 'submit-forget') submitForget(el.dataset.name || '');
  if (action === 'close-forget') closeForget(el.dataset.name || '');
  if (action === 'dismiss-forget') dismissForget(el.dataset.name || '');
});
fetchState();
schedulePoll(7000);
</script>
"""
    return _wrap_page(
        "Wi-Fi",
        body.replace("{csrf_fetch_helpers_js}", csrf_fetch_helpers_js()),
        csrf_token,
    )


def _wrap_page(title: str, body: str, csrf_token: str = "") -> bytes:
    csrf = csrf_meta_html(csrf_token) if csrf_token else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{csrf}
<title>{title}</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
{NAV_BACK_HTML}
<h1>{title}</h1>
{body}
</body>
</html>""".encode()


# ============================================================
# HTTP handler
# ============================================================


def _make_handler() -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send(status, body, "application/json")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 100_000:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                return {}

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                self._send_html(_landing_html(ctx["csrf_token"]))
                return
            if path == "/state":
                try:
                    self._send_json(gather_state())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/state failed")
                    self._send_json({"error": str(e)}, status=502)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {"/scan", "/connect", "/forget", "/radio"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            body = self._read_json()
            try:
                if path == "/scan":
                    self._send_json(scan_networks_report())
                    return
                if path == "/connect":
                    ssid = (body.get("ssid") or "").strip()
                    name = (body.get("name") or "").strip()
                    password = body.get("password")
                    hidden = bool(body.get("hidden"))
                    if ssid:
                        ok, msg = connect_new(
                            ssid, password or None, hidden=hidden,
                        )
                    elif name:
                        ok, msg = connect_saved(name)
                    else:
                        self._send_json(
                            {"ok": False, "message": "ssid or name required"},
                            status=400,
                        )
                        return
                    self._send_json({"ok": ok, "message": msg},
                                    status=200 if ok else 502)
                    return
                if path == "/forget":
                    name = (body.get("name") or "").strip()
                    if not name:
                        self._send_json(
                            {"ok": False, "message": "name required"},
                            status=400,
                        )
                        return
                    ok, msg = forget(name)
                    self._send_json({"ok": ok, "message": msg},
                                    status=200 if ok else 502)
                    return
                if path == "/radio":
                    on = bool(body.get("on"))
                    ok, msg = set_radio(on)
                    self._send_json({"ok": ok, "message": msg},
                                    status=200 if ok else 502)
                    return
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                self._send_json({"ok": False, "message": str(e)}, status=502)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(target) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per _systemd.make_http_server's contract."""
    from . import _systemd
    return _systemd.make_http_server(target, _make_handler())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wifi-web",
        description="Wi-Fi network management for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_WIFI_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_WIFI_WEB_PORT", "8775")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port))
    logger.info("jasper-wifi-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
