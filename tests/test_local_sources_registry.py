# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.local_sources import (
    local_source_lifecycle,
    local_source_audio_refresh_units,
    local_source_lifecycles,
    local_source_park_units,
    local_source_restore_units,
)
from jasper.music_sources import MUSIC_SOURCE_SPECS, Source


def test_every_declared_music_source_has_lifecycle():
    lifecycles = {lifecycle.source for lifecycle in local_source_lifecycles()}
    sources = {spec.id for spec in MUSIC_SOURCE_SPECS}
    assert lifecycles == sources
    for spec in MUSIC_SOURCE_SPECS:
        assert local_source_lifecycle(spec.id).source == spec.id


def test_usb_runtime_includes_host_visible_gadget_owner():
    lifecycle = local_source_lifecycle(Source.USBSINK)
    assert "jasper-usbsink-init.service" in lifecycle.runtime_units


def test_usb_parking_includes_host_visible_gadget_owner():
    """USB Audio Input's bridge and host-visible gadget are separate
    resources. Parking local sources must tear down the gadget, not only
    stop the bridge daemon."""
    units = local_source_park_units()
    assert "jasper-usbsink-init.service" in units
    assert "jasper-usbsink.service" in units


def test_usb_restore_uses_intent_unit_not_init_unit():
    """Unparking should preserve /sources intent: start the USB bridge only
    if it is enabled, letting Requires= bring init up. The init unit is an
    implementation detail, not independent user intent."""
    units = local_source_restore_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbsink-init.service" not in units


def test_audio_refresh_units_stay_at_renderer_boundary():
    units = local_source_audio_refresh_units()
    assert "jasper-usbsink.service" in units
    assert "jasper-usbsink-init.service" not in units


def test_shared_source_infrastructure_parks_with_sources():
    """Mux is shared source infrastructure, not one source's daemon, but it
    still parks and restores with the local-source runtime profile."""
    assert "jasper-mux.service" in local_source_park_units()
    assert "jasper-mux.service" in local_source_restore_units()
    assert "jasper-mux.service" not in local_source_audio_refresh_units()
