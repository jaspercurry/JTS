# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle resources for local music sources.

``jasper.music_sources`` owns cross-cutting source metadata such as fan-in
labels, wizard keys, and volume mode. This module owns the operational
resources that make those sources run or advertise, plus shared local-source
infrastructure such as the source arbiter.

Keep two concepts separate:

* persistent household intent: whether a source should be enabled when this
  speaker is solo or a pair leader;
* effective runtime permission: whether the current role may run/advertise
  local sources at all.

USB Audio Input is the sharp edge: the bridge process and the host-visible
ConfigFS gadget are separate units. Parking a follower must park the entire
source resource group, not just the bridge daemon.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..music_sources import Source


@dataclass(frozen=True)
class LocalSourceLifecycle:
    """Operational resources owned by one local music source.

    ``intent_unit`` is the unit whose enabled state represents the user's
    /sources choice for systemd-backed sources. Bluetooth is runtime-only
    DBus power, so it has no persistent intent unit here. ``park_units`` are
    stopped while local sources are role-parked. ``restore_units`` are started
    only if enabled when the role un-parks, preserving systemd-backed intent.
    """

    source: Source
    intent_unit: str | None
    runtime_units: tuple[str, ...]
    park_units: tuple[str, ...]
    restore_units: tuple[str, ...]
    advertise_units: tuple[str, ...] = ()
    audio_refresh_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalSourceInfrastructureLifecycle:
    """Operational resources shared by local music sources.

    These are not one source's intent unit, so they stay out of the
    per-source lifecycle table. They still park with local sources because
    their work is meaningful only when local source inputs may run.
    """

    name: str
    runtime_units: tuple[str, ...]
    park_units: tuple[str, ...]
    restore_units: tuple[str, ...]


_LIFECYCLES: tuple[LocalSourceLifecycle, ...] = (
    LocalSourceLifecycle(
        source=Source.AIRPLAY,
        intent_unit="shairport-sync.service",
        runtime_units=("shairport-sync.service", "nqptp.service"),
        park_units=("shairport-sync.service", "nqptp.service"),
        restore_units=("shairport-sync.service", "nqptp.service"),
        advertise_units=("shairport-sync.service",),
        audio_refresh_units=("shairport-sync.service",),
    ),
    LocalSourceLifecycle(
        source=Source.SPOTIFY,
        intent_unit="librespot.service",
        runtime_units=("librespot.service",),
        park_units=("librespot.service",),
        restore_units=("librespot.service",),
        advertise_units=("librespot.service",),
        audio_refresh_units=("librespot.service",),
    ),
    LocalSourceLifecycle(
        source=Source.BLUETOOTH,
        intent_unit=None,
        runtime_units=(
            "bluealsa-aplay.service",
            "bluealsa.service",
            "bt-agent.service",
        ),
        park_units=(
            "bluealsa-aplay.service",
            "bluealsa.service",
            "bt-agent.service",
        ),
        restore_units=(
            "bluealsa-aplay.service",
            "bluealsa.service",
            "bt-agent.service",
        ),
        advertise_units=("bt-agent.service",),
        audio_refresh_units=("bluealsa-aplay.service",),
    ),
    LocalSourceLifecycle(
        source=Source.USBSINK,
        intent_unit="jasper-usbsink.service",
        runtime_units=("jasper-usbsink-init.service", "jasper-usbsink.service"),
        # Stop init first: it owns the host-visible gadget, and its PartOf=
        # relationship stops the bridge too. The bridge is included as a
        # belt-and-suspenders stop for systems with partial unit drift.
        park_units=("jasper-usbsink-init.service", "jasper-usbsink.service"),
        # Restore only the household intent unit. Requires= brings the init
        # gadget up when USB Audio Input is intentionally enabled.
        restore_units=("jasper-usbsink.service",),
        advertise_units=("jasper-usbsink-init.service",),
        audio_refresh_units=("jasper-usbsink.service",),
    ),
)


_INFRASTRUCTURE: tuple[LocalSourceInfrastructureLifecycle, ...] = (
    LocalSourceInfrastructureLifecycle(
        name="source-arbiter",
        runtime_units=("jasper-mux.service",),
        park_units=("jasper-mux.service",),
        restore_units=("jasper-mux.service",),
    ),
)


def _unique(units: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for unit in units:
        if unit in seen:
            continue
        seen.add(unit)
        out.append(unit)
    return tuple(out)


def local_source_lifecycles() -> tuple[LocalSourceLifecycle, ...]:
    return _LIFECYCLES


def local_source_lifecycle(source: Source) -> LocalSourceLifecycle:
    """Return the lifecycle resources for one declared music source."""
    for lifecycle in _LIFECYCLES:
        if lifecycle.source == source:
            return lifecycle
    raise KeyError(source)


def local_source_infrastructure_lifecycles() -> tuple[
    LocalSourceInfrastructureLifecycle, ...
]:
    return _INFRASTRUCTURE


def _flatten(
    attr: str,
    *,
    include_infrastructure: bool,
) -> tuple[str, ...]:
    units: list[str] = []
    for lifecycle in _LIFECYCLES:
        units.extend(getattr(lifecycle, attr))
    if include_infrastructure:
        for infrastructure in _INFRASTRUCTURE:
            units.extend(getattr(infrastructure, attr))
    return _unique(tuple(units))


def local_source_park_units() -> tuple[str, ...]:
    return _flatten("park_units", include_infrastructure=True)


def local_source_restore_units() -> tuple[str, ...]:
    return _flatten("restore_units", include_infrastructure=True)


def local_source_runtime_units() -> tuple[str, ...]:
    return _flatten("runtime_units", include_infrastructure=True)


def local_source_advertise_units() -> tuple[str, ...]:
    return _flatten("advertise_units", include_infrastructure=False)


def local_source_audio_refresh_units() -> tuple[str, ...]:
    """Local source units safe to refresh with ``systemctl try-restart``.

    Do not pass these to plain ``systemctl restart``: disabled optional
    sources, especially USB Audio Input, must not be resurrected by a
    dashboard audio restart.
    """
    return _flatten("audio_refresh_units", include_infrastructure=False)
