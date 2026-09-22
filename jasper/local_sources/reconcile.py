# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Converge local sources to household intent through root host operations."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from jasper.accessories.reconcile import request_reconcile
from jasper.atomic_io import advisory_file_lock
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
from jasper.bluetooth.rfkill import BluetoothRfkillState, read_bluetooth_rfkill_state
from jasper.env_load import SOURCE_INTENT_ENV
from jasper.fanin.status import (
    DIRECT_HEALTH_CAPTURING,
    DIRECT_HEALTH_IDLE,
    extract_direct_sample,
    read_fanin_status,
)
from jasper.install_profile import (
    install_profile_allows_local_sources,
    read_install_profile,
)
from jasper.local_sources import local_source_lifecycle
from jasper.local_sources.markers import (
    SHARED_LABEL,
    local_sources_allowed,
    publish_allowed_markers,
)
from jasper.log_event import log_event
from jasper.logging_setup import configure_logging
from jasper.music_sources import Source
from jasper.output_hardware import current_usb_data_role
from jasper.service_units import LIBRESPOT_SERVICE
from jasper.source_intent import (
    SOURCE_STATUS_PATH,
    StatusWriter,
    _intent_fingerprint,
    _parse_source_intents,
    _publish_reconcile_status,
    _read_intent,
    source_intent_sources,
)
from jasper.source_intent_units import (
    RECONCILE_SYSTEMD_TIMEOUT_SECONDS,
    _BLUETOOTH_SERVICE,
    _UNIT_ENABLEMENT_VERBS,
    _UNIT_STATE_QUERY_TIMEOUT_SEC,
    _USB_COUPLING_UNIT,
    _USB_DIRECT_SETTLE_ATTEMPTS,
    _USB_DIRECT_SETTLE_SECONDS,
    _unit_action_timeout_sec,
)
from jasper.systemd_probe import unit_query, unit_state

logger = logging.getLogger(__name__)

SOURCE_RECONCILE_LOCK_TIMEOUT_SECONDS = 5.0
_MUX_UNIT = "jasper-mux.service"
_WORST_CASE_ORDINARY_STOP_ACTIONS = (
    ("shairport-sync.service", "stop"),
    ("nqptp.service", "stop"),
    (LIBRESPOT_SERVICE, "stop"),
    ("bt-agent.service", "stop"),
    ("bluealsa-aplay.service", "stop"),
    ("bluealsa.service", "stop"),
    ("jasper-usbsink.service", "stop"),
    ("jasper-usbgadget.service", "restart"),
)
# The status-invalidating entry point waits out a legitimate in-flight pass
# rather than racing it, so its lock wait outlasts the unit ceiling. It stays
# below the broker bound so the caller still owns the terminal result.
_INVALIDATING_RECONCILE_LOCK_TIMEOUT_SECONDS = RECONCILE_SYSTEMD_TIMEOUT_SECONDS + 5.0
_UAC2_CARD_PATH = "/proc/asound/UAC2Gadget"
_BLUETOOTH_SETTLE_ATTEMPTS = 12
_BLUETOOTH_SETTLE_SECONDS = 0.25
_BLUETOOTH_DBUS_TIMEOUT_SEC = 0.75
_BLUETOOTH_BLUEZ_ATTEMPTS = 3

SystemctlRunner = Callable[[str, bool], tuple[int, str]]
UnitRunner = Callable[[str, str], tuple[int, str]]
UnitProbe = Callable[[str], bool | None]


@dataclass(frozen=True)
class ReconcileOps:
    """Injectable host operations used by the four concrete appliers.

    This is a test seam, not an extension contract.  The root coordinator owns
    every callable and source declarations never receive this object.
    """

    set_enabled: SystemctlRunner
    run_unit: UnitRunner
    unit_enabled: UnitProbe
    unit_active: UnitProbe
    unit_failed: UnitProbe
    local_sources_allowed: Callable[[], bool]
    usb_port_role: Callable[[], UsbPortRoleState]
    usb_audio_present: Callable[[], bool]
    usb_direct_present: Callable[[], bool]
    usb_direct_ready: Callable[[], bool]
    rfkill_state: Callable[[], BluetoothRfkillState]
    set_rfkill_blocked: Callable[[bool], tuple[int, str]]
    bluez_powered: Callable[[], bool | None]
    set_bluez_powered: Callable[[bool], tuple[int, str]]
    settle: Callable[[float], None]
    publish_markers: Callable[[], None]


