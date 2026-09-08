# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single owner of local-source availability/enabled/effective status.

Everything that answers "is this source installed, allowed by the current
install profile, effectively on, or blocked from turning on" for the four
local music sources (AirPlay, Bluetooth, Spotify Connect, USB Audio Input)
lives here. The /sources/ wizard (``jasper.web.sources_setup``) and
jasper-control's mux-status augmenter (``jasper.control.handlers.volume``)
both consume it; neither owns it, so control no longer has to reach into a
web module private to learn per-source availability.

``read_source_status()`` is a WIRE FORMAT: it is exactly the JSON body
``GET /state`` on the /sources/ wizard sends, and the shape jasper-control's
mux-status augmenter reads ``available``/``enabled`` out of.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Mapping

from ..audio_hardware.usb_port_role import gadget_unavailable_detail
from ..bluetooth.availability import (
    BLUETOOTH_CONTROL_PLANE_UNIT,
    BluetoothAvailability,
    bluetooth_unavailable_reason,
    probe_bluetooth_availability,
)
from ..fanin.status import (
    DIRECT_HEALTH_CAPTURING,
    DIRECT_HEALTH_IDLE,
    extract_direct_sample,
    read_fanin_status,
)
from ..install_profile import (
    install_profile_allows_local_sources,
    read_install_profile,
)
from ..music_sources import SOURCE_SPECS, Source
from ..output_hardware import current_usb_data_role
from ..service_units import read_unit_states, unit_active, unit_activating, unit_loaded
from ..source_intent import read_source_intents
from .markers import local_sources_allowed
from .registry import local_source_lifecycle

logger = logging.getLogger(__name__)

_BLUETOOTH_STATE_TIMEOUT_SEC = 5.0


def _intent_unit(source: Source) -> str:
    unit = local_source_lifecycle(source).intent_unit
    if unit is None:
        raise RuntimeError(f"{source.value} has no systemd intent unit")
    return unit


def _single_unit(units: tuple[str, ...], label: str) -> str:
    if len(units) != 1:
        raise RuntimeError(f"{label} expected one unit, got {units!r}")
    return units[0]


