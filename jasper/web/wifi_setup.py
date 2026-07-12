# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
from ..control.restart_broker import manage_units
from ..log_event import log_event
from ._common import (
    JsonBodyError,
    begin_request,
    canonical_header,
    canonical_page,
    reject_csrf,
    read_json_object,
    send_html_response,
    send_json_response,
    guard_read_request,
    guard_mutating_request,
)

logger = logging.getLogger(__name__)


# Preserve the wizard's established direct-socket ceiling. Connect payloads
# are normally tiny, but the cap is endpoint-owned rather than shared policy.
_JSON_BODY_LIMIT = 100_000


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
_CONNECT_PREFLIGHT_TIMEOUT_CEILING = 25
_CONNECT_CLEANUP_TIMEOUT = 10
# Worst serialized POST /connect outer-timeout budget: _current_wifi plus
# _profile_exists can issue five 5 s reads; a scan-cache miss can drive two
# connect attempts; failed new-profile cleanup gets 10 s; rollback gets 30 s.
# nginx must exceed this ceiling with response/NetworkManager scheduling room.
CONNECT_NEW_TIMEOUT_CEILING = (
    _CONNECT_PREFLIGHT_TIMEOUT_CEILING
    + (2 * _CONNECT_TIMEOUT)
    + _CONNECT_CLEANUP_TIMEOUT
    + _ROLLBACK_TIMEOUT
)
_SCAN_HEALTH_JOURNAL_LINES = 120
_SCAN_REPAIR_IFACE = os.environ.get("JASPER_WIFI_SCAN_REPAIR_IFACE", "wlan0")
_SCAN_REPAIR_UNIT = os.environ.get(
    "JASPER_WIFI_SCAN_REPAIR_UNIT", "jasper-wifi-scan-repair.service",
)
_SCAN_REPAIR_RETRY_DELAYS = (2.0, 3.0)
_NM_AUTOCONNECT_RETRIES_FOREVER = "0"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
_SCAN_REPAIR_ROOT_TIMEOUT = _env_float("JASPER_WIFI_SCAN_REPAIR_ROOT_TIMEOUT", 20.0)


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
        logger.warning(
            "nmcli timed out after %ss: %s",
            timeout,
            " ".join(_scrub_argv(cmd)),
        )
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


def _harden_wifi_profile(profile_name: str) -> None:
    """Best-effort NetworkManager resilience settings for JTS-owned WiFi.

    `connection.autoconnect-retries 0` is NetworkManager's "retry forever"
    value. Without this, `-1` falls back to NM's global default and a long
    household/router flap can exhaust retries before the AP is healthy again.
    `ipv6.method link-local` keeps mDNS fast for `.local` hostnames without
    enabling routed IPv6; iOS/macOS clients otherwise wait on IPv6 mDNS before
    falling back to IPv4 when the profile is set to `ignore`.

    Best-effort by contract: a hardening failure (nmcli non-zero, timeout,
    or even a missing/unrunnable nmcli binary) MUST NOT turn a successful
    user connect into a failed one — so every failure mode here is logged at
    WARNING and swallowed (see AGENTS.md "harden that NM profile … keep that
    resilience write best-effort"). The identical key set is written at
    install by `tune_wifi_for_airplay` and on recovery by the guardian's
    `harden_profile`; drift across the three is guarded by
    tests/test_wifi_profile_hardening_contract.py.
    """
    try:
        proc = _run_nmcli(
            [
                "nmcli", "connection", "modify", profile_name,
                "connection.autoconnect", "yes",
                "connection.autoconnect-retries", _NM_AUTOCONNECT_RETRIES_FOREVER,
                "802-11-wireless.powersave", "2",
                "ipv6.method", "link-local",
            ],
            timeout=10,
        )
    except OSError as exc:
        # e.g. nmcli not on PATH. _run_nmcli already absorbs TimeoutExpired.
        log_event(
            logger,
            "wifi.profile_harden_failed",
            profile=profile_name,
            err=f"{type(exc).__name__}: {exc}",
            level=logging.WARNING,
        )
        return
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        log_event(
            logger,
            "wifi.profile_harden_failed",
            profile=profile_name,
            err=err[0] if err else "unknown",
            level=logging.WARNING,
        )


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