def _run_systemctl(unit: str, enabled: bool) -> tuple[int, str]:
    return _run_unit_action(unit, "enable" if enabled else "disable")


def _run_unit_action(unit: str, verb: str) -> tuple[int, str]:
    timeout = _unit_action_timeout_sec(unit, verb)
    flags = ["--no-reload"] if verb in _UNIT_ENABLEMENT_VERBS else []
    try:
        process = subprocess.run(
            ["systemctl", verb, *flags, unit],
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    detail = (process.stderr or process.stdout or "").strip()
    return process.returncode, detail


def _query_unit_state(query: str, unit: str) -> bool | None:
    """Tri-state ``systemctl is-active``/``is-enabled``/``is-failed``.

    Classification lives in jasper.systemd_probe (shared with the multiroom
    reconciler's `_systemctl_unit_state`); this wrapper only picks the timeout.
    """
    return unit_query(unit_state(query, unit, timeout=_UNIT_STATE_QUERY_TIMEOUT_SEC))


def _unit_enabled(unit: str) -> bool | None:
    return _query_unit_state("is-enabled", unit)


def _unit_active(unit: str) -> bool | None:
    return _query_unit_state("is-active", unit)


def _unit_failed(unit: str) -> bool | None:
    """Tri-state `systemctl is-failed`: None must make the caller raise
    rather than guess; a not-found unit must never converge a reset-failed."""
    return _query_unit_state("is-failed", unit)


def _local_sources_allowed() -> bool:
    try:
        if not install_profile_allows_local_sources(read_install_profile()):
            return False
        return local_sources_allowed()[0]
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "source.reconcile.role_probe_failed",
            error=str(exc),
            level=logging.WARNING,
        )
        return False


def _usb_audio_present() -> bool:
    try:
        return os.path.isdir(_UAC2_CARD_PATH)
    except OSError:
        return False


def _usb_direct_sample():
    return extract_direct_sample(read_fanin_status())


def _usb_direct_present() -> bool:
    return _usb_direct_sample() is not None


def _usb_direct_ready() -> bool:
    sample = _usb_direct_sample()
    return bool(
        sample is not None
        and sample.present
        and sample.health in {DIRECT_HEALTH_IDLE, DIRECT_HEALTH_CAPTURING}
    )


