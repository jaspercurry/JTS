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

USB Audio Input is the sharp edge: the audio bridge process and the composite
ConfigFS gadget are separate units, and the gadget now carries an always-on USB
management network in ADDITION to the wizard-toggled audio function. So parking
a follower must STOP the audio bridge units but only RECOMPOSE (restart) the
gadget owner — the host-visible audio device disappears while the network
function persists. That distinction is expressed declaratively here
(``park_restart_units`` / ``restore_restart_units``); the reconciler stays
source-agnostic and never special-cases usbsink.
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

    ``park_restart_units`` / ``restore_restart_units`` are units that must be
    RESTARTED (not stopped/started) on park / restore — the shape a composite
    unit needs when parking should tear down only *part* of what it owns. The
    USB gadget is the sole user: on park it recomposes without the audio
    function (host-visible audio device gone) but keeps the always-on USB
    network; on restore it recomposes to add the audio function back iff USB
    audio is enabled. Restart-on-both keeps the reconciler declarative.
    """

    source: Source
    intent_unit: str | None
    runtime_units: tuple[str, ...]
    park_units: tuple[str, ...]
    restore_units: tuple[str, ...]
    advertise_units: tuple[str, ...] = ()
    audio_refresh_units: tuple[str, ...] = ()
    park_restart_units: tuple[str, ...] = ()
    restore_restart_units: tuple[str, ...] = ()


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
        runtime_units=(
            "jasper-usbgadget.service",
            "jasper-usbsink.service",
            "jasper-usbsink-volume.service",
        ),
        # Park the AUDIO bridge units only. The composite gadget owner is NOT
        # stopped here — stopping it would also drop the always-on USB
        # management network. It is recomposed instead (park_restart_units).
        park_units=(
            "jasper-usbsink.service",
            "jasper-usbsink-volume.service",
        ),
        # Restore only the household intent unit. Requires= brings the gadget
        # up when USB Audio Input is intentionally enabled.
        restore_units=("jasper-usbsink.service",),
        advertise_units=("jasper-usbgadget.service",),
        audio_refresh_units=("jasper-usbsink.service", "jasper-usbsink-volume.service"),
        # Recompose the gadget on park: it drops the uac2 (audio) function
        # because the bridge is now parked, but keeps ncm (network) up — the
        # host-visible audio device disappears while USB networking persists.
        park_restart_units=("jasper-usbgadget.service",),
        # On un-park, recompose again so the audio function comes back iff USB
        # audio is enabled (gadget-up reads the intent). Mirrors the park side.
        restore_restart_units=("jasper-usbgadget.service",),
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


def local_source_audio_refresh_units() -> tuple[str, ...]:
    """Local source units safe to refresh with ``systemctl try-restart``.

    Do not pass these to plain ``systemctl restart``: disabled optional
    sources, especially USB Audio Input, must not be resurrected by a
    dashboard audio restart.
    """
    return _flatten("audio_refresh_units", include_infrastructure=False)


def local_source_park_restart_units() -> tuple[str, ...]:
    """Units RESTARTED (recomposed) rather than stopped when local sources
    park. Today only the composite USB gadget: parking recomposes it without
    the audio function while keeping the always-on USB network."""
    return _flatten("park_restart_units", include_infrastructure=False)


def local_source_restore_restart_units() -> tuple[str, ...]:
    """Units RESTARTED (recomposed) rather than started when local sources
    un-park. Mirror of :func:`local_source_park_restart_units`; the gadget
    recomposes to add the audio function back iff USB audio is enabled."""
    return _flatten("restore_restart_units", include_infrastructure=False)