def _read_scan_repair_state() -> dict[str, Any]:
    """Read the repair helper's rate-limit state, best-effort."""
    try:
        raw = json.loads(wifi_scan_repair.DEFAULT_STATE_PATH.read_text(
            encoding="utf-8",
        ))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _root_scan_repair_result(iface: str) -> dict[str, Any]:
    started_at = time.time()
    resp = manage_units(
        _SCAN_REPAIR_UNIT,
        verb="start",
        reason="wifi-scan-suppressed",
        no_block=False,
        timeout=_SCAN_REPAIR_ROOT_TIMEOUT,
    )
    result: dict[str, Any] = {
        "iface": iface,
        "attempted": False,
        "reason": "root_unit_start_failed",
        "unit": _SCAN_REPAIR_UNIT,
        "broker": resp,
    }
    if not resp.get("ok"):
        return result

    result["reason"] = "root_unit_started"
    state = _read_scan_repair_state()
    state_iface = state.get("iface")
    last_attempt_at = _float_or_none(state.get("lastAttemptAt")) or 0.0
    if state_iface == iface and last_attempt_at >= started_at - 1.0:
        result["attempted"] = True
        result["ack"] = bool(state.get("lastAck"))
        if state.get("lastReason"):
            result["reason"] = str(state["lastReason"])
        if state.get("error"):
            result["error"] = str(state["error"])
    elif state.get("nextAllowedAt") is not None:
        next_allowed_at = _float_or_none(state.get("nextAllowedAt"))
        remaining = (
            max(0.0, next_allowed_at - time.time())
            if next_allowed_at is not None
            else 0.0
        )
        result["reason"] = "cooldown"
        result["cooldownRemaining"] = round(remaining, 3)
    return result


def _repair_scan_suppression(iface: str) -> dict[str, Any]:
    """Run the bounded scan repair directly as root or via the root helper."""
    if os.geteuid() == 0:
        return wifi_scan_repair.maybe_repair_scan_suppression(iface).to_dict()
    return _root_scan_repair_result(iface)


def scan_networks_report(*, allow_repair: bool = True) -> dict[str, Any]:
    """Trigger a fresh scan and optionally repair Pi 5 scan suppression."""
    report = _scan_networks_report_once()
    if not allow_repair or report["scan"].get("reason") != "driver_scan_suppressed":
        return report

    repair_dict = _repair_scan_suppression(_SCAN_REPAIR_IFACE)
    if not (repair_dict.get("attempted") and repair_dict.get("ack")):
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


def _scrub_psk(text: str, password: str | None) -> str:
    """Mask the household PSK out of nmcli output before it is logged or
    returned to the browser. Ports deploy/bin/jasper-wifi-guardian's
    scrub_psk: (1) replace the literal PSK, (2) replace `password <arg>`
    echo-back (nmcli can quote the PSK back verbatim in error text, and
    the PSK rode this same nmcli's argv)."""
    if password:
        text = text.replace(password, "***")
    return re.sub(r"password\s+\S+", "password ***", text)