def _set_bluetooth_rfkill_blocked(blocked: bool) -> tuple[int, str]:
    try:
        process = subprocess.run(
            ["rfkill", "block" if blocked else "unblock", "bluetooth"],
            check=False,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    detail = (process.stderr or process.stdout or "").strip()
    return process.returncode, detail


def _read_bluez_powered() -> bool | None:
    from dbus_next.errors import (
        DBusError,
    )  # lazy: import cost, dbus_next must stay out of the resident daemons (ADR-0226)

    try:
        from jasper.bluetooth.adapter import (
            state,
        )  # lazy: import cost, dbus_next must stay out of the resident daemons (ADR-0226)

        snapshot = asyncio.run(
            asyncio.wait_for(
                state(),
                timeout=_BLUETOOTH_DBUS_TIMEOUT_SEC,
            )
        )
        return bool(snapshot.get("powered", False))
    except (DBusError, OSError, RuntimeError, asyncio.TimeoutError):
        return None


def _set_bluez_powered(enabled: bool) -> tuple[int, str]:
    from dbus_next.errors import (
        DBusError,
    )  # lazy: import cost, dbus_next must stay out of the resident daemons (ADR-0226)

    try:
        from jasper.bluetooth.adapter import (
            set_powered,
        )  # lazy: import cost, dbus_next must stay out of the resident daemons (ADR-0226)

        asyncio.run(
            asyncio.wait_for(
                set_powered(enabled),
                timeout=_BLUETOOTH_DBUS_TIMEOUT_SEC,
            )
        )
    except (DBusError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
        return 1, str(exc)
    return 0, ""


def _publish_markers() -> None:
    verdicts = publish_allowed_markers()
    # mux is the only gated unit no source lifecycle owns, so nothing else
    # re-starts it once its marker reappears. Never stopped here: the gate is
    # start-boundary only. Costs one _DEFAULT_UNIT_ACTION_TIMEOUT_SEC.
    if verdicts[SHARED_LABEL][0]:
        _run_unit_action(_MUX_UNIT, "start")


def default_reconcile_ops() -> ReconcileOps:
    return ReconcileOps(
        set_enabled=_run_systemctl,
        run_unit=_run_unit_action,
        unit_enabled=_unit_enabled,
        unit_active=_unit_active,
        unit_failed=_unit_failed,
        local_sources_allowed=_local_sources_allowed,
        usb_port_role=current_usb_data_role,
        usb_audio_present=_usb_audio_present,
        usb_direct_present=_usb_direct_present,
        usb_direct_ready=_usb_direct_ready,
        rfkill_state=read_bluetooth_rfkill_state,
        set_rfkill_blocked=_set_bluetooth_rfkill_blocked,
        bluez_powered=_read_bluez_powered,
        set_bluez_powered=_set_bluez_powered,
        settle=time.sleep,
        publish_markers=_publish_markers,
    )


def _check_result(rc: int, detail: str, operation: str) -> None:
    if rc != 0:
        raise RuntimeError(f"{operation} failed: {detail or f'rc={rc}'}")


def _ensure_enabled(ops: ReconcileOps, unit: str, desired: bool) -> bool:
    current = ops.unit_enabled(unit)
    if current is desired:
        return False
    rc, detail = ops.set_enabled(unit, desired)
    _check_result(rc, detail, f"systemctl {'enable' if desired else 'disable'} {unit}")
    if ops.unit_enabled(unit) is not desired:
        raise RuntimeError(f"{unit} enablement did not converge to {desired}")
    return True


def _ensure_active(
    ops: ReconcileOps,
    unit: str,
    desired: bool,
    *,
    force: bool = False,
    failed_state_preflighted: bool = False,
) -> bool:
    current = ops.unit_active(unit)
    if current is desired and not force:
        return _reset_failed_if_needed(ops, unit) if not desired else False
    if desired and not failed_state_preflighted:
        _reset_failed_if_needed(ops, unit)
    verb = "start" if desired else "stop"
    rc, detail = ops.run_unit(unit, verb)
    _check_result(rc, detail, f"systemctl {verb} {unit}")
    if ops.unit_active(unit) is not desired:
        raise RuntimeError(f"{unit} active state did not converge to {desired}")
    if not desired:
        _reset_failed_if_needed(ops, unit)
    return True


def _reset_failed_if_needed(ops: ReconcileOps, unit: str) -> bool:
    """Clear a stale failed/start-limit latch around intentional transitions."""

    failed = ops.unit_failed(unit)
    if failed is None:
        raise RuntimeError(f"could not determine whether {unit} is failed")
    if not failed:
        return False
    rc, detail = ops.run_unit(unit, "reset-failed")
    _check_result(rc, detail, f"systemctl reset-failed {unit}")
    if ops.unit_failed(unit) is not False:
        raise RuntimeError(f"{unit} failed state did not reset to inactive")
    return True


def _attempt_teardown(
    errors: list[str],
    operation: str,
    action: Callable[[], object],
) -> None:
    """Run one safe teardown step and retain a bounded error for the caller."""

    from dbus_next.errors import (
        DBusError,
    )  # lazy: import cost, dbus_next must stay out of the resident daemons (ADR-0226)

    try:
        action()
    except (DBusError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        errors.append(f"{operation}: {exc}")


def _reconcile_systemd_source(
    source: Source,
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    lifecycle = local_source_lifecycle(source)
    if lifecycle.intent_unit is None:
        raise RuntimeError(f"{source.value} has no systemd intent unit")
    if lifecycle.intent_unit not in lifecycle.runtime_units:
        raise RuntimeError(f"{source.value} lifecycle declaration is incomplete")
    effective_on = desired and allowed
    # All source-owned runtime units mirror the same persisted intent.  This
    # matters for AirPlay's nqptp companion: leaving it enabled would let boot
    # or a later role-restoration pass revive part of a source the household
    # turned off.
    if effective_on:
        for unit in lifecycle.runtime_units:
            _ensure_enabled(ops, unit, True)
        # Clear every owned latch before the first start. The intent unit may
        # pull companions in through Requires=, so resetting companions only
        # when their explicit verification turn arrives is too late.
        for unit in lifecycle.runtime_units:
            _reset_failed_if_needed(ops, unit)
    else:
        # Off and follower parking are safety transitions. A failed unit-file
        # mutation must not prevent later resources from being stopped; the
        # marker gate also blocks any stale/queued restart.
        teardown_errors: list[str] = []
        for unit in lifecycle.runtime_units:
            _attempt_teardown(
                teardown_errors,
                f"set {unit} enabled={desired}",
                partial(_ensure_enabled, ops, unit, desired),
            )
        for unit in lifecycle.runtime_units:
            _attempt_teardown(
                teardown_errors,
                f"stop {unit}",
                partial(
                    _ensure_active,
                    ops,
                    unit,
                    False,
                    force=False,
                ),
            )
        if teardown_errors:
            raise RuntimeError("; ".join(teardown_errors))
        return "parked" if desired else "off"

    # Stop the main/intent unit first so it releases its companions cleanly;
    # start it first so systemd's Requires=/After= graph establishes them in
    # the package-declared order.  Explicit checks then verify every resource.
    _ensure_active(
        ops,
        lifecycle.intent_unit,
        effective_on,
        force=False,
        failed_state_preflighted=True,
    )
    for unit in lifecycle.runtime_units:
        if unit != lifecycle.intent_unit:
            _ensure_active(
                ops,
                unit,
                effective_on,
                force=False,
                failed_state_preflighted=True,
            )
    return "on"


def _reconcile_usbsink(
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    lifecycle = local_source_lifecycle(Source.USBSINK)
    unit = lifecycle.intent_unit
    if unit is None or len(lifecycle.advertise_units) != 1:
        raise RuntimeError("USB lifecycle declaration is incomplete")
    gadget = lifecycle.advertise_units[0]
    usb_role = ops.usb_port_role()

    # Hardware unavailability is orthogonal to follower parking. Preserve
    # household intent, but withdraw every derived USB-audio resource in all
    # roles. During a host-role change whose current controller is still
    # peripheral, keep the management transport alive until reboot so a deploy
    # running over NCM cannot sever itself. Stable host/unsupported states stop
    # the composite owner completely.
    if not usb_role.gadget_available:
        hardware_teardown_errors: list[str] = []
        _attempt_teardown(
            hardware_teardown_errors,
            f"disable {unit} while USB gadget unavailable",
            lambda: _ensure_enabled(ops, unit, False),
        )
        _attempt_teardown(
            hardware_teardown_errors,
            f"stop {unit} while USB gadget unavailable",
            lambda: _ensure_active(ops, unit, False, force=False),
        )
        if usb_role.management_transport_available:
            if ops.usb_audio_present():
                _attempt_teardown(
                    hardware_teardown_errors,
                    f"recompose {gadget} to management-only while role changes",
                    lambda: _check_result(
                        *ops.run_unit(gadget, "restart"),
                        f"systemctl restart {gadget}",
                    ),
                )
        else:
            _attempt_teardown(
                hardware_teardown_errors,
                f"stop {gadget} while USB gadget unavailable",
                lambda: _ensure_active(ops, gadget, False, force=False),
            )
        if ops.usb_audio_present():
            hardware_teardown_errors.append(
                "USB audio function remained after gadget withdrawal"
            )
        else:
            _attempt_teardown(
                hardware_teardown_errors,
                f"start {_USB_COUPLING_UNIT}",
                lambda: _check_result(
                    *ops.run_unit(_USB_COUPLING_UNIT, "start"),
                    f"systemctl start {_USB_COUPLING_UNIT}",
                ),
            )
        if hardware_teardown_errors:
            raise RuntimeError("; ".join(hardware_teardown_errors))
        if not desired:
            return "off"
        return "unavailable" if allowed else "parked"

    effective_on = desired and allowed

    if effective_on:
        try:
            # Enablement is written before composition because gadget-up uses
            # it as a derived readiness mirror in addition to canonical intent.
            _ensure_enabled(ops, unit, True)
            # Arm fan-in's direct lane while UAC2 is still absent. The lane's
            # bounded reopen loop can wait for the card; this guarantees a
            # consumer is already standing by before the gadget advertises the
            # host-visible endpoint.
            if ops.usb_audio_present() and not ops.usb_direct_present():
                # Repair an unsafe pre-coordinator/old-boot state first. The
                # gadget's three-way gate now suppresses UAC2 while the live
                # direct lane is absent, so this restart withdraws audio but
                # preserves the NCM management link.
                rc, detail = ops.run_unit(gadget, "restart")
                _check_result(rc, detail, f"systemctl restart {gadget}")
                if ops.usb_audio_present():
                    raise RuntimeError(
                        "USB audio remained advertised without a direct consumer"
                    )
            if not ops.usb_direct_present():
                # NOT _check_result: this unit also runs an opportunistic
                # CamillaDSP self-heal, so its exit status is not evidence
                # about USB transport. The lane arming IS verified — by the
                # checks below and the usb_direct_ready() settle loop, which
                # still fail this transition when the lane does not arm.
                # See ADR-0191.
                ops.run_unit(_USB_COUPLING_UNIT, "start")
            if not ops.usb_audio_present():
                rc, detail = ops.run_unit(gadget, "restart")
                _check_result(rc, detail, f"systemctl restart {gadget}")
            _ensure_active(ops, unit, True)
            if not ops.usb_audio_present():
                raise RuntimeError("USB audio function did not appear after recompose")
            for attempt in range(_USB_DIRECT_SETTLE_ATTEMPTS):
                if ops.usb_direct_ready():
                    break
                if attempt + 1 < _USB_DIRECT_SETTLE_ATTEMPTS:
                    ops.settle(_USB_DIRECT_SETTLE_SECONDS)
            else:
                raise RuntimeError(
                    "fan-in direct USB capture lane did not become ready"
                )
            return "on"
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            # No teardown: a failed On leaves the endpoint composed. An
            # unconsumed UAC2 is a disclosed state, not a reason to withdraw
            # the transport, and the rollback this replaces stopped the whole
            # composite gadget — taking the NCM management network with it.
            # Canonical intent still says On, so the next pass retries.
            # See ADR-0191. Off and follower parking below keep their
            # teardowns: those ARE safety transitions.
            raise RuntimeError(f"USB On transition failed: {exc}") from exc

    # Off and follower parking are safety transitions. Keep going through
    # stop, NCM-only recompose, and coupling disarm even if the derived
    # enablement mirror cannot be repaired.
    teardown_errors: list[str] = []

    _attempt_teardown(
        teardown_errors,
        f"set {unit} enabled={desired}",
        lambda: _ensure_enabled(ops, unit, desired),
    )
    _attempt_teardown(
        teardown_errors,
        f"stop {unit}",
        lambda: _ensure_active(ops, unit, False, force=False),
    )
    if ops.usb_audio_present():
        _attempt_teardown(
            teardown_errors,
            f"recompose {gadget}",
            lambda: _check_result(
                *ops.run_unit(gadget, "restart"),
                f"systemctl restart {gadget}",
            ),
        )
    if ops.usb_audio_present():
        _attempt_teardown(
            teardown_errors,
            f"stop {gadget} after failed UAC2 withdrawal",
            lambda: _check_result(
                *ops.run_unit(gadget, "stop"),
                f"systemctl stop {gadget}",
            ),
        )
    audio_withdrawn = not ops.usb_audio_present()
    if not audio_withdrawn:
        teardown_errors.append(
            "USB audio function remained after recompose; direct capture left armed"
        )
    # Always converge the persisted fan-in decision, even when the derived
    # unit is already Off and the live fan-in probe is absent. Otherwise stale
    # JASPER_FANIN_USB_DIRECT=enabled can survive a down daemon and re-arm on a
    # later start despite canonical Off/follower parking.
    if audio_withdrawn:
        _attempt_teardown(
            teardown_errors,
            f"start {_USB_COUPLING_UNIT}",
            lambda: _check_result(
                *ops.run_unit(_USB_COUPLING_UNIT, "start"),
                f"systemctl start {_USB_COUPLING_UNIT}",
            ),
        )
    if teardown_errors:
        raise RuntimeError("; ".join(teardown_errors))
    return "parked" if desired else "off"


def _wait_for_bluetooth_radio(
    ops: ReconcileOps,
    *,
    required: bool = True,
) -> BluetoothRfkillState:
    """Wait briefly for Pi firmware/kernel RF-kill registration."""

    state = ops.rfkill_state()
    for attempt in range(_BLUETOOTH_BLUEZ_ATTEMPTS):
        if state.present:
            return state
        if attempt + 1 < _BLUETOOTH_BLUEZ_ATTEMPTS:
            ops.settle(_BLUETOOTH_SETTLE_SECONDS)
            state = ops.rfkill_state()
    if required:
        raise RuntimeError("Bluetooth radio did not appear before the settle deadline")
    return state


def _rfkill_converge(ops: ReconcileOps, blocked: bool) -> bool:
    state = _wait_for_bluetooth_radio(ops)
    if not blocked and state.hard_blocked:
        raise RuntimeError("Bluetooth radio is hardware-blocked")
    converged = state.fully_soft_blocked if blocked else not state.soft_blocked
    if converged:
        return False
    rc, detail = ops.set_rfkill_blocked(blocked)
    _check_result(
        rc,
        detail,
        f"rfkill {'block' if blocked else 'unblock'} bluetooth",
    )
    state = _wait_for_bluetooth_radio(ops)
    converged = state.fully_soft_blocked if blocked else not state.soft_blocked
    if not converged:
        raise RuntimeError(f"Bluetooth RF-kill did not converge to blocked={blocked}")
    if not blocked and state.hard_blocked:
        raise RuntimeError("Bluetooth radio is hardware-blocked")
    return True


def _bluez_power_converge(ops: ReconcileOps, desired: bool) -> bool:
    """Retry the bounded Adapter1 transition while hci settles after RF-kill."""

    detail = "BlueZ adapter unavailable"
    changed = False
    for attempt in range(_BLUETOOTH_SETTLE_ATTEMPTS):
        if ops.bluez_powered() is desired:
            return changed
        rc, attempt_detail = ops.set_bluez_powered(desired)
        changed = True
        if rc != 0:
            detail = attempt_detail or f"rc={rc}"
        if attempt + 1 < _BLUETOOTH_SETTLE_ATTEMPTS:
            ops.settle(_BLUETOOTH_SETTLE_SECONDS)
    if ops.bluez_powered() is desired:
        return changed
    raise RuntimeError(
        f"BlueZ Powered did not converge to {str(desired).lower()}: {detail}"
    )


def _reconcile_bluetooth(
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    lifecycle = local_source_lifecycle(Source.BLUETOOTH)
    effective_on = desired and allowed
    start_order = (
        "bluealsa.service",
        "bluealsa-aplay.service",
        "bt-agent.service",
    )
    stop_order = tuple(reversed(start_order))
    if set(start_order) != set(lifecycle.runtime_units):
        raise RuntimeError("Bluetooth lifecycle declaration is incomplete")

    # systemd enablement is derived state. Mirroring household intent onto all
    # three units preserves boot state; role park/restore still comes back
    # through this same coordinator rather than teaching grouping Bluetooth.
    teardown_errors: list[str] = []

    if effective_on:
        for unit in start_order:
            _ensure_enabled(ops, unit, True)
    else:
        for unit in start_order:
            _attempt_teardown(
                teardown_errors,
                f"set {unit} enabled={desired}",
                partial(_ensure_enabled, ops, unit, desired),
            )

    def reconcile_accessories() -> None:
        # Optional Bluetooth accessories own their own adapter-unit registry.
        # Request a fresh pass; the owner's freshness barrier is the request
        # file it claims — see jasper.accessories.reconcile.request_reconcile.
        request_reconcile("source-intent")

    if desired and not allowed:
        # Parking suppresses local playback/advertising, not the shared radio.
        # In particular, do not create a new RF-kill block that grouping's
        # ordinary start-if-enabled restore has no authority to clear.
        for unit in stop_order:
            _attempt_teardown(
                teardown_errors,
                f"stop {unit}",
                partial(
                    _ensure_active,
                    ops,
                    unit,
                    False,
                    force=False,
                ),
            )
        _attempt_teardown(
            teardown_errors,
            "reconcile Bluetooth accessories",
            reconcile_accessories,
        )
        if teardown_errors:
            raise RuntimeError("; ".join(teardown_errors))
        return "parked"

    if effective_on:
        _ensure_active(ops, _BLUETOOTH_SERVICE, True)
        _wait_for_bluetooth_radio(ops)
        _rfkill_converge(ops, False)
        _bluez_power_converge(ops, True)
        for unit in start_order:
            _ensure_active(ops, unit, True)
        reconcile_accessories()
        return "on"

    # Off is fail-closed: attempt every safe teardown step even if an earlier
    # service refuses to stop. RF-kill is the strongest final guard, so a
    # bluealsa or BlueZ failure must never prevent that attempt.
    for unit in stop_order:
        _attempt_teardown(
            teardown_errors,
            f"stop {unit}",
            partial(
                _ensure_active,
                ops,
                unit,
                False,
                force=False,
            ),
        )
    _attempt_teardown(
        teardown_errors,
        f"start {_BLUETOOTH_SERVICE} control plane",
        lambda: _ensure_active(ops, _BLUETOOTH_SERVICE, True),
    )
    radio_state: BluetoothRfkillState | None = None
    try:
        # A conclusively absent adapter is already safe-Off. Read/probe errors
        # still fail loudly, but absence itself must not make a household Off
        # request fail after all source-owned services were torn down.
        radio_state = _wait_for_bluetooth_radio(ops, required=False)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        teardown_errors.append(f"wait for Bluetooth radio: {exc}")
    if radio_state is not None and radio_state.present:
        _attempt_teardown(
            teardown_errors,
            "set BlueZ Powered=false",
            lambda: _bluez_power_converge(ops, False),
        )
        _attempt_teardown(
            teardown_errors,
            "RF-kill Bluetooth",
            lambda: _rfkill_converge(ops, True),
        )
    _attempt_teardown(
        teardown_errors,
        "reconcile Bluetooth accessories",
        reconcile_accessories,
    )
    if teardown_errors:
        raise RuntimeError("; ".join(teardown_errors))
    return "off"


def _apply_source(
    source: Source,
    desired: bool,
    allowed: bool,
    ops: ReconcileOps,
) -> str:
    if source == Source.USBSINK:
        return _reconcile_usbsink(desired, allowed, ops)
    if source == Source.BLUETOOTH:
        return _reconcile_bluetooth(desired, allowed, ops)
    # Ordinary sources are selected by their lifecycle declaration, not a
    # second central enum set.  This is deliberately only a dispatch rule—not
    # a plugin API: USB and Bluetooth keep their concrete ordered appliers,
    # while any declared source with one intent unit uses the common systemd
    # mechanism without another coordinator edit.
    if local_source_lifecycle(source).intent_unit is not None:
        return _reconcile_systemd_source(source, desired, allowed, ops)
    raise RuntimeError(f"unsupported source {source.value}")


def _usbsink_applies_first(source: Source) -> bool:
    """Sort key: USBSINK converges before the rest, in their registry order.

    The gadget's On path recomposes usb0 mid-boot; avahi 0.8 drops the
    SRV/TXT records of any mDNS advert registered less than ~1 s before an
    interface disappears. Applying the sink first lets usb0 settle before
    shairport-sync/librespot register their adverts.
    """
    return source is not Source.USBSINK


def _reconcile_once(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    ops: ReconcileOps | None = None,
    status_path: str | None = None,
    status_writer: StatusWriter | None = None,
) -> int:
    """Converge every declared source while the process lock is held.

    The operation bundle is the only injection seam.  Full convergence always
    handles both persistent enablement and runtime state, so there is no
    separate deploy-only stop mode.
    """

    from dbus_next.errors import (
        DBusError,
    )  # lazy: import cost, dbus_next must stay out of the resident daemons (ADR-0226)

    operations = ops or default_reconcile_ops()

    try:
        text = _read_intent(env_path)
    except RuntimeError as exc:
        log_event(
            logger,
            "source_intent.read_failed",
            error=str(exc),
            level=logging.WARNING,
        )
        failure_outcomes = {
            source.value: {
                "desired": "unknown",
                "effective": "degraded",
                "result": "failed",
                "reason": str(exc)[:300],
            }
            for source in source_intent_sources()
        }
        _publish_reconcile_status(
            path=status_path,
            intent_fingerprint="",
            outcomes=failure_outcomes,
            writer=status_writer,
        )
        return 1

    fingerprint = _intent_fingerprint(text)
    intents, problems = _parse_source_intents(text)
    failures = len(problems)
    outcomes: dict[str, dict[str, str]] = {}
    invalid_sources = {
        problem.source
        for problem in problems
        if problem.event == "source_intent.bad_value" and problem.source is not None
    }
    for problem in problems:
        fields: dict[str, Any] = {}
        if problem.source is not None:
            fields["source"] = problem.source.value
        if problem.key:
            fields["key"] = problem.key
        if problem.value:
            fields["value"] = problem.value
        log_event(logger, problem.event, level=logging.WARNING, **fields)

    allowed = operations.local_sources_allowed()
    try:
        operations.publish_markers()
    except OSError as exc:
        # An unpublishable marker leaves the previous verdict standing; every
        # source still converges below and the pass still reports.
        failures += 1
        log_event(
            logger,
            "local_sources.marker_publish_failed",
            error=str(exc)[:300],
            level=logging.WARNING,
        )
    applied = 0
    for source in sorted(source_intent_sources(), key=_usbsink_applies_first):
        if source not in intents:
            continue
        desired = intents[source]
        desired_label = (
            "invalid"
            if source in invalid_sources
            else "enabled"
            if desired
            else "disabled"
        )
        try:
            effective = _apply_source(source, desired, allowed, operations)
        except (
            DBusError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as exc:
            failures += 1
            failure_reason = str(exc)[:300]
            outcomes[source.value] = {
                "desired": desired_label,
                "effective": "degraded",
                "result": "failed",
                "reason": failure_reason,
            }
            log_event(
                logger,
                "source.reconcile",
                source=source.value,
                desired=desired_label,
                effective="degraded",
                result="failed",
                reason=failure_reason,
                level=logging.WARNING,
            )
            continue
        applied += 1
        if source in invalid_sources:
            outcomes[source.value] = {
                "desired": "invalid",
                "effective": effective,
                "result": "failed",
                "reason": "invalid_intent_fail_closed",
            }
            log_event(
                logger,
                "source.reconcile",
                source=source.value,
                desired="invalid",
                effective=effective,
                result="failed",
                reason="invalid_intent_fail_closed",
                level=logging.WARNING,
            )
        else:
            success_reason = ""
            if source == Source.USBSINK and effective == "unavailable":
                success_reason = operations.usb_port_role().reason
            outcomes[source.value] = {
                "desired": desired_label,
                "effective": effective,
                "result": "ok",
                "reason": success_reason,
            }
            log_event(
                logger,
                "source.reconcile",
                source=source.value,
                desired=desired_label,
                effective=effective,
                result="ok",
                reason=success_reason,
            )

    if not _publish_reconcile_status(
        path=status_path,
        intent_fingerprint=fingerprint,
        outcomes=outcomes,
        writer=status_writer,
    ):
        failures += 1
    log_event(
        logger,
        "source_intent.reconciled",
        applied=applied,
        failures=failures,
    )
    return 1 if failures else 0


def _invalidate_reconcile_status(path: str) -> bool:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log_event(
            logger,
            "source_intent.status_invalidation_failed",
            path=path,
            error=str(exc),
            level=logging.ERROR,
        )
        return False
    return True


def reconcile(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    ops: ReconcileOps | None = None,
    status_path: str | None = None,
    status_writer: StatusWriter | None = None,
    invalidate_status_before: bool = False,
) -> int:
    """Serialize and converge every source to the latest persisted intent.

    systemd, boot, deploy, and direct operator invocations all share this lock.
    The intent read happens after acquisition, so two coordinator processes can
    never apply opposite snapshots concurrently.
    """

    try:
        with source_reconcile_lock(
            env_path=env_path,
            timeout_sec=(
                _INVALIDATING_RECONCILE_LOCK_TIMEOUT_SECONDS
                if invalidate_status_before
                else SOURCE_RECONCILE_LOCK_TIMEOUT_SECONDS
            ),
        ):
            if (
                invalidate_status_before
                and status_path is not None
                and not _invalidate_reconcile_status(status_path)
            ):
                return 1
            return _reconcile_once(
                env_path=env_path,
                ops=ops,
                status_path=status_path,
                status_writer=status_writer,
            )
    except TimeoutError as exc:
        if invalidate_status_before and status_path is not None:
            _invalidate_reconcile_status(status_path)
        log_event(
            logger,
            "source_intent.lock_timeout",
            error=str(exc),
            level=logging.WARNING,
        )
        return 1


def source_reconcile_lock(
    *,
    env_path: str = SOURCE_INTENT_ENV,
    timeout_sec: float = SOURCE_RECONCILE_LOCK_TIMEOUT_SECONDS,
):
    """Return the shared source-lifecycle reconcile lock context.

    Cross-subsystem callers that must compose source lifecycle work acquire this
    lock first. The ordinary source reconcile holds it while invoking the
    coupling owner, preserving the global ``source -> coupling`` order.
    """

    return advisory_file_lock(
        f"{env_path}.reconcile.lock",
        timeout_sec=timeout_sec,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-source-intent-reconcile",
        description="Converge local music sources to persisted household intent.",
    )
    parser.add_argument("--env-path", default=SOURCE_INTENT_ENV)
    parser.add_argument("--status-path", default=SOURCE_STATUS_PATH)
    parser.add_argument("--reason", default="")
    parser.add_argument(
        "--invalidate-status-before",
        action="store_true",
        help="remove the prior acknowledgement after acquiring the reconcile lock",
    )
    args = parser.parse_args(argv)
    configure_logging()
    if args.reason:
        log_event(logger, "source_intent.begin", reason=args.reason)
    try:
        Path(args.status_path).parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    except OSError as exc:
        log_event(
            logger,
            "source_intent.status_dir_failed",
            path=str(Path(args.status_path).parent),
            error=str(exc),
            level=logging.ERROR,
        )
        return 1
    return reconcile(
        env_path=args.env_path,
        status_path=args.status_path,
        invalidate_status_before=args.invalidate_status_before,
    )


if __name__ == "__main__":
    raise SystemExit(main())
