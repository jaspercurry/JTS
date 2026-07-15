# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.local_sources import (
    local_source_lifecycle,
    local_source_audio_refresh_units,
    local_source_lifecycles,
    local_source_park_units,
)
from jasper.music_sources import MUSIC_SOURCE_SPECS, Source


def test_every_declared_music_source_has_lifecycle():
    lifecycles = {lifecycle.source for lifecycle in local_source_lifecycles()}
    sources = {spec.id for spec in MUSIC_SOURCE_SPECS}
    assert lifecycles == sources
    for spec in MUSIC_SOURCE_SPECS:
        assert local_source_lifecycle(spec.id).source == spec.id


def test_shipped_source_intent_defaults_are_declared_once():
    defaults = {
        lifecycle.source: lifecycle.default_enabled
        for lifecycle in local_source_lifecycles()
    }
    assert defaults == {
        Source.AIRPLAY: True,
        Source.SPOTIFY: True,
        Source.BLUETOOTH: True,
        Source.USBSINK: False,
    }


def test_usb_runtime_includes_composite_gadget_owner():
    lifecycle = local_source_lifecycle(Source.USBSINK)
    assert "jasper-usbgadget.service" in lifecycle.runtime_units
    assert "jasper-usbsink-volume.service" in lifecycle.runtime_units
    assert set(lifecycle.health_units) == {
        "jasper-usbgadget.service",
        "jasper-usbsink.service",
    }


def test_bluetooth_health_matches_its_required_on_contract():
    bluetooth = local_source_lifecycle(Source.BLUETOOTH)

    assert set(bluetooth.health_units) <= set(bluetooth.runtime_units)
    assert "bluealsa-aplay.service" in bluetooth.health_units
    assert "bluealsa.service" in bluetooth.health_units
    assert "bt-agent.service" in bluetooth.health_units


def test_usb_parking_stops_audio_lifecycle_not_the_composite_gadget():
    """USB Audio Input's lifecycle marker and composite gadget are separate
    resources. Parking a follower must stop the audio lifecycle units but keep
    the gadget owner out of the ordinary park set: when hardware permits, the
    coordinator recomposes it to preserve the management network."""
    units = local_source_park_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbsink-volume.service" in units
    # The gadget has hardware-aware composition policy, not a blanket park stop.
    assert "jasper-usbgadget.service" not in units


def test_audio_refresh_units_stay_at_renderer_boundary():
    units = local_source_audio_refresh_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbsink-volume.service" in units
    # The composite gadget is infrastructure, not a refreshable renderer — a
    # dashboard audio restart must never recompose it (a network blip).
    assert "jasper-usbgadget.service" not in units


def test_shared_source_infrastructure_parks_with_sources():
    """Mux is shared source infrastructure, not one source's daemon."""
    assert "jasper-mux.service" in local_source_park_units()
    assert "jasper-mux.service" not in local_source_audio_refresh_units()
