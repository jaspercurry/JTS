"""City-pack model + self-contained provider runtime.

Covers the toggleable city-pack layer (jasper.transit.CityPack /
enabled_packs) and the self-contained provider runtime — each provider
carries its own build_client + make_tools, so active_transit_tools lets
voice_daemon iterate enabled packs instead of hardcoding each provider. The
load-bearing test is that the pack toggle gates tool registration — a
configured provider in a *disabled* city produces no tools.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jasper.transit import (
    CITY_PACKS,
    NYC_PACK,
    REGISTRY,
    TRANSIT_CITIES_ENV,
    ActiveTransit,
    CityPack,
    active_transit_tools,
    enabled_pack_ids,
    enabled_packs,
    pack_for_provider,
)
from jasper.transit import _derive_registry


def test_nyc_pack_bundles_the_three_providers_in_order():
    assert NYC_PACK.id == "nyc"
    assert [p.id for p in NYC_PACK.providers] == ["nyc_subway", "nyc_bus", "citibike"]


def test_registry_is_derived_from_packs_single_source():
    assert REGISTRY == tuple(p for pack in CITY_PACKS for p in pack.providers)
    assert {p.id for p in REGISTRY} == {"nyc_subway", "nyc_bus", "citibike"}


def test_derive_registry_rejects_duplicate_provider_ids():
    # Provider id keys by_id / wizard dispatch / install migration / gating —
    # a duplicate across packs would silently shadow, so it must fail LOUD at
    # derivation time, not ship a first-wins shadow.
    dup = CityPack(id="dup", label="Dup", providers=(NYC_PACK.providers[0],))
    with pytest.raises(ValueError, match="duplicate transit provider id"):
        _derive_registry((NYC_PACK, dup))  # nyc_subway appears in both


def test_pack_for_provider_reverse_lookup():
    # Every NYC provider maps back to the NYC pack; unknown -> None.
    assert pack_for_provider("nyc_subway") is NYC_PACK
    assert pack_for_provider("nyc_bus") is NYC_PACK
    assert pack_for_provider("citibike") is NYC_PACK
    assert pack_for_provider("berlin_ubahn") is None
    # The mapping is consistent with pack containment for every provider.
    for pack in CITY_PACKS:
        for p in pack.providers:
            assert pack_for_provider(p.id) is pack


def test_transit_cities_env_constant():
    # The key name is owned here so wizard/daemon/install.sh can't drift.
    assert TRANSIT_CITIES_ENV == "JASPER_TRANSIT_CITIES"


def test_pack_covers_uses_provider_bboxes():
    assert NYC_PACK.covers(40.7128, -74.0060) is True  # NYC City Hall
    assert NYC_PACK.covers(0.0, 0.0) is False  # Gulf of Guinea


def test_enabled_pack_ids_absent_falls_back_to_all():
    # Key ABSENT -> all packs. Non-breaking default for installs predating
    # the toggle (each provider still self-gates on its own config).
    assert enabled_pack_ids({}) == ("nyc",)


def test_enabled_pack_ids_present_is_exactly_listed():
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "nyc"}) == ("nyc",)
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "berlin"}) == ()  # unknown ignored
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "nyc, berlin"}) == ("nyc",)


def test_enabled_pack_ids_present_but_empty_means_none():
    # The load-bearing case: a household that unchecked every city in the
    # wizard round-trips through an empty/whitespace value, which must mean
    # NONE — not the absent-key "all" fallback, or the toggle couldn't
    # actually turn transit off.
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": ""}) == ()
    assert enabled_pack_ids({"JASPER_TRANSIT_CITIES": "   "}) == ()


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
    result = active_transit_tools(nyc_on, _cfg())
    assert result.tools == [] and result.configured is False and result.clients == []

    # subway configured + NYC enabled -> subway tools register, and the built
    # client is owned by the result so the daemon can close it on shutdown.
    cfg = _cfg(subway_enabled=True, subway_station_id="127", subway_default_direction="")
    result = active_transit_tools(nyc_on, cfg)
    assert result.configured is True
    assert len(result.tools) >= 1
    assert len(result.clients) == 1

    # same config, NYC pack DISABLED -> the toggle gates: no tools at all
    result = active_transit_tools({"JASPER_TRANSIT_CITIES": "berlin"}, cfg)
    assert result.tools == [] and result.configured is False and result.clients == []


def test_active_transit_tools_isolates_a_failing_provider(monkeypatch):
    # A provider whose build_client OR make_tools raises must NOT take down the
    # whole call. The voice daemon builds this at startup BEFORE its main
    # try/except, and make_tools lazily imports each tool factory — so an
    # unguarded raise (e.g. ImportError) would crash the entire daemon. It must
    # degrade to "no tools for that provider"; sibling providers still register.
    import jasper.transit as transit_mod

    def _good_tool():
        return None

    class _Good:
        id = "good"

        def build_client(self, cfg):
            return object()

        def make_tools(self, client):
            return [_good_tool]

    class _BuildBoom:
        id = "build_boom"

        def build_client(self, cfg):
            raise RuntimeError("build kaboom")

        def make_tools(self, client):  # never reached
            return [_good_tool]

    class _ToolsBoom:
        id = "tools_boom"

        def build_client(self, cfg):
            return object()

        def make_tools(self, client):
            raise ImportError("lazy import kaboom")  # the real-world shape

    pack = CityPack(
        id="test", label="Test",
        providers=(_BuildBoom(), _ToolsBoom(), _Good()),
    )
    monkeypatch.setattr(transit_mod, "CITY_PACKS", (pack,))

    # Two broken providers, yet the call must not raise.
    result = active_transit_tools({"JASPER_TRANSIT_CITIES": "test"}, _cfg())
    # Only the good provider's tool survived.
    assert result.tools == [_good_tool]
    assert result.configured is True
    # Every provider that built a client is owned for cleanup — including
    # _ToolsBoom, whose make_tools raised AFTER build_client succeeded (so its
    # client is still closed on shutdown). _BuildBoom never built one.
    assert len(result.clients) == 2


def test_active_transit_aclose_is_duck_typed_and_failure_safe():
    # The managed result owns client cleanup: it closes pooled clients,
    # skips per-call ones (no aclose), and swallows a failing aclose so one
    # bad client can't abort daemon shutdown.
    closed: list[str] = []

    class _Pooled:
        async def aclose(self):
            closed.append("ok")

    class _PerCall:  # no aclose -> must be skipped, not error
        pass

    class _Broken:
        async def aclose(self):
            raise RuntimeError("boom")  # must be swallowed, not propagated

    result = ActiveTransit(
        tools=[], configured=False, clients=[_Pooled(), _PerCall(), _Broken()],
    )
    asyncio.run(result.aclose())  # must not raise
    assert closed == ["ok"]