def _readable_nmcli_error(
    proc: subprocess.CompletedProcess[str], password: str | None = None,
) -> str:
    err = (proc.stderr or proc.stdout or "").strip() or "Connection failed"
    # Scrub any echoed PSK BEFORE trimming/returning — nmcli can echo the
    # submitted password back in error text (argv, "password 'PSK'"), and
    # this string is both logged and shipped to the browser as the error.
    err = _scrub_psk(err, password)
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
            log_event(
                logger,
                "wifi_guardian.stash_skip",
                profile=profile_name,
                reason="secrets_unavailable",
            )
            return
        ssid, psk, key_mgmt = secrets
        if key_mgmt == "wpa-eap":
            log_event(
                logger,
                "wifi_guardian.stash_skip",
                ssid=ssid,
                reason="enterprise",
            )
            return
        wifi_guardian_persistence.write_stash(
            _STASH_PATH, ssid, psk, key_mgmt,
        )
        log_event(
            logger,
            "wifi_guardian.stash_written",
            ssid=ssid,
            key_mgmt=key_mgmt,
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "wifi_guardian.stash_write_failed",
            profile=profile_name,
            err=repr(e),
            level=logging.WARNING,
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
            log_event(
                logger,
                "wifi_guardian.stash_skip",
                ssid=ssid,
                reason="enterprise",
            )
            return
        wifi_guardian_persistence.write_stash(
            _STASH_PATH, ssid, password or "", key_mgmt,
        )
        # PSK never appears in the log line — only the SSID and key_mgmt.
        log_event(
            logger,
            "wifi_guardian.stash_written",
            ssid=ssid,
            key_mgmt=key_mgmt,
        )
    except Exception as e:  # noqa: BLE001
        # Wrap-all because this is a recovery aid path. Anything that
        # raises here (full disk, permission flip, nmcli timeout in
        # _resolve_key_mgmt) should not block the user's successful
        # connect from returning.
        log_event(
            logger,
            "wifi_guardian.stash_write_failed",
            ssid=ssid,
            err=repr(e),
            level=logging.WARNING,
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
        log_event(logger, "wifi_guardian.stash_cleared", ssid=ssid)
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "wifi_guardian.stash_clear_failed",
            ssid=ssid,
            err=repr(e),
            level=logging.WARNING,
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
    err = _readable_nmcli_error(proc, password)

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
            err = _readable_nmcli_error(hidden_proc, password)

    if proc.returncode == 0:
        _harden_wifi_profile(ssid)
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
            timeout=_CONNECT_CLEANUP_TIMEOUT, log_argv=False,
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
        _harden_wifi_profile(name)
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


def _landing_html(csrf_token: str = "") -> bytes:
    """Render the /wifi/ page on the canonical design system.

    The page is live (fetch-driven): a 7 s ``./state`` poll, on-demand
    ``./scan``, and inline Connect / Forget panels that POST to
    ``./connect`` / ``./forget`` / ``./radio``. So this returns only the
    static shell — the current-network slot, the scan list, the join-by-name
    fields, and the saved-networks collapse — and the behaviour ships as the
    ES module ``/assets/wifi/js/main.js`` (which reads the CSRF token from the
    ``<meta name="jts-csrf">`` tag ``canonical_page()`` emits and attaches it
    to every mutating POST via the shared ``jsonHeaders()``).

    There is no server-rendered ``<form>`` here and no flash banner: status is
    surfaced inline by the module, not via the PRG flash cookie. Page-specific
    styling lives in ``/assets/wifi/wifi.css``; shared primitives come from
    ``app.css``."""
    body = f"""
{canonical_header("Wi-Fi")}
<main class="page">
  <p class="form-hint">Switch the speaker's Wi-Fi network or manage saved
  networks. Changes take effect immediately.</p>

  <div id="current"></div>

  <div class="wifi-region">
    <h2 class="eyebrow">Available networks</h2>
    <button id="scan-btn" class="btn btn--ghost" data-action="rescan">Scan</button>
  </div>
  <div id="scan-health"></div>
  <div class="net-list" id="avail-list">
    <div class="empty">Tap Scan to look for nearby networks.</div>
  </div>

  <div class="wifi-region">
    <h2 class="eyebrow">Join by name</h2>
  </div>
  <div class="info-card manual-fields">
    <div class="field">
      <label for="manual-ssid">Network name</label>
      <input id="manual-ssid" type="text" autocomplete="off"
             autocapitalize="off" spellcheck="false">
    </div>
    <div class="field">
      <label for="manual-password">Password</label>
      <input id="manual-password" type="password" autocomplete="off"
             autocapitalize="off" spellcheck="false">
      <span class="show-pw" data-action="toggle-manual-pw">Show password</span>
      <label class="manual-check" for="manual-hidden">
        <input id="manual-hidden" type="checkbox">
        Hidden network
      </label>
    </div>
    <div id="manual-result"></div>
    <div class="form-actions">
      <button id="manual-connect-btn" class="btn btn--primary"
              data-action="submit-manual">Connect</button>
    </div>
  </div>

  <details class="disclosure">
    <summary>Saved networks <span class="saved-count" id="saved-count"></span></summary>
    <div class="disclosure-body">
      <div class="net-list" id="saved-list">
        <div class="empty">Loading…</div>
      </div>
    </div>
  </details>
</main>
<script type="module" src="/assets/wifi/js/main.js"></script>
"""
    return canonical_page(
        "Wi-Fi", body,
        csrf_token=csrf_token,
        page_css_href="/assets/wifi/wifi.css",
    )




# ============================================================
# HTTP handler
# ============================================================


def _make_handler() -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            send_json_response(self, payload, status=status)

        def _read_json(self) -> dict[str, Any]:
            try:
                return read_json_object(self, max_bytes=_JSON_BODY_LIMIT)
            except (JsonBodyError, OSError):
                return {}

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                self._send_html(_landing_html(ctx["csrf_token"]))
                return
            if path == "/state":
                if not guard_read_request(self):
                    return
                try:
                    payload = gather_state()
                    status = 200
                except Exception as e:  # noqa: BLE001
                    logger.exception("/state failed")
                    payload = {"error": str(e)}
                    status = 502
                self._send_json(payload, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path not in {"/scan", "/connect", "/forget", "/radio"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            body = self._read_json()
            response_committed = False

            def commit_json_response(
                payload: dict[str, Any], *, status: int = 200,
            ) -> None:
                nonlocal response_committed
                if response_committed:
                    raise RuntimeError("response already committed")
                response_committed = True
                self._send_json(payload, status=status)

            try:
                if path == "/scan":
                    commit_json_response(scan_networks_report())
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
                        commit_json_response(
                            {"ok": False, "message": "ssid or name required"},
                            status=400,
                        )
                        return
                    connect_fields = (
                        {"mode": "new", "ssid": ssid}
                        if ssid
                        else {"mode": "saved", "profile": name}
                    )
                    log_event(
                        logger,
                        "wifi.connect",
                        fields=connect_fields,
                        ok=ok,
                        client=self.address_string(),
                        level=logging.INFO if ok else logging.WARNING,
                    )
                    commit_json_response(
                        {"ok": ok, "message": msg},
                        status=200 if ok else 502,
                    )
                    return
                if path == "/forget":
                    name = (body.get("name") or "").strip()
                    if not name:
                        commit_json_response(
                            {"ok": False, "message": "name required"},
                            status=400,
                        )
                        return
                    ok, msg = forget(name)
                    log_event(
                        logger,
                        "wifi.forget",
                        profile=name,
                        ok=ok,
                        client=self.address_string(),
                        level=logging.INFO if ok else logging.WARNING,
                    )
                    commit_json_response(
                        {"ok": ok, "message": msg},
                        status=200 if ok else 502,
                    )
                    return
                if path == "/radio":
                    if (
                        not isinstance(body, dict)
                        or type(body.get("on")) is not bool
                    ):
                        commit_json_response(
                            {"ok": False, "message": "on must be a boolean"},
                            status=400,
                        )
                        return
                    on = body["on"]
                    ok, msg = set_radio(on)
                    log_event(
                        logger,
                        "wifi.radio",
                        enabled=on,
                        ok=ok,
                        client=self.address_string(),
                        level=logging.INFO if ok else logging.WARNING,
                    )
                    commit_json_response(
                        {"ok": ok, "message": msg},
                        status=200 if ok else 502,
                    )
                    return
            except Exception as e:  # noqa: BLE001
                if response_committed:
                    raise
                log_event(
                    logger,
                    "wifi.post_dispatch_failed",
                    action=path.removeprefix("/"),
                    error=type(e).__name__,
                    ok=False,
                    client=self.address_string(),
                    level=logging.ERROR,
                )
                commit_json_response(
                    {"ok": False, "message": "Wi-Fi action failed"},
                    status=502,
                )
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
