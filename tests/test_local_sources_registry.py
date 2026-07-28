# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import ast
from pathlib import Path

from jasper.local_sources import (
    local_source_lifecycle,
    local_source_audio_refresh_units,
    local_source_lifecycles,
    local_source_park_units,
)
from jasper.music_sources import (
    MUSIC_SOURCE_SPECS,
    SOURCE_TO_ACTIVE_KEY,
    Source,
)


ROOT = Path(__file__).resolve().parents[1]


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


def test_renderer_active_keys_are_consumed_from_source_registry():
    """Keep renderer wire keys declaration-owned instead of hand-copied.

    These consumers may branch on source behavior, but the stable renderer
    state key itself belongs to ``MusicSourceSpec.renderer_active_key``.
    """

    consumers = (
        "jasper/renderer.py",
        "jasper/spotify_routing.py",
        "jasper/volume_coordinator.py",
        "jasper/tools/spotify.py",
        "jasper/tools/transport.py",
    )
    active_keys = set(SOURCE_TO_ACTIVE_KEY.values())

    for relative_path in consumers:
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        literal_uses: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in active_keys
            ):
                literal_uses.append((node.lineno, str(node.args[0].value)))
            elif isinstance(node, ast.Dict):
                literal_uses.extend(
                    (key.lineno, str(key.value))
                    for key in node.keys
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in active_keys
                    )
                )
        assert not literal_uses, (
            f"{relative_path} hard-codes renderer active keys: {literal_uses}; "
            "use SOURCE_TO_ACTIVE_KEY"
        )
