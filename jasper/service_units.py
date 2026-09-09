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
import time
from typing import Any, Mapping, Sequence

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
    "jasper-journal-review.timer",
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
    "ControlGroup", "ActiveEnterTimestampMonotonic",
)

# Bound on ONE `systemctl` invocation a reconciler makes to CHANGE unit state
# (enable/start/stop/restart/reset-failed). Reconcilers run from udev and from
# install, where an unresponsive manager would otherwise hang the pass
# indefinitely; their own pass ceilings are multiples of this. The read-only
# `show` probes above take a much shorter timeout of their own.
SYSTEMCTL_TIMEOUT_SEC = 10.0


def unit_failed(record: Mapping[str, Any] | None) -> bool:
    """Whether a ``read_unit_states`` record says the unit is not doing its job.

    The union of the copies already in the tree, which is
    ``control.audio_health._service_failed``'s — the wider one, and the correct
    one:

    * ``active_state == "failed"`` — systemd's own verdict.
    * ``load_state`` ``error``/``not-found`` — a unit systemd cannot load is not
      running either, and it reads ``inactive`` rather than ``failed``.
    * a non-success ``Result`` while not ``active`` — ``Result`` survives into
      ``inactive`` (a start-limited unit sits there carrying ``exit-code``), and
      the ``active`` guard keeps a unit that has since recovered out of it.

    ``None`` (systemctl unavailable) is NOT failed: that is unknown, and a
    caller must say so rather than report a healthy speaker.
    """
    if not record:
        return False
    active_state = str(record.get("active_state") or "")
    return (
        active_state == "failed"
        or str(record.get("load_state") or "") in {"error", "not-found"}
        or (
            str(record.get("result") or "") not in {"", "success"}
            and active_state != "active"
        )
    )


def unit_unstable(record: Mapping[str, Any] | None) -> bool:
    """Whether a ``read_unit_states`` record is stuck mid-transition.

    ``active_state`` ``activating``/``deactivating`` is a Type=oneshot unit's
    NORMAL in-flight state, not instability — a caller tracking oneshots must
    exclude those itself (see
    ``jasper.cli.doctor._shared._ONESHOT_RUNTIME_STATE_UNITS``); on a
    long-running daemon it signals a stuck start/stop.
    """
    if not record:
        return False
    return str(record.get("active_state") or "") in {"activating", "deactivating"}


def unit_loaded(record: Mapping[str, Any] | None) -> bool:
    """Whether a ``read_unit_states`` record says the unit is installed
    (systemd could load its unit file). ``None`` (missing from the batch,
    or systemctl unavailable) is not loaded — same fail-soft rule as
    :func:`unit_failed`."""
    if not record:
        return False
    return str(record.get("load_state") or "") == "loaded"


def unit_active(record: Mapping[str, Any] | None) -> bool:
    """Whether a ``read_unit_states`` record says the unit is currently
    running. ``None`` is not active."""
    if not record:
        return False
    return str(record.get("active_state") or "") == "active"


def unit_activating(record: Mapping[str, Any] | None) -> bool:
    """Whether a ``read_unit_states`` record says the unit is starting up.
    ``None`` is not activating."""
    if not record:
        return False
    return str(record.get("active_state") or "") == "activating"


def unit_not_running(record: Mapping[str, Any] | None) -> str | None:
    """Small stable code for a ``read_unit_states`` record that is not doing
    its job, or ``None`` when ``active_state == "active"``.

    Codes, checked in this order:

    * ``"missing"`` — no record, or ``load_state == "not-found"``.
    * ``"not_enabled"`` — ``unit_file_state`` known and neither ``enabled``
      nor ``enabled-runtime``.
    * ``None`` — active.
    * ``"starting"`` — ``active_state`` ``activating``/``reloading``.
    * ``"inactive"`` — anything else (a clean stop, ``failed``, a
      jasper-camilla-recover park).

    Shared by :mod:`jasper.control.audio_health` and jasper-doctor's
    ``_service_state_failure``/``check_camilla_service`` (#2163, ADR-0175).
    """
    if record is None:
        return "missing"
    if not record:
        return None
    if str(record.get("load_state") or "") == "not-found":
        return "missing"
    unit_file_state = str(record.get("unit_file_state") or "")
    if unit_file_state not in {"", "enabled", "enabled-runtime"}:
        return "not_enabled"
    active_state = str(record.get("active_state") or "")
    if active_state == "active":
        return None
    if active_state in {"activating", "reloading"}:
        return "starting"
    return "inactive"


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
            "active_enter_timestamp_monotonic": systemd_int(
                record.get("ActiveEnterTimestampMonotonic")
            ),
        }
    return out


def run_systemctl(
    args: Sequence[str], *, timeout: float | None = SYSTEMCTL_TIMEOUT_SEC,
    executable: str = "systemctl", capture_output: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *args],
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.DEVNULL if quiet else (
            subprocess.PIPE if capture_output else None
        ),
        text=True,
        check=False,
        timeout=timeout,
    )


def unit_uptime_sec(record: Mapping[str, Any] | None) -> float | None:
    """Seconds since a ``read_unit_states`` record's unit last (re)started,
    from its ``active_enter_timestamp_monotonic``. None when the record or
    the timestamp is unavailable.

    ``ActiveEnterTimestampMonotonic`` and ``CLOCK_MONOTONIC`` are the same
    kernel clock, so there is no NTP-skew case to guard against.
    """
    started_us = record.get("active_enter_timestamp_monotonic") if record else None
    if not isinstance(started_us, int) or started_us <= 0:
        return None
    try:
        now_us = time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6
    except OSError:
        return None
    return (now_us - started_us) / 1e6


def _show(args: list[str], timeout: float) -> str | None:
    """``systemctl show`` stdout, or None when systemctl itself is unavailable
    or the call fails, so a caller can say "unknown" rather than "inactive"."""
    try:
        proc = run_systemctl(
            ["show", "--no-page", *args],
            timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode not in (0, 1):
        return None
    return proc.stdout


def read_unit_states(
    units: tuple[str, ...] | list[str],
    *,
    timeout: float = 2.0,
) -> dict[str, dict[str, Any]] | None:
    """One ``systemctl show`` over ``units``, keyed by unit name; None when
    systemctl is unavailable. A unit systemd does not know comes back with
    ``load_state == "not-found"``.

    A non-empty ask that yields NO records is also ``None``: systemctl ran but
    answered nothing (no D-Bus, a host not booted with systemd), which is
    "unknown", not "none of these units exist" — the latter would let a caller
    report every unit as missing on a box where they are all running."""
    if not units:
        return {}
    args = [f"--property={prop}" for prop in SHOW_PROPERTIES]
    args.extend(units)
    out = _show(args, timeout)
    if out is None:
        return None
    return parse_systemctl_show_units(out) or None


def read_unit_property(
    prop: str,
    units: tuple[str, ...] | list[str],
    *,
    timeout: float = 2.0,
) -> list[str] | None:
    """One value of ``prop`` per unit, in input order; None when systemctl is
    unavailable or the reply is not one block per unit.

    The sibling of :func:`read_unit_states` for a property outside
    ``SHOW_PROPERTIES`` (``ExecStart``, ``OOMScoreAdjust``, ``User``, ...).
    One subprocess per property rather than per unit: unbatched, such a
    property would cost N invocations, a large constant-factor loss on the
    Pi."""
    if not units:
        return []
    out = _show([f"--property={prop}", *units], timeout)
    if out is None:
        return None
    values = parse_property_blocks(out, prop)
    if len(values) != len(units):
        return None
    return values
