# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.local_sources import (
    local_source_lifecycle,
    local_source_audio_refresh_units,
    local_source_lifecycles,
    local_source_park_restart_units,
    local_source_park_units,
    local_source_restore_restart_units,
    local_source_restore_units,
)
from jasper.music_sources import MUSIC_SOURCE_SPECS, Source


def test_every_declared_music_source_has_lifecycle():
    lifecycles = {lifecycle.source for lifecycle in local_source_lifecycles()}
    sources = {spec.id for spec in MUSIC_SOURCE_SPECS}
    assert lifecycles == sources
    for spec in MUSIC_SOURCE_SPECS:
        assert local_source_lifecycle(spec.id).source == spec.id


def test_usb_runtime_includes_composite_gadget_owner():
    lifecycle = local_source_lifecycle(Source.USBSINK)
    assert "jasper-usbgadget.service" in lifecycle.runtime_units
    assert "jasper-usbsink-volume.service" in lifecycle.runtime_units
    assert set(lifecycle.health_units) == {
        "jasper-usbgadget.service",
        "jasper-usbsink.service",
    }


def test_source_health_units_exclude_ancillary_pairing_agent():
    bluetooth = local_source_lifecycle(Source.BLUETOOTH)

    assert set(bluetooth.health_units) <= set(bluetooth.runtime_units)
    assert "bluealsa-aplay.service" in bluetooth.health_units
    assert "bluealsa.service" in bluetooth.health_units
    assert "bt-agent.service" not in bluetooth.health_units


def test_usb_parking_stops_audio_bridge_not_the_composite_gadget():
    """USB Audio Input's audio bridge and the composite gadget are separate
    resources. Parking a follower must STOP the audio bridge units but NOT stop
    the gadget owner — stopping it would drop the always-on USB management
    network. The gadget is recomposed (park_restart) instead."""
    units = local_source_park_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbsink-volume.service" in units
    # The gadget must NOT be in the stop-park set — it stays up for the network.
    assert "jasper-usbgadget.service" not in units


def test_usb_park_restart_recomposes_the_gadget():
    """Parking recomposes the composite gadget so the host-visible audio
    function drops while the always-on USB network persists."""
    assert local_source_park_restart_units() == ("jasper-usbgadget.service",)
    assert local_source_restore_restart_units() == ("jasper-usbgadget.service",)


def test_usb_restore_uses_intent_unit_not_gadget_unit():
    """Unparking should preserve /sources intent: start the USB bridge only
    if it is enabled, letting Requires= bring the gadget up. The gadget is
    recomposed via restore_restart, never a plain start (it is always-on)."""
    units = local_source_restore_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbgadget.service" not in units


def test_audio_refresh_units_stay_at_renderer_boundary():
    units = local_source_audio_refresh_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbsink-volume.service" in units
    # The composite gadget is infrastructure, not a refreshable renderer — a
    # dashboard audio restart must never recompose it (a network blip).
    assert "jasper-usbgadget.service" not in units


def test_shared_source_infrastructure_parks_with_sources():
    """Mux is shared source infrastructure, not one source's daemon, but it
    still parks and restores with the local-source runtime profile."""
    assert "jasper-mux.service" in local_source_park_units()
    assert "jasper-mux.service" in local_source_restore_units()
    assert "jasper-mux.service" not in local_source_audio_refresh_units()
