"""/wifi/ — Wi-Fi network management.

Phone-Settings-style page:
  - Current network card at top (always visible).
  - Available networks list in the middle (Scan button + tap-to-connect).
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
  POST /scan      {} → {networks: [...]} (triggers rescan first)
  POST /connect   {ssid, password?} | {name} → connect, rolls back on failure
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

from ._common import NAV_BACK_CSS, NAV_BACK_HTML

logger = logging.getLogger(__name__)


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
    """Return the list of saved WiFi connection profiles (sorted by name)."""
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
        saved.append({
            "name": name,
            "uuid": uuid,
            "autoconnect": autoconnect.lower() == "yes",
        })
    saved.sort(key=lambda p: p["name"].lower())
    return saved


# ============================================================
# Scan
# ============================================================


# Cap signal strength at 100; nmcli sometimes reports 101+ on saturated
# meters and we treat those identically.
_SIGNAL_MAX = 100


def scan_networks() -> list[dict[str, Any]]:
    """Trigger a fresh scan and return the deduplicated list of nearby
    networks (one entry per SSID, strongest BSSID wins).

    Hidden networks (those broadcasting with empty SSID) are filtered
    out — connecting to a hidden network is a separate manual-entry
    flow (deferred per PLAN.md)."""
    # rescan request — fire and proceed; the subsequent list call below
    # is what blocks for results. Without the rescan, the kernel-side
    # cache can be stale by minutes on a quiet network.
    _run_nmcli(
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
        return []

    by_ssid: dict[str, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        fields = _parse_terse(line)
        if len(fields) < 8:
            continue
        in_use, bssid, ssid, _mode, chan, _rate, signal_s, security = fields[:8]
        if not ssid:  # hidden — skip
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
        # Dedup by SSID, keep strongest signal.
        prev = by_ssid.get(ssid)
        if prev is None or entry["signal"] > prev["signal"]:
            by_ssid[ssid] = entry

    networks = list(by_ssid.values())
    networks.sort(key=lambda n: (-n["signal"], n["ssid"].lower()))
    return networks


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


def connect_new(ssid: str, password: str | None) -> tuple[bool, str]:
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

    cmd = ["nmcli", "--wait", str(_CONNECT_WAIT),
           "device", "wifi", "connect", ssid]
    if password:
        cmd.extend(["password", password])
    proc = _run_nmcli_secret(cmd, timeout=_CONNECT_TIMEOUT)

    if proc.returncode == 0:
        return True, f"Connected to {ssid}"

    err = (proc.stderr or proc.stdout or "").strip() or "Connection failed"
    # Trim nmcli's "Error: " prefix and the verbose "Connection activation
    # failed: (NN) " wrapper so the message that lands in the UI is
    # actually readable.
    err = re.sub(r"^Error:\s*", "", err)
    err = re.sub(r"^Connection activation failed:\s*\(\d+\)\s*", "", err)
    err = err.splitlines()[0] if err else "Connection failed"

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
        return True, f"Connected to {name}"
    err = (proc.stderr or proc.stdout or "").strip() or "Activation failed"
    err = re.sub(r"^Error:\s*", "", err).splitlines()[0]
    return False, err


def forget(name: str) -> tuple[bool, str]:
    """Delete a saved connection profile. If it's currently active,
    nmcli takes the device down with it."""
    proc = _run_nmcli(
        ["nmcli", "connection", "delete", name],
        timeout=10,
    )
    if proc.returncode == 0:
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


_PAGE_STYLE = """
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
  .switch {
    position: relative; width: 50px; height: 28px;
    background: #ccc; border-radius: 14px; cursor: pointer;
    transition: background 0.15s;
  }
  .switch.on { background: var(--green); }
  .switch.disabled { opacity: 0.5; cursor: not-allowed; }
  .switch .nub {
    position: absolute; top: 2px; left: 2px;
    width: 24px; height: 24px; background: white;
    border-radius: 50%; transition: left 0.15s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.2);
  }
  .switch.on .nub { left: 24px; }

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


