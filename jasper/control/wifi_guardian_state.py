"""Read-only state snapshot for `/state.resilience.wifi_guardian`.

The WiFi profile guardian is a `Type=oneshot` systemd unit that runs at
boot — no resident daemon to ask for state. This module's `snapshot()`
synthesizes the equivalent by reading three sources:

  1. The wizard-owned stash file (``/var/lib/jasper/wifi_guardian.env``)
  2. The most recent `event=wifi_guardian.*` line in the systemd journal
     for `jasper-wifi-guardian.service`
  3. The current NM-active WiFi SSID via `nmcli`

The shape mirrors `shairport_supervisor.snapshot()`: always returns a
dict, never raises, includes a top-level `enabled` field so consumers
can distinguish "feature off / no stash yet" from "feature on, here's
the latest state."

The PSK is never read from the stash by this module. Only SSID +
key_mgmt + last-action metadata.

Cost: ~30 ms typical (one stat() + one nmcli + one journalctl call).
Called from the `/state` aggregator which already runs ~200 ms parallel
probes, so fits well inside the budget.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from ..wifi_guardian_persistence import (
    DEFAULT_PATH as _DEFAULT_STASH,
    read_stash,
)

logger = logging.getLogger(__name__)


_ACTIONS_LOOKED_FOR = (
    "wifi_guardian.steady_state",
    "wifi_guardian.stash_stale",
    "wifi_guardian.activate",
    "wifi_guardian.activate_ok",
    "wifi_guardian.activate_fail",
    "wifi_guardian.recreate_attempt",
    "wifi_guardian.recreate_ok",
    "wifi_guardian.recreate_fail",
    "wifi_guardian.absent",
    "wifi_guardian.skip",
)


def _stash_path() -> str:
    return os.environ.get("JASPER_WIFI_STASH_FILE", _DEFAULT_STASH)


def _active_ssid() -> str | None:
    """Probe NM for the currently-active WiFi SSID. Returns None if
    nmcli isn't installed, no WiFi connection is active, or the call
    fails for any reason — this is a /state probe, not a doctor check,
    so all failure paths report "unknown" rather than raising."""
    try:
        proc = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection",
             "show", "--active"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    active_name: str | None = None
    for raw in proc.stdout.splitlines():
        parts = raw.split(":", 1)
        if len(parts) == 2 and parts[1] in ("802-11-wireless", "wifi"):
            active_name = parts[0]
            break
    if not active_name:
        return None
    # Resolve to actual SSID (netplan-seeded profile NAMEs can differ).
    try:
        ssid_proc = subprocess.run(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid",
             "connection", "show", active_name],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return active_name
    if ssid_proc.returncode != 0:
        return active_name
    for raw in ssid_proc.stdout.splitlines():
        if raw.startswith("802-11-wireless.ssid:"):
            val = raw.split(":", 1)[1]
            if val:
                return val
            break
    return active_name


def _last_guardian_event() -> dict[str, Any] | None:
    """Read the most recent `event=wifi_guardian.*` line from the
    journal for jasper-wifi-guardian.service. Returns
    ``{"action": str, "ts": str}`` or None if no run has happened or
    journalctl is unreachable.

    Uses `journalctl --output=json -n 200` and scans for the newest
    matching MESSAGE. Bounded at 200 lines so this stays cheap even
    on a noisy unit."""
    try:
        proc = subprocess.run(
            ["journalctl",
             "--unit=jasper-wifi-guardian.service",
             "--output=cat",
             "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
             "--no-pager",
             "-n", "200",
             "-o", "json"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    import json
    newest: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("MESSAGE", "")
        if not isinstance(msg, str):
            continue
        # Find an `event=wifi_guardian.<action>` token.
        for action in _ACTIONS_LOOKED_FOR:
            tag = f"event={action}"
            if tag in msg:
                ts_raw = entry.get("__REALTIME_TIMESTAMP")
                # journalctl JSON returns realtime as a microsecond
                # Unix epoch string; convert to ISO 8601 for /state.
                ts_iso: str | None = None
                if isinstance(ts_raw, str) and ts_raw.isdigit():
                    try:
                        from datetime import datetime, timezone
                        ts_iso = datetime.fromtimestamp(
                            int(ts_raw) / 1_000_000, tz=timezone.utc,
                        ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except (OverflowError, OSError, ValueError):
                        ts_iso = None
                newest = {"action": action.split(".", 1)[1], "ts": ts_iso}
                # Don't break — `-n 200` is newest-last on systemd's
                # `-o json` output, so the LAST matching entry we see
                # is the most recent. (Confirmed against systemd 257.)
        # Loop falls through — newest gets overwritten until we hit the
        # latest entry.
    return newest


def snapshot() -> dict[str, Any]:
    """Read-only state for `/state.resilience.wifi_guardian`.

    Always returns a dict, never raises. ``enabled`` is True when a
    stash file exists (the guardian's `ConditionPathExists=` would have
    let the unit run on the last boot); False otherwise.

    Fields:
      - ``enabled``                  (bool)   stash file present
      - ``stash_ssid``               (str|None) SSID in the stash (no PSK)
      - ``stash_key_mgmt``           (str|None) e.g. ``wpa-psk`` / ``none``
      - ``active_ssid``              (str|None) what NM is currently on
      - ``stash_matches_active``     (bool|None) when both are known
      - ``last_action``              (str|None) e.g. ``steady_state``
      - ``last_run_at``              (str|None) ISO-8601 UTC
    """
    out: dict[str, Any] = {
        "enabled": False,
        "stash_ssid": None,
        "stash_key_mgmt": None,
        "active_ssid": None,
        "stash_matches_active": None,
        "last_action": None,
        "last_run_at": None,
    }

    try:
        stash = read_stash(_stash_path())
    except Exception as e:  # noqa: BLE001
        logger.warning("event=wifi_guardian_state.stash_read_failed err=%r", e)
        stash = None

    if stash is not None:
        out["enabled"] = True
        out["stash_ssid"] = stash.ssid
        out["stash_key_mgmt"] = stash.key_mgmt

    active = _active_ssid()
    if active is not None:
        out["active_ssid"] = active
    if stash is not None and active is not None:
        out["stash_matches_active"] = stash.ssid == active

    last = _last_guardian_event()
    if last is not None:
        out["last_action"] = last.get("action")
        out["last_run_at"] = last.get("ts")

    return out
