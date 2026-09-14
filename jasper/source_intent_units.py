# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Source-intent reconcile unit name and timeout budget — a stdlib-only leaf.

``jasper.source_intent`` imports asyncio (for its Bluetooth D-Bus calls),
``jasper.install_profile``, and ``jasper.local_sources`` for its full
reconcile machinery. A boot oneshot that only needs the reconcile unit name
and its derived systemd/broker timeout ceilings
(``jasper.multiroom.reconcile``, ``jasper.control.restart_broker``) should
not pay import cost for that whole tree — that is the reason this is a
separate module rather than a re-export. ``jasper.source_intent`` re-imports
every name below so existing importers keep working unchanged.
"""
from __future__ import annotations

from jasper.service_units import FANIN_SERVICE, LIBRESPOT_SERVICE

RECONCILE_UNIT = "jasper-source-intent-reconcile.service"

_RESET_FAILED_ACTION_TIMEOUT_SEC = 5.0
_USB_DIRECT_SETTLE_ATTEMPTS = 20
_USB_DIRECT_SETTLE_SECONDS = 0.25

_BLUETOOTH_SERVICE = "bluetooth.service"
_ACCESSORY_RECONCILE_UNIT = "jasper-accessory-reconcile.service"
_USB_COUPLING_UNIT = "jasper-fanin-coupling-auto.service"
# Every blocking source-unit action has two finite layers: the systemd unit's
# explicit TimeoutStartSec/TimeoutStopSec contract, then this client's slightly
# longer subprocess bound.  A client must never report timeout while PID 1 may
# still legally be running the same job.  ``restart`` can consume both service
# ceilings, so its client bound covers their sum.  Simple services keep small
# start ceilings; AirPlay has a real pre-start renderer and USB may spend 30 s
# in its wait-card ExecStartPre.
_DEFAULT_UNIT_ACTION_TIMEOUT_SEC = 15.0
_UNIT_ENABLEMENT_ACTION_TIMEOUT_SEC = 5.0
# The two verbs that only move symlinks, and the only two `--no-reload`
# accepts.  Nothing here writes a unit file or drop-in first, and on systemd
# 257 both readers of an enablement — `systemctl is-enabled` below, and the
# cached `UnitFileState` property jasper/accessories/reconcile.py shows — read
# a `--no-reload` enable or disable back immediately.  So the implicit
# daemon-reload buys nothing, and it cost 3.6-14.9 s per call under the memory
# pressure of #3639.  Removal condition: a caller here writes a unit file.
_UNIT_ENABLEMENT_VERBS = frozenset({"enable", "disable"})
_UNIT_STATE_QUERY_TIMEOUT_SEC = 2.0
_UNIT_ACTION_CLIENT_MARGIN_SEC = 1.0
_SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC: dict[str, tuple[float, float]] = {
    # unit: (TimeoutStartSec, TimeoutStopSec)
    "shairport-sync.service": (30.0, 5.0),
    "nqptp.service": (2.0, 5.0),
    LIBRESPOT_SERVICE: (2.0, 5.0),
    "bluealsa.service": (5.0, 5.0),
    "bluealsa-aplay.service": (2.0, 5.0),
    "bt-agent.service": (2.0, 10.0),
    "jasper-usbgadget.service": (5.0, 5.0),
    "jasper-usbsink.service": (40.0, 5.0),
    "jasper-usbsink-volume.service": (2.0, 5.0),
}
_CONTROL_UNIT_SYSTEMD_TIMEOUT_SEC: dict[str, tuple[float, float]] = {
    _BLUETOOTH_SERVICE: (10.0, 10.0),
}
# A unit's declared TimeoutStartSec is not always the bound PID 1 enforces on
# the whole start phase. Measured on jts4 across all 13 gadget starts since its
# 2026-08-17 boot: every one ran past the declared TimeoutStartSec=5s without
# being terminated (6.48 s to 25.51 s), with no drop-in and `systemctl show`
# still reporting TimeoutStartUSec=5s. systemd 257's systemd.service(5)
# describes TimeoutStartSec= only as "the time to wait for start-up" and does
# not state how it applies to a Type=oneshot running several Exec* commands, so
# the mechanism is not quotable from the shipped documentation; what the
# measurements track is one declared ceiling per command. Model it per command:
# that is the conservative reading whichever way PID 1 actually arms the timer,
# and a command-count contract test forces a re-derivation when a command is
# added or removed. Units absent here keep their declared ceiling: the gadget is
# the only one whose phases have been measured, and the only one that has
# produced a false client timeout, so widening the model past it would be
# guessing rather than reading a measurement.
_UNIT_PHASE_COMMAND_COUNTS: dict[str, tuple[int, int]] = {
    # unit: (start-phase Exec* commands, stop-phase Exec* commands)
    # ExecCondition + 2 ExecStartPre + ExecStart + ExecStartPost; 2 ExecStop +
    # 5 ExecStopPost.
    "jasper-usbgadget.service": (5, 7),
}
# The manager's DefaultTimeoutStartSec, which governs a pulled dependency whose
# own unit declares no TimeoutStartSec= override.
_SYSTEMD_DEFAULT_TIMEOUT_START_SEC = 90.0
_FANIN_RESTART_BACKOFF_SEC = 5.0
# jasper-usbgadget.service orders its start half After= three units it also
# pulls with Requires=/Wants=, so PID 1 can legally hold a gadget start until
# all three report terminal, before the gadget's own start phase begins.
# jasper-audio-hardware-reconcile is a Type=oneshot with RemainAfterExit=no, so
# it is inactive between runs and every gadget start re-queues it. jasper-fanin
# declares no start override and so takes the manager default, plus its
# RestartSec when it is in restart backoff — reachable, because the failed-On
# rollback recomposes the gadget on the same path the coupling owner restarts
# fan-in on. Summed rather than maxed: the three are mutually unordered and
# normally start concurrently, so this is a ceiling, not an expected wait.
_USB_GADGET_START_DEPENDENCY_SEC: dict[str, float] = {
    "jasper-usb-network-plan.service": 10.0,
    "jasper-audio-hardware-reconcile.service": 50.0,
    FANIN_SERVICE: (
        _SYSTEMD_DEFAULT_TIMEOUT_START_SEC + _FANIN_RESTART_BACKOFF_SEC
    ),
}
# A synchronous start waits for the whole required dependency transaction, not
# just the named service. AirPlay's packaged unit Requires=/After= our nqptp
# timing service, so a cold start legally consumes both start ceilings. The USB
# gadget pays the same way for the three units above.
_SOURCE_UNIT_START_DEPENDENCY_TIMEOUT_SEC: dict[str, float] = {
    "shairport-sync.service": _SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC["nqptp.service"][0],
    "jasper-usbsink.service": _SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC[
        "jasper-usbsink-volume.service"
    ][0],
    "jasper-usbgadget.service": sum(_USB_GADGET_START_DEPENDENCY_SEC.values()),
}
# Owner oneshots are different: a synchronous ``systemctl start`` may join and
# wait for their full Type=oneshot activation. USB starts coupling once.
# Bluetooth no longer starts the accessory owner at all — it publishes a
# request file (jasper.accessories.reconcile.request_reconcile) — so that entry
# and the two accessory barriers below are unspent headroom in the budget, not
# a bound this coordinator can reach. Their 5-second margin includes
# broker/client overhead.
# Each value mirrors its target's shipped TimeoutStartSec and is pinned to it by
# tests/test_source_intent_systemd.py: the coupling entry read 125.0 against a
# target raised to 210 s in #2651, so a client could report timeout with 85 s of
# the owner's activation still legally left to run. That target is now derived
# (jasper.fanin.coupling_reconcile.COUPLING_AUTO_TIMEOUT_START_SEC) from the
# pass it actually has to outlast.
_OWNER_UNIT_ACTION_TIMEOUT_SEC = {
    _ACCESSORY_RECONCILE_UNIT: 65.0,  # target TimeoutStartSec=60
    _USB_COUPLING_UNIT: 772.0,  # target TimeoutStartSec=767
}


def _unit_action_timeout_sec(unit: str, verb: str) -> float:
    if verb == "start" and unit in _OWNER_UNIT_ACTION_TIMEOUT_SEC:
        return _OWNER_UNIT_ACTION_TIMEOUT_SEC[unit]
    if verb in _UNIT_ENABLEMENT_VERBS:
        return _UNIT_ENABLEMENT_ACTION_TIMEOUT_SEC
    if verb == "reset-failed":
        return _RESET_FAILED_ACTION_TIMEOUT_SEC
    bounds = _SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC.get(
        unit
    ) or _CONTROL_UNIT_SYSTEMD_TIMEOUT_SEC.get(unit)
    if bounds is None or verb not in {"start", "stop", "restart"}:
        return _DEFAULT_UNIT_ACTION_TIMEOUT_SEC
    declared_start, declared_stop = bounds
    start_commands, stop_commands = _UNIT_PHASE_COMMAND_COUNTS.get(unit, (1, 1))
    start_timeout = declared_start * start_commands
    stop_timeout = declared_stop * stop_commands
    dependency_timeout = _SOURCE_UNIT_START_DEPENDENCY_TIMEOUT_SEC.get(unit, 0.0)
    service_timeout = {
        "start": start_timeout + dependency_timeout,
        "stop": stop_timeout,
        "restart": start_timeout + stop_timeout + dependency_timeout,
    }[verb]
    return service_timeout + _UNIT_ACTION_CLIENT_MARGIN_SEC


# A maximally cold On pass can block once on AirPlay's main unit (its Requires=
# transaction brings nqptp up), Spotify, the Bluetooth control plane plus three
# runtime units, a USB gadget recompose, and USB standby start. The complete
# outer budget below also includes every enablement pre/action/post sequence,
# state-probe overhead, one coupling owner barrier (plus retained accessory
# headroom — see _OWNER_UNIT_ACTION_TIMEOUT_SEC), bounded BlueZ/RF-kill work,
# direct-lane settling, and failed-USB rollback.
_WORST_CASE_ORDINARY_START_ACTIONS = (
    ("shairport-sync.service", "start"),
    (LIBRESPOT_SERVICE, "start"),
    (_BLUETOOTH_SERVICE, "start"),
    ("bluealsa.service", "start"),
    ("bluealsa-aplay.service", "start"),
    ("bt-agent.service", "start"),
    ("jasper-usbgadget.service", "restart"),
    ("jasper-usbsink.service", "start"),
)
_NON_SYSTEMD_RECONCILE_BUDGET_SEC = 15.0
_MAX_ENABLEMENT_TRANSITIONS = 7
_MAX_ENSURE_ACTIVE_TRANSITIONS = len(_WORST_CASE_ORDINARY_START_ACTIONS)
# Ordinary/Bluetooth appliers converge every runtime unit. USB converges only
# its intent unit through _ensure_active; gadget/volume are ordered
# dependencies. Equals sum(1 if lifecycle.source == Source.USBSINK else
# len(lifecycle.runtime_units) for lifecycle in local_source_lifecycles())
# over the fixed 4-source registry (airplay 2, spotify 1, bluetooth 3, usbsink
# 1) — frozen rather than computed so this leaf stays stdlib-only (no
# jasper.local_sources/jasper.music_sources import). A registry change that
# moves this number is caught by
# test_source_intent_systemd.py::test_max_failed_reset_transitions_matches_local_source_registry.
_MAX_FAILED_RESET_TRANSITIONS = 7
_ENABLEMENT_TRANSITION_BUDGET_SEC = (
    2 * _UNIT_STATE_QUERY_TIMEOUT_SEC + _UNIT_ENABLEMENT_ACTION_TIMEOUT_SEC
)
_FAILED_RESET_BUDGET_SEC = _MAX_FAILED_RESET_TRANSITIONS * (
    2 * _UNIT_STATE_QUERY_TIMEOUT_SEC
    + _unit_action_timeout_sec("source.service", "reset-failed")
)
_ACTIVE_TRANSITION_BUDGET_SEC = sum(
    _unit_action_timeout_sec(unit, verb)
    for unit, verb in _WORST_CASE_ORDINARY_START_ACTIONS
) + (2 * _UNIT_STATE_QUERY_TIMEOUT_SEC * _MAX_ENSURE_ACTIVE_TRANSITIONS)
_OWNER_RECONCILE_BUDGET_SEC = (
    2 * _OWNER_UNIT_ACTION_TIMEOUT_SEC[_ACCESSORY_RECONCILE_UNIT]
    + _OWNER_UNIT_ACTION_TIMEOUT_SEC[_USB_COUPLING_UNIT]
)
_BLUETOOTH_CONTROL_BUDGET_SEC = 30.0
_USB_DIRECT_WAIT_BUDGET_SEC = (
    _USB_DIRECT_SETTLE_ATTEMPTS * 0.5
    + (_USB_DIRECT_SETTLE_ATTEMPTS - 1) * _USB_DIRECT_SETTLE_SECONDS
)
# The failed-On rollback, enumerated in the call order the except-branch of the
# USB applier runs them: stop the derived unit through _ensure_active (an active
# probe either side of the stop action, then one failed-state reset sequence),
# disable it through _ensure_enabled (an enablement probe either side of the
# disable action), recompose the gadget to NCM-only, stop the gadget when audio
# survived that recompose, and start the coupling owner to disarm the direct
# lane. The live-state probes between those steps are not systemd waits and are
# carried by the non-systemd budget instead.
_USB_FAILED_ON_CLEANUP_BUDGET_SEC = (
    2 * _UNIT_STATE_QUERY_TIMEOUT_SEC
    + _unit_action_timeout_sec("jasper-usbsink.service", "stop")
    + (2 * _UNIT_STATE_QUERY_TIMEOUT_SEC + _RESET_FAILED_ACTION_TIMEOUT_SEC)
    + _ENABLEMENT_TRANSITION_BUDGET_SEC
    + _unit_action_timeout_sec("jasper-usbgadget.service", "restart")
    + _unit_action_timeout_sec("jasper-usbgadget.service", "stop")
    + _OWNER_UNIT_ACTION_TIMEOUT_SEC[_USB_COUPLING_UNIT]
)
_RECONCILE_TIMEOUT_MARGIN_SEC = 21.25
_NON_OWNER_RECONCILE_BUDGET_SEC = (
    _NON_SYSTEMD_RECONCILE_BUDGET_SEC
    + _MAX_ENABLEMENT_TRANSITIONS * _ENABLEMENT_TRANSITION_BUDGET_SEC
    + _FAILED_RESET_BUDGET_SEC
    + _ACTIVE_TRANSITION_BUDGET_SEC
    + _unit_action_timeout_sec("jasper-usbgadget.service", "restart")
    + _BLUETOOTH_CONTROL_BUDGET_SEC
    + _USB_DIRECT_WAIT_BUDGET_SEC
    + _USB_FAILED_ON_CLEANUP_BUDGET_SEC
)
RECONCILE_SYSTEMD_TIMEOUT_SECONDS = (
    _NON_OWNER_RECONCILE_BUDGET_SEC
    + _OWNER_RECONCILE_BUDGET_SEC
    + _RECONCILE_TIMEOUT_MARGIN_SEC
)
RECONCILE_BROKER_TIMEOUT_SECONDS = RECONCILE_SYSTEMD_TIMEOUT_SECONDS + 10.0
