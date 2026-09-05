# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The systemd unit roster and a shared ``systemctl show`` reader.

jasper-control's samplers, jasper-doctor and jasper-system-soak read unit
state through here, sharing one roster and one parser (ADR-0233 rule 1).
Stdlib only: the doctor imports this on every run.
"""
from __future__ import annotations

import subprocess
from typing import Any

# Dashboard group per JTS unit. A jasper-*.service not listed here still
# renders, under "JTS".
JASPER_SERVICE_GROUPS = {
    "jasper-aec-bridge.service": "Mic",
    "jasper-voice.service": "Voice",
    "jasper-camilla.service": "Audio",
    "jasper-fanin.service": "Audio",
    "jasper-outputd.service": "Audio",
    "jasper-mux.service": "Audio",
    "jasper-usbgadget.service": "Audio",
    "jasper-usbsink.service": "Audio",
    "jasper-usbsink-volume.service": "Audio",
    "jasper-control.service": "Control",
    "jasper-web.service": "Control",
    "jasper-system-web.service": "Control",
    "jasper-input.service": "Hardware",
    "jasper-accessory-reconcile.service": "Hardware",
    "jasper-headphone-monitor.service": "Hardware",
}

EXTRA_SERVICE_GROUPS = {
    "shairport-sync.service": "Audio",
    "librespot.service": "Audio",
    "bluealsa.service": "Audio",
    "bluealsa-aplay.service": "Audio",
    "nqptp.service": "Audio",
    "nginx.service": "Web",
    "avahi-daemon.service": "Network",
    "NetworkManager.service": "Network",
    "wpa_supplicant.service": "Network",
    "ssh.service": "System",
    "dbus.service": "System",
    "systemd-journald.service": "System",
    "bluetooth.service": "System",
    "bt-agent.service": "System",
}

# Units the doctor judges that the dashboard roster does not carry. A unit a
# check asks about that is on neither list is read on demand.
DOCTOR_EXTRA_UNITS = (
    "jasper-fanin-coupling-auto.service",
    "jasper-aec-commission.service",
    "jasper-enhanced-aec-install.service",
    "jasper-usbnet-dhcp.service",
    "jasper-camilla-crossover.service",
    "jasper-snapclient.service",
    "jasper-snapserver.service",
    "jasper-usbmic.service",
    "jasper-chat-web.service",
    "jasper-bluetooth-web.service",
    "jasper-correction-web.service",
    "jasper-correction-web.socket",
    "jasper-bluetooth-web.socket",
    "jasper-chat-web.socket",
    "jasper-web.socket",
    "jasper-system-web.socket",
    "jasper-wifi-recover.timer",
    "jasper-accessory-reconcile.path",
)

DOCTOR_UNIT_ROSTER: tuple[str, ...] = (
    *JASPER_SERVICE_GROUPS,
    *EXTRA_SERVICE_GROUPS,
    *DOCTOR_EXTRA_UNITS,
)

SHOW_PROPERTIES = (
    "Id", "LoadState", "ActiveState", "SubState", "UnitFileState", "Result",
    "NRestarts", "MainPID", "TasksCurrent", "MemoryCurrent", "CPUUsageNSec",
    "ControlGroup",
)


def systemd_int(value: str | None) -> int | None:
    """An integer property, or None for unset: an empty value, a bracketed
    placeholder such as ``[not set]``, or UINT64_MAX (systemd's unset
    accounting value)."""
    raw = (value or "").strip()
    if not raw or raw.startswith("["):
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    if parsed >= (1 << 63):
        return None
    return parsed


def show_blocks(text: str) -> list[dict[str, str]]:
    """``systemctl show`` output as one ``Key=value`` mapping per unit, in
    output order. Units are separated by a blank line; a line without ``=``
    is dropped."""
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        cur[key] = value
    if cur:
        blocks.append(cur)
    return blocks


def parse_property_blocks(text: str, prop: str) -> list[str]:
    """``prop``'s value per unit from ``systemctl show --property=<prop> u1
    u2 ...``, in output order. A unit whose value is empty still emits a
    ``<prop>=`` line, so an empty value keeps its slot; a block missing the
    property yields ``""``."""
    return [block.get(prop, "") for block in show_blocks(text)]


def parse_systemctl_show_units(text: str) -> dict[str, dict[str, Any]]:
    """``systemctl show`` output for N units, keyed by unit name.

    Units are blank-line separated blocks of ``Key=value`` lines. The
    numeric properties are coerced; string properties are None when
    systemd emitted them empty.
    """
    records = show_blocks(text)

    out: dict[str, dict[str, Any]] = {}
    for record in records:
        unit = (record.get("Id") or record.get("Names") or "").split()[0]
        if not unit:
            continue
        out[unit] = {
            "unit": unit,
            "load_state": record.get("LoadState") or None,
            "active_state": record.get("ActiveState") or None,
            "sub_state": record.get("SubState") or None,
            "unit_file_state": record.get("UnitFileState") or None,
            "result": record.get("Result") or None,
            "n_restarts": systemd_int(record.get("NRestarts")) or 0,
            "main_pid": systemd_int(record.get("MainPID")) or 0,
            "tasks_current": systemd_int(record.get("TasksCurrent")),
            "memory_current_bytes": systemd_int(record.get("MemoryCurrent")),
            "cpu_usage_nsec": systemd_int(record.get("CPUUsageNSec")),
            "control_group": record.get("ControlGroup") or "",
        }
    return out


def read_unit_states(
    units: tuple[str, ...] | list[str],
    *,
    timeout: float = 2.0,
) -> dict[str, dict[str, Any]] | None:
    """One ``systemctl show`` over ``units``; None when systemctl itself is
    unavailable or the call fails, so a caller can say "unknown" rather than
    "inactive". A unit systemd does not know comes back with
    ``load_state == "not-found"``.

    A non-empty ask that yields NO records is also ``None``: systemctl ran but
    answered nothing (no D-Bus, a host not booted with systemd), which is
    "unknown", not "none of these units exist" — the latter would let a caller
    report every unit as missing on a box where they are all running."""
    if not units:
        return {}
    cmd = ["systemctl", "show", "--no-page"]
    cmd.extend(f"--property={prop}" for prop in SHOW_PROPERTIES)
    cmd.extend(units)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode not in (0, 1):
        return None
    return parse_systemctl_show_units(proc.stdout) or None