def _landing_html() -> bytes:
    body = """
<p class="sub">Switch the speaker's Wi-Fi network or manage saved
networks. Changes take effect immediately.</p>

<div id="current"></div>

<h2>Available networks
  <button id="scan-btn" onclick="rescan()"
          style="float:right;font-size:0.8em;padding:0.3em 0.8em;">Scan</button>
</h2>
<div class="net-list" id="avail-list">
  <div class="empty">Tap Scan to look for nearby networks.</div>
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
let scanning = false;
let openSsid = null;     // available-list inline panel currently open
let openSavedName = null;// saved-list inline panel currently open
let stateTimer = null;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
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
    renderSaved();
  } catch (e) {
    document.getElementById('current').innerHTML =
      '<div class="current-card disconnected">' +
      '<div class="ssid">Status unavailable</div>' +
      '<div class="meta">Could not reach the Wi-Fi backend.</div>' +
      '</div>';
  }
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

  // Radio toggle row. The on→off path is gated by a confirm() with a
  // stark lockout warning if there's no ethernet fallback.
  const switchClass = 'switch' + (state.radioOn ? ' on' : '');
  inner += '<div class="radio-row">' +
           '  <div><div class="label">Wi-Fi radio</div>' +
           '    <div class="meta" style="font-size:0.85em">' +
                  (state.hasEthernet
                    ? 'Ethernet is connected — switching Wi-Fi is safe.'
                    : '<span style="color:#c44;font-weight:600">' +
                      'No Ethernet fallback. Be careful — turning ' +
                      'Wi-Fi off will disconnect the Pi.</span>') +
           '    </div>' +
           '  </div>' +
           '  <div class="' + switchClass +
                '" onclick="toggleRadio()"><div class="nub"></div></div>' +
           '</div>';

  wrap.className = '';
  wrap.innerHTML = '<div class="' + cardClass + '">' + inner + '</div>';
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
    return '<div class="net-row" id="sv-' + idsafe + '">' +
      '<div class="head">' +
      '  <div class="info">' +
      '    <div class="ssid">' + escapeHtml(p.name) + badge + '</div>' +
      '  </div>' +
      '  <div class="actions">' +
      '    <button class="danger" onclick="' +
              "openForget('" + jsArg(p.name) + "')" +
            '">Forget</button>' +
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

function jsArg(s) {
  // Escape a string for safe inclusion inside `'...'` in an onclick
  // HTML attribute. Two layers: JS-string escape (\\ and ') then HTML
  // attribute escape (& and "). The double-quoted HTML attribute
  // doesn't need ' escaped at the HTML layer; the double-quote does.
  var jsEsc = String(s).replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'");
  return jsEsc.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

// Available networks list --------------------------------------------
function renderAvail() {
  const list = document.getElementById('avail-list');
  if (!scanResults.length) {
    list.innerHTML = scanning
      ? '<div class="empty">Scanning…</div>'
      : '<div class="empty">Tap Scan to look for nearby networks.</div>';
    return;
  }
  list.innerHTML = scanResults.map(n => {
    const idsafe = cssIdSafe(n.ssid);
    const lock = n.secured ? ' 🔒' : '';
    const inUseBadge = n.inUse ? '<span class="badge">Connected</span>' : '';
    return '<div class="net-row" id="av-' + idsafe + '">' +
      '<div class="head" onclick="' +
            "openConnect('" + jsArg(n.ssid) + "')" + '">' +
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
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    const data = await r.json();
    scanResults = data.networks || [];
  } catch (e) {
    scanResults = [];
  } finally {
    scanning = false;
    btn.classList.remove('scanning');
    btn.innerHTML = 'Scan';
    btn.disabled = false;
    renderAvail();
  }
}

// Connect panel ------------------------------------------------------
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
  const warn = (state.lockoutRisk === 'high' && state.current)
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

  let pwBlock = '';
  if (net.secured) {
    pwBlock =
      '<label for="pw-' + idsafe + '">Password</label>' +
      '<input id="pw-' + idsafe + '" type="password" autocomplete="off" autocapitalize="off" spellcheck="false">' +
      '<span class="show-pw" onclick="togglePw(\\'' + jsArg(ssid) + '\\')">' +
      'Show password</span>';
  } else {
    pwBlock = '<div class="meta" style="margin:0.4em 0">Open network — no password required.</div>';
  }

  slot.innerHTML =
    '<div class="panel" id="panel-' + idsafe + '">' +
    warn +
    pwBlock +
    '<div class="btns">' +
    '  <button onclick="submitConnect(\\'' + jsArg(ssid) + '\\', ' +
        (net.secured ? 'true' : 'false') + ')">Connect</button>' +
    '  <button class="secondary" onclick="closeConnect(\\'' + jsArg(ssid) + '\\')">Cancel</button>' +
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
      headers: {'Content-Type': 'application/json'},
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
        'onclick="dismissPanel(\\'' + jsArg(ssid) + '\\')">Dismiss</button>' +
        '</div></div>';
      slot.dataset.locked = '1';
      setTimeout(fetchState, 500);
    }
  } catch (e) {
    slot.innerHTML =
      '<div class="panel"><div class="result err">' +
      'Network error talking to the Wi-Fi backend.' +
      '</div><div class="btns"><button class="secondary" ' +
      'onclick="dismissPanel(\\'' + jsArg(ssid) + '\\')">Dismiss</button>' +
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
    '<div>Forget <strong>' + escapeHtml(name) + '</strong>? ' +
    'You\\'ll need the password again to reconnect.</div>' +
    '<div class="btns">' +
    '  <button class="danger" onclick="submitForget(\\'' + jsArg(name) + '\\')">Forget</button>' +
    '  <button class="secondary" onclick="closeForget(\\'' + jsArg(name) + '\\')">Cancel</button>' +
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
      headers: {'Content-Type': 'application/json'},
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
        'onclick="dismissForget(\\'' + jsArg(name) + '\\')">Dismiss</button>' +
        '</div></div>';
    }
  } catch (e) {
    slot.innerHTML =
      '<div class="panel"><div class="result err">' +
      'Network error talking to the Wi-Fi backend.</div>' +
      '<div class="btns"><button class="secondary" ' +
      'onclick="dismissForget(\\'' + jsArg(name) + '\\')">Dismiss</button>' +
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
  const target = !state.radioOn;
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
      if (!ok) return;
    } else {
      const ok = window.confirm(
        'Turn Wi-Fi off? The Pi will stay reachable on Ethernet, ' +
        'but any Wi-Fi-only renderers (AirPlay from a phone, etc.) ' +
        'will disconnect.',
      );
      if (!ok) return;
    }
  }
  try {
    const r = await fetch('./radio', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      alert('Radio toggle failed: ' + (data.message || data.error || r.status));
    }
  } catch (e) {
    // If we just turned off Wi-Fi and there's no ethernet, the fetch
    // never returns — that's expected. Don't alert.
  }
  setTimeout(fetchState, 600);
}

// Bootstrap ----------------------------------------------------------
fetchState();
schedulePoll(7000);
</script>
"""
    return _wrap_page("Wi-Fi", body)


def _wrap_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
            self._send(status, body, "text/html; charset=utf-8")

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
                self._send_html(_landing_html())
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
            body = self._read_json()
            try:
                if path == "/scan":
                    networks = scan_networks()
                    self._send_json({"networks": networks})
                    return
                if path == "/connect":
                    ssid = (body.get("ssid") or "").strip()
                    name = (body.get("name") or "").strip()
                    password = body.get("password")
                    if ssid:
                        ok, msg = connect_new(ssid, password or None)
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
