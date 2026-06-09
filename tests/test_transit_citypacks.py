"""City-pack model + self-contained provider runtime.

Covers the toggleable city-pack layer (jasper.transit.CityPack /
enabled_packs) and the self-contained provider runtime — each provider
carries its own build_client + make_tools, so active_transit_tools lets
voice_daemon iterate enabled packs instead of hardcoding each provider. The
load-bearing test is that the pack toggle gates tool registration — a
configured provider in a *disabled* city produces no tools.
"""
from __future__ import annotations

from types import SimpleNamespace

from jasper.transit import (
    CITY_PACKS,
    NYC_PACK,
    REGISTRY,
    active_transit_tools,
    enabled_pack_ids,
    enabled_packs,
    pack_by_id,
)


def test_nyc_pack_bundles_the_three_providers_in_order():
    assert NYC_PACK.id == "nyc"
    assert [p.id for p in NYC_PACK.providers] == ["nyc_subway", "nyc_bus", "citibike"]


def test_registry_is_derived_from_packs_single_source():
    assert REGISTRY == tuple(p for pack in CITY_PACKS for p in pack.providers)
    assert {p.id for p in REGISTRY} == {"nyc_subway", "nyc_bus", "citibike"}


def test_pack_by_id():
    assert pack_by_id("nyc") is NYC_PACK
    assert pack_by_id("berlin") is None


def test_pack_covers_uses_provider_bboxes():
    assert NYC_PACK.covers(40.7128, -74.0060) is True  # NYC City Hall
    assert NYC_PACK.covers(0.0, 0.0) is False  # Gulf of Guinea


def test_enabled_pack_ids_explicit_unknown_and_fallback():
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "nyc"}) == ("nyc",)
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "berlin"}) == ()  # unknown ignored
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "nyc, berlin"}) == ("nyc",)
    # unset/empty -> all packs (non-breaking for installs predating the toggle)
    assert enabled_pack_ids({}) == ("nyc",)
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "   "}) == ("nyc",)


def test_enabled_packs_returns_pack_objects():
    assert enabled_packs({"JASPER_TRANSIT_CITIES": "nyc"}) == (NYC_PACK,)
    assert enabled_packs({"JASPER_TRANSIT_CITIES": "berlin"}) == ()


def test_every_provider_is_self_contained_runtime():
    # In the city-pack design each provider carries its own build_client +
    # make_tools; a missing one would silently drop that mode's tools.
    for pack in CITY_PACKS:
        for p in pack.providers:
            assert callable(getattr(p, "build_client", None)), f"{p.id}: no build_client"
            assert callable(getattr(p, "make_tools", None)), f"{p.id}: no make_tools"


def _cfg(**over):
    base = dict(
        subway_enabled=False, subway_station_id="", subway_default_direction="",
        bus_enabled=False, bus_stops=[], mta_bustime_key="",
        citibike_enabled=False, citibike_stations=(), citibike_ebike_only=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_active_transit_tools_gates_on_both_config_and_pack():
    nyc_on = {"JASPER_TRANSIT_CITIES": "nyc"}

    # nothing configured -> no tools, not configured, no clients to close
    tools, configured, clients = active_transit_tools(nyc_on, _cfg())
    assert tools == [] and configured is False and clients == []

    # subway configured + NYC enabled -> subway tools register, and the
    # built client is returned so the daemon can close it on shutdown.
    cfg = _cfg(subway_enabled=True, subway_station_id="127", subway_default_direction="")
    tools, configured, clients = active_transit_tools(nyc_on, cfg)
    assert configured is True and len(tools) >= 1 and len(clients) == 1

    # same config, NYC pack DISABLED -> the toggle gates: no tools at all
    tools, configured, clients = active_transit_tools(
        {"JASPER_TRANSIT_CITIES": "berlin"}, cfg
    )
    assert tools == [] and configured is False and clients == []