# jasper-usbsink.service is the derived USB lifecycle unit; canonical intent is
# owned by jasper.source_intent. The composite gadget unit owns the host-visible
# ConfigFS gadget (default-on network where hardware permits + user-toggled audio).
USBSINK_UNIT = _intent_unit(Source.USBSINK)
USBSINK_GADGET_UNIT = _single_unit(
    local_source_lifecycle(Source.USBSINK).advertise_units,
    "USB Audio Input advertise units",
)
SOURCE_UNAVAILABLE = {
    "airplay": (
        "AirPlay is not installed on this speaker. Re-run install.sh to "
        "set up the local renderer stack."
    ),
    "spotify_connect": (
        "Spotify Connect is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
    "bluetooth": (
        "Bluetooth audio is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
    "usbsink": (
        "USB Audio Input is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
}

# read_unit_states() returning None means systemctl itself is unreachable,
# timed out, or answered nothing -- not that no JTS units are installed.
# Every systemd-backed source (AirPlay, Spotify, USB, and Bluetooth's
# activation-unit check) reports this distinct reason instead of the
# per-source SOURCE_UNAVAILABLE message, so the UI suggests retrying rather
# than re-running install.sh on a speaker that is actually fine.
UNIT_STATE_UNAVAILABLE_REASON = (
    "systemd unit state is unavailable right now; retry shortly."
)

# The ALSA card the composite gadget's uac2 function registers. Its presence
# is the host-visible "USB audio device is advertised" signal now that the
# gadget unit can outlive audio (it also carries the USB management network),
# so gadget-active is no longer a proxy for audio-advertised.
UAC2_CARD_PATH = "/proc/asound/UAC2Gadget"
BLUETOOTH_RUNTIME_UNITS = local_source_lifecycle(Source.BLUETOOTH).runtime_units
_BLUETOOTH_LIFECYCLE = local_source_lifecycle(Source.BLUETOOTH)
_STATE_UNITS = tuple(dict.fromkeys((
    *local_source_lifecycle(Source.AIRPLAY).health_units,
    *local_source_lifecycle(Source.SPOTIFY).health_units,
    USBSINK_UNIT,
    USBSINK_GADGET_UNIT,
    BLUETOOTH_CONTROL_PLANE_UNIT,
    *_BLUETOOTH_LIFECYCLE.runtime_units,
)))


def _uac2_card_present() -> bool:
    """True iff the composite gadget's uac2 (audio) function is composed —
    the host currently sees JTS as a USB audio device. Fail-soft to False."""
    try:
        return os.path.isdir(UAC2_CARD_PATH)
    except OSError:
        return False


def _usbsink_capability() -> tuple[bool, str]:
    """Read the shared hardware role instead of re-parsing boot config."""

    try:
        state = current_usb_data_role()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("USB data-role probe failed: %s", exc)
        return False, "USB hardware capability state is unavailable."
    return state.gadget_available, gadget_unavailable_detail(state)


def _profile_allows_local_sources() -> bool:
    """True when this install role may run local source resource groups."""
    try:
        return install_profile_allows_local_sources(read_install_profile())
    except ValueError as e:
        logger.warning("invalid install profile while reading source status: %s", e)
        return False


def _source_state(
    *,
    desired: bool,
    observed: bool,
    available: bool,
    parked: bool = False,
    unavailable_reason: str = "",
    degraded_reason: str = "",
) -> dict[str, bool | str]:
    if parked:
        effective = "parked"
    elif desired and not available:
        effective = "unavailable"
    elif degraded_reason or observed != desired:
        effective = "degraded"
    else:
        effective = "on" if observed else "off"
    state: dict[str, bool | str] = {
        # `enabled` is retained as the compatibility name consumed by the
        # existing UI. Both fields are the persisted desired state, never an
        # inference from a process that may have crashed.
        "enabled": bool(desired),
        "desired": bool(desired),
        "effective": effective,
        "available": bool(available),
    }
    if not available and unavailable_reason:
        state["unavailableReason"] = unavailable_reason
    if effective == "degraded":
        state["degradedReason"] = degraded_reason or (
            "This source is set to "
            f"{'on' if desired else 'off'}, but its current runtime state "
            "does not match. Check jasper-doctor or try the toggle again."
        )
    return state


def _source_availability(
    source: Source,
    *,
    records: Mapping[str, dict[str, Any]] | None,
    profile_allows: bool | None = None,
    bluetooth: BluetoothAvailability | None = None,
) -> tuple[bool, str]:
    """Whether ``source`` may run now, and the reason it may not ("" when it may).

    One derivation for the status snapshot (which passes its unit-record batch,
    profile verdict and Bluetooth probe) and for the turn-on precondition
    (which probes only what its one source needs). Precedence: install profile,
    then the unit-state read itself (``records`` is None when systemctl was
    unreachable or answered nothing: that is not "not installed"), then the
    source's units (USB: main unit, then gadget unit), then hardware (USB data
    role; Bluetooth adapter and units together).
    """
    wizard_key = SOURCE_SPECS[source].wizard_key
    if profile_allows is None:
        profile_allows = _profile_allows_local_sources()
    if not profile_allows:
        return False, SOURCE_UNAVAILABLE[wizard_key]
    if records is None:
        return False, UNIT_STATE_UNAVAILABLE_REASON
    if source == Source.BLUETOOTH:
        if bluetooth is None:
            bluetooth = _bluetooth_availability(records)
        if not bluetooth.available:
            return False, bluetooth_unavailable_reason(bluetooth)
        return True, ""
    if source == Source.USBSINK:
        if not unit_loaded(records.get(USBSINK_UNIT)):
            return False, SOURCE_UNAVAILABLE[wizard_key]
        if not unit_loaded(records.get(USBSINK_GADGET_UNIT)):
            return False, (
                "USB Audio Input is missing its composite gadget unit. Re-run "
                "install.sh to repair the local renderer stack."
            )
        gadget_available, reason = _usbsink_capability()
        return gadget_available, ("" if gadget_available else reason)
    lifecycle = local_source_lifecycle(source)
    if not all(unit_loaded(records.get(unit)) for unit in lifecycle.health_units):
        return False, SOURCE_UNAVAILABLE[wizard_key]
    return True, ""


def _systemd_source_state(
    source: Source,
    *,
    desired: bool,
    parked: bool,
    records: Mapping[str, dict[str, Any]] | None,
    profile_allows: bool,
) -> dict[str, bool | str]:
    lifecycle = local_source_lifecycle(source)
    available, unavailable_reason = _source_availability(
        source, records=records, profile_allows=profile_allows,
    )
    # ``records`` is None when the shared batch read was unavailable:
    # _source_availability already turned that into UNIT_STATE_UNAVAILABLE_REASON
    # above, so every unit here reads as "not observed active" -- unknown,
    # never a fabricated "still active" degradation.
    records_map = records or {}
    active = {
        unit: unit_active(records_map.get(unit)) for unit in lifecycle.health_units
    }
    observed = all(active.values()) if desired else any(active.values())
    inactive = [unit for unit, running in active.items() if not running]
    degraded_reason = ""
    if desired and inactive:
        degraded_reason = f"required services are inactive: {', '.join(inactive)}"
    elif not desired:
        unexpected = [unit for unit, running in active.items() if running]
        if unexpected:
            degraded_reason = f"services are still active: {', '.join(unexpected)}"
    return _source_state(
        desired=desired,
        observed=observed,
        available=available,
        parked=parked,
        unavailable_reason=unavailable_reason,
        degraded_reason=degraded_reason,
    )


async def _bt_state() -> tuple[bool, bool]:
    """Return (powered, has_paired_hid) from the BlueZ control plane.

    Hardware availability is probed independently so an intentionally RF-killed
    adapter remains available to turn back on. ``has_paired_hid`` is true when
    a wireless remote (volume knob etc.) is paired — the wizard surfaces this
    as a confirm-before-off prompt so toggling BT doesn't silently kill the
    remote."""
    try:
        # Both lazy: dbus-next costs ~11 MB RSS, and jasper-control imports this
        # module at startup; only a Bluetooth probe should pay for it. Inside
        # the guard so a broken dbus-next degrades to off like a wedged BlueZ.
        from ..bluetooth.adapter import has_paired_hid  # lazy: import cost
        from ..bluetooth.adapter import state as _bt_adapter_state  # lazy: import cost

        s = await _bt_adapter_state()
        powered = bool(s.get("powered", False))
        hid = False
        try:
            hid = await has_paired_hid()
        except Exception as e:  # noqa: BLE001
            # Non-fatal: the powered toggle still works, we just lose
            # the warning. Logged in case the helper itself breaks.
            logger.debug("has_paired_hid probe failed: %s", e)
        return powered, hid
    except Exception as e:  # noqa: BLE001
        # A stopped/wedged control plane is effective-off, not proof that the
        # hardware is absent. The independent sysfs/unit probe owns available.
        logger.debug("bluetooth state probe failed: %s", e)
        return False, False


def _bluetooth_units() -> tuple[str, ...]:
    """The units Bluetooth's activation-availability check reads."""
    lifecycle = local_source_lifecycle(Source.BLUETOOTH)
    return (BLUETOOTH_CONTROL_PLANE_UNIT, *lifecycle.runtime_units)


def _bluetooth_availability(
    records: Mapping[str, dict[str, Any]],
) -> BluetoothAvailability:
    """Adapter and activation-unit availability over one ``read_unit_states``
    batch that covers ``_bluetooth_units()``."""
    return probe_bluetooth_availability(lambda unit: unit_loaded(records.get(unit)))


def sources_parked() -> bool:
    """True while the reconciler-facing role verdict parks local sources
    (an active bonded follower, or a role transition still in flight).

    Deliberately the markers role verdict
    (:func:`jasper.local_sources.markers.local_sources_allowed`), not
    ``jasper.web._common.bonded_follower_active``: on a config read failure
    the verdict here honours a prior reconciler deny (stays parked) where
    ``_common`` fails open.
    """
    return not local_sources_allowed()[0]


def read_source_status() -> dict[str, dict[str, bool | str]]:
    """One-shot snapshot of all four sources. The BT branch runs an
    asyncio task because dbus-next is async-only; the rest share one
    ``systemctl show`` batch via :func:`jasper.service_units.read_unit_states`."""
    intents = read_source_intents()
    # None (not {}) is preserved past this point: it means the batch itself
    # is unavailable (systemctl unreachable/timed out), which every
    # systemd-backed source must report as UNIT_STATE_UNAVAILABLE_REASON, not
    # as "every unit not-found". ``records_map`` below is the None-safe {}
    # fallback for direct .get() reads of observed (not availability) state.
    records = read_unit_states(_STATE_UNITS, timeout=5.0)
    records_map = records or {}
    try:
        bt_powered, bt_has_hid = asyncio.run(asyncio.wait_for(
            _bt_state(),
            timeout=_BLUETOOTH_STATE_TIMEOUT_SEC,
        ))
    except asyncio.TimeoutError:
        logger.warning(
            "Bluetooth state probe exceeded %.1fs",
            _BLUETOOTH_STATE_TIMEOUT_SEC,
        )
        bt_powered, bt_has_hid = False, False
    profile_allows = _profile_allows_local_sources()
    parked = sources_parked()
    usbsink_available, usbsink_reason = _source_availability(
        Source.USBSINK, records=records, profile_allows=profile_allows,
    )
    usbsink_main_active = unit_active(records_map.get(USBSINK_UNIT))
    # Host-visible audio device presence is the uac2 ALSA card, NOT gadget-unit
    # activity: the composite gadget can outlive audio (it also carries the USB
    # management network), so its being active no longer implies audio is
    # advertised. The card exists iff the uac2 function is composed.
    usbsink_card_present = _uac2_card_present()
    usbsink_starting = unit_activating(records_map.get(USBSINK_UNIT))
    fanin_status = read_fanin_status()
    usbsink_direct_sample = extract_direct_sample(fanin_status)
    usbsink_direct_present = usbsink_direct_sample is not None
    usbsink_direct_healthy = (
        usbsink_direct_sample is not None
        and usbsink_direct_sample.present
        and usbsink_direct_sample.health
        in {DIRECT_HEALTH_IDLE, DIRECT_HEALTH_CAPTURING}
    )
    usbsink_desired = intents[Source.USBSINK]
    # jasper-usbsink is the process-free lifecycle-readiness marker; fan-in is
    # the real PCM consumer. Desired-On therefore requires all three boundaries:
    # readiness proof, advertised UAC2 card, and a healthy direct fan-in lane.
    # Desired-Off treats any surviving boundary as drift.
    usbsink_effectively_on = (
        (usbsink_main_active or usbsink_starting)
        and usbsink_card_present
        and usbsink_direct_healthy
        if usbsink_desired
        else (
            usbsink_main_active
            or usbsink_starting
            or usbsink_card_present
            or usbsink_direct_present
        )
    )
    usbsink_degraded_reason = ""
    if usbsink_available and usbsink_desired:
        if usbsink_card_present and not (
            usbsink_main_active or usbsink_starting
        ):
            usbsink_degraded_reason = (
                "USB Audio Input is advertised to hosts, but its lifecycle "
                "service is not active yet. Toggle it off to hide the USB "
                "device, or "
                "check jasper-doctor if it stays here."
            )
        elif (usbsink_main_active or usbsink_starting) and not usbsink_card_present:
            usbsink_degraded_reason = (
                "USB Audio Input's lifecycle service is running, but the "
                "host-visible audio device is not advertised. Check "
                "jasper-doctor or turn "
                "the source off until gadget mode is repaired."
            )
        elif not usbsink_direct_present:
            usbsink_degraded_reason = (
                "USB Audio Input is advertised, but fan-in has no direct USB "
                "capture lane. Check jasper-doctor or toggle the source again."
            )
        elif usbsink_direct_sample is not None and not usbsink_direct_healthy:
            usbsink_degraded_reason = (
                "USB Audio Input's direct fan-in capture lane is not healthy "
                f"({usbsink_direct_sample.health or 'unknown'})."
            )
    bt_availability = _bluetooth_availability(records_map)
    bt_any_soft_blocked = bt_availability.any_soft_blocked
    bt_all_soft_blocked = bt_availability.all_soft_blocked
    bt_rfkill_error = bt_availability.error
    bt_unit_active = {
        unit: unit_active(records_map.get(unit)) for unit in BLUETOOTH_RUNTIME_UNITS
    }
    bt_runtime_active = all(bt_unit_active.values())
    bt_available_for_role, bt_unavailable_reason = _source_availability(
        Source.BLUETOOTH,
        records=records,
        profile_allows=profile_allows,
        bluetooth=bt_availability,
    )
    bt_desired = intents[Source.BLUETOOTH]
    bt_observed_on = (
        bt_powered and bt_runtime_active and bt_any_soft_blocked is not True
    )
    bt_degraded: list[str] = []
    if bt_rfkill_error:
        bt_degraded.append(f"RF-kill state is unreadable: {bt_rfkill_error}")
    if bt_desired:
        if bt_any_soft_blocked is True:
            bt_degraded.append("the Bluetooth radio is RF-killed")
        if not bt_powered:
            bt_degraded.append("BlueZ reports the adapter powered off")
        inactive = [unit for unit in BLUETOOTH_RUNTIME_UNITS if not bt_unit_active[unit]]
        if inactive:
            bt_degraded.append(f"required services are inactive: {', '.join(inactive)}")
    else:
        active = [unit for unit, is_active in bt_unit_active.items() if is_active]
        if active:
            bt_degraded.append(f"services are still active: {', '.join(active)}")
        if bt_powered:
            bt_degraded.append("BlueZ still reports the adapter powered on")
        if bt_all_soft_blocked is False:
            bt_degraded.append("the Bluetooth radio is not RF-killed")
    return {
        # Sibling key, not a source: the JS iterates a fixed SOURCES list,
        # so this rides alongside safely. Bonded followers are parked by the
        # grouping reconciler. The page disables toggles and explains; POST
        # /set 409s.
        "pair": {"parked": parked},
        "airplay": _systemd_source_state(
            Source.AIRPLAY,
            desired=intents[Source.AIRPLAY], parked=parked,
            records=records,
            profile_allows=profile_allows,
        ),
        "bluetooth": {
            **_source_state(
                desired=bt_desired,
                observed=bt_observed_on,
                available=bt_available_for_role,
                parked=parked,
                unavailable_reason=bt_unavailable_reason,
                degraded_reason="; ".join(bt_degraded),
            ),
            "hasPairedHid": bt_has_hid,
        },
        "spotify_connect": _systemd_source_state(
            Source.SPOTIFY,
            desired=intents[Source.SPOTIFY], parked=parked,
            records=records,
            profile_allows=profile_allows,
        ),
        "usbsink": _source_state(
            desired=usbsink_desired,
            observed=usbsink_effectively_on,
            available=usbsink_available,
            parked=parked,
            unavailable_reason=usbsink_reason,
            degraded_reason=usbsink_degraded_reason,
        ),
    }


def enable_blocker(source: Source) -> str:
    """Return "" when ``source`` may be turned on now, else the reason the
    /sources/ wizard raises as a ``RuntimeError`` on a blocked ``POST /set``."""
    units = (
        _bluetooth_units() if source == Source.BLUETOOTH
        else local_source_lifecycle(source).health_units
    )
    available, reason = _source_availability(
        source, records=read_unit_states(units, timeout=5.0),
    )
    return "" if available else reason


__all__ = ["read_source_status", "enable_blocker", "sources_parked"]
