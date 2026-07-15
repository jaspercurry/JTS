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

This is an inventory, not a generic lifecycle executor. It declares shared
defaults and the ordinary runtime/guard/health resources used by status,
installer, and safety checks. Source-specific mechanics remain in the one
host-owned source coordinator. For example, USB Audio Input needs ordered
fan-in arming and ConfigFS recomposition; those operations are deliberately
implemented by ``jasper.source_intent`` rather than encoded as increasingly
powerful registry callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..music_sources import Source


@dataclass(frozen=True)
class LocalSourceLifecycle:
    """Operational resources owned by one local music source.

    ``default_enabled`` is the shipped household intent used when the wizard
    has not written an override. ``intent_unit`` is the unit whose enabled
    state mirrors that intent for systemd-backed sources. Bluetooth has no
    single intent unit: its radio and dependent units are reconciled together.
    ``health_units`` are the source-critical units whose failure means the
    source needs attention. A helper belongs there when the source's On
    contract requires it; Bluetooth pairing is a product capability, so its
    agent is health-critical even though it does not carry audio frames.
    ``park_units`` are stopped while local sources are role-parked.

    Special source mechanisms are intentionally absent. The canonical source
    coordinator owns those concrete appliers so this inventory stays small and
    cannot become a second orchestration framework.
    """

    source: Source
    default_enabled: bool
    intent_unit: str | None
    runtime_units: tuple[str, ...]
    health_units: tuple[str, ...]
    park_units: tuple[str, ...]
    advertise_units: tuple[str, ...] = ()
    audio_refresh_units: tuple[str, ...] = ()


_LIFECYCLES: tuple[LocalSourceLifecycle, ...] = (
    LocalSourceLifecycle(
        source=Source.AIRPLAY,
        default_enabled=True,
        intent_unit="shairport-sync.service",
        runtime_units=("shairport-sync.service", "nqptp.service"),
        health_units=("shairport-sync.service", "nqptp.service"),
        park_units=("shairport-sync.service", "nqptp.service"),
        advertise_units=("shairport-sync.service",),
        audio_refresh_units=("shairport-sync.service",),
    ),
    LocalSourceLifecycle(
        source=Source.SPOTIFY,
        default_enabled=True,
        intent_unit="librespot.service",
        runtime_units=("librespot.service",),
        health_units=("librespot.service",),
        park_units=("librespot.service",),
        advertise_units=("librespot.service",),
        audio_refresh_units=("librespot.service",),
    ),
    LocalSourceLifecycle(
        source=Source.BLUETOOTH,
        default_enabled=True,
        intent_unit=None,
        runtime_units=(
            "bluealsa-aplay.service",
            "bluealsa.service",
            "bt-agent.service",
        ),
        health_units=(
            "bluealsa-aplay.service",
            "bluealsa.service",
            "bt-agent.service",
        ),
        park_units=(
            "bluealsa-aplay.service",
            "bluealsa.service",
            "bt-agent.service",
        ),
        advertise_units=("bt-agent.service",),
        audio_refresh_units=("bluealsa-aplay.service",),
    ),
    LocalSourceLifecycle(
        source=Source.USBSINK,
        default_enabled=False,
        intent_unit="jasper-usbsink.service",
        runtime_units=(
            "jasper-usbgadget.service",
            "jasper-usbsink.service",
            "jasper-usbsink-volume.service",
        ),
        health_units=(
            "jasper-usbgadget.service",
            "jasper-usbsink.service",
        ),
        # Park the audio readiness marker and volume observer only. The composite
        # gadget owner is NOT stopped here — stopping it would also drop the
        # always-on USB management network. The source coordinator separately
        # recomposes it to remove UAC2 while preserving NCM.
        park_units=(
            "jasper-usbsink.service",
            "jasper-usbsink-volume.service",
        ),
        advertise_units=("jasper-usbgadget.service",),
        audio_refresh_units=("jasper-usbsink.service", "jasper-usbsink-volume.service"),
    ),
)


_SHARED_PARK_UNITS = ("jasper-mux.service",)


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


def _flatten(attr: str) -> tuple[str, ...]:
    units: list[str] = []
    for lifecycle in _LIFECYCLES:
        units.extend(getattr(lifecycle, attr))
    return _unique(tuple(units))


def local_source_park_units() -> tuple[str, ...]:
    return _unique((*_flatten("park_units"), *_SHARED_PARK_UNITS))


def local_source_audio_refresh_units() -> tuple[str, ...]:
    """Local source units safe to refresh with ``systemctl try-restart``.

    Do not pass these to plain ``systemctl restart``: disabled optional
    sources, especially USB Audio Input, must not be resurrected by a
    dashboard audio restart.
    """
    return _flatten("audio_refresh_units")
