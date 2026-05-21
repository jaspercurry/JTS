"""Tests for the transit provider registry and the two NYC providers.

The subway provider is data-driven (CSV bundled in the package), so its
tests are deterministic. The bus provider hits BusTime over HTTP — we
construct a `_NycBus` instance with an injected `httpx.MockTransport`
so the test suite stays offline.
"""
from __future__ import annotations

import httpx
import pytest

from jasper import transit
from jasper.transit import base
from jasper.transit.providers import nyc_bus, nyc_subway


# ---- Module-level registry ------------------------------------------------


def test_registry_includes_both_nyc_providers():
    ids = [p.id for p in transit.REGISTRY]
    assert "nyc_subway" in ids
    assert "nyc_bus" in ids


def test_by_id_returns_provider_or_none():
    assert transit.by_id("nyc_subway") is transit.REGISTRY[0]
    assert transit.by_id("nope") is None


def test_covering_filters_by_bbox():
    # Sunset Park
    assert "nyc_subway" in [p.id for p in transit.covering(40.646, -73.994)]
    # London
    assert transit.covering(51.5, -0.1) == ()


def test_all_env_keys_dedupes_and_preserves_order():
    keys = transit.all_env_keys()
    # No duplicates across providers, even though both touch NYC.
    assert len(keys) == len(set(keys))
    # Subway keys come first because subway is first in REGISTRY.
    assert keys[0] == "JASPER_SUBWAY_STATION_ID"
    assert "JASPER_MTA_BUSTIME_KEY" in keys


# ---- Base types -----------------------------------------------------------


def test_bounding_box_includes_edges():
    bb = base.BoundingBox(40.0, 41.0, -74.0, -73.0)
    assert bb.includes(40.0, -74.0)  # corner
    assert bb.includes(40.5, -73.5)
    assert not bb.includes(39.99, -73.5)
    assert not bb.includes(40.5, -72.99)


def test_haversine_zero_distance():
    assert base.haversine_miles(40.0, -73.0, 40.0, -73.0) == pytest.approx(0.0, abs=1e-9)


def test_haversine_known_distance():
    # 9 Av (B12) to Fort Hamilton Pkwy (B13) is ~0.4 miles.
    d = base.haversine_miles(40.646292, -73.994324, 40.640914, -73.994304)
    assert 0.3 < d < 0.5


# ---- NYC Subway -----------------------------------------------------------


def test_nyc_subway_provider_metadata():
    p = nyc_subway.PROVIDER
    assert p.id == "nyc_subway"
    assert p.kind == "subway"
    assert p.credentials == ()
    assert "JASPER_SUBWAY_STATION_ID" in p.env_keys


def test_nyc_subway_finds_b12_nearest_to_user_home():
    """Sunset Park, NY — 9 Av (B12) should be the nearest stop by a
    wide margin to coords near the speaker's home. This is a smoke
    test against the bundled CSV."""
    stops = nyc_subway.PROVIDER.find_stops_near(40.646, -73.994, count=3)
    assert len(stops) == 3
    assert stops[0].stop_id == "B12"
    assert stops[0].distance_mi < 0.1  # right on top
    # Sorted by distance ascending.
    assert stops[0].distance_mi <= stops[1].distance_mi <= stops[2].distance_mi
    # Display name includes line + borough.
    assert "D" in stops[0].display_name
    assert "Brooklyn" in stops[0].display_name


def test_nyc_subway_far_from_nyc_returns_distant_stops():
    """The wizard is responsible for treating "nearest is too far" as
    no-coverage; the provider just returns sorted-by-distance and
    doesn't second-guess. London → nearest NYC stop is ~3500 mi away."""
    stops = nyc_subway.PROVIDER.find_stops_near(51.5, -0.1, count=1)
    assert len(stops) == 1
    assert stops[0].distance_mi > 3000


def test_nyc_subway_validate_credential_raises():
    """Subway is keyless; calling validate_credential is a programming
    error, not a user error. NotImplementedError makes the bug loud."""
    with pytest.raises(NotImplementedError):
        nyc_subway.PROVIDER.validate_credential("X", "y")


# ---- NYC Bus --------------------------------------------------------------


def _bus_with_handler(handler) -> nyc_bus._NycBus:
    return nyc_bus._NycBus(http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_nyc_bus_provider_metadata():
    p = nyc_bus.PROVIDER
    assert p.id == "nyc_bus"
    assert p.kind == "bus"
    assert len(p.credentials) == 1
    assert p.credentials[0].env_key == "JASPER_MTA_BUSTIME_KEY"


def test_nyc_bus_find_stops_requires_key():
    with pytest.raises(transit.TransitError, match="API key"):
        nyc_bus.PROVIDER.find_stops_near(40.65, -73.99, credentials={})


def test_nyc_bus_find_stops_parses_oba_response():
    """Verify we pull lat/lon/id/name from the OBA shape and resolve
    route IDs through the references block to short names."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "stops-for-location.json" in request.url.path
        assert request.url.params.get("key") == "k"
        return httpx.Response(200, json={
            "data": {
                "stops": [
                    {
                        "id": "MTA_302680",
                        "name": "4 AV/39 ST",
                        "lat": 40.6533, "lon": -73.9994,
                        "direction": "E",
                        "routeIds": ["MTA NYCT_B35", "MTA NYCT_B70"],
                    },
                    {
                        "id": "MTA_999",
                        "name": "Far Stop",
                        "lat": 40.7, "lon": -73.9,
                        "routeIds": ["MTA NYCT_B35"],
                    },
                ],
                "references": {
                    "routes": [
                        {"id": "MTA NYCT_B35", "shortName": "B35"},
                        {"id": "MTA NYCT_B70", "shortName": "B70"},
                    ],
                },
            },
        })

    provider = _bus_with_handler(handler)
    stops = provider.find_stops_near(
        40.65, -73.999, credentials={"JASPER_MTA_BUSTIME_KEY": "k"}, count=5,
    )
    assert len(stops) == 2
    near, far = stops
    assert near.stop_id == "MTA_302680"
    assert near.distance_mi < far.distance_mi
    assert near.lines == ("B35", "B70")
    assert "E" in near.display_name  # direction hint included


def test_nyc_bus_find_stops_handles_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = _bus_with_handler(handler)
    with pytest.raises(transit.TransitError, match="BusTime"):
        provider.find_stops_near(
            40.65, -73.99, credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
        )


def test_nyc_bus_validate_credential_true_on_oba_200_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "agencies-with-coverage.json" in request.url.path
        return httpx.Response(200, json={"code": 200, "data": []})

    provider = _bus_with_handler(handler)
    assert provider.validate_credential("JASPER_MTA_BUSTIME_KEY", "good-key")


def test_nyc_bus_validate_credential_false_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _bus_with_handler(handler)
    assert not provider.validate_credential("JASPER_MTA_BUSTIME_KEY", "bad-key")


def test_nyc_bus_validate_credential_false_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    provider = _bus_with_handler(handler)
    assert not provider.validate_credential("JASPER_MTA_BUSTIME_KEY", "key")


def test_nyc_bus_validate_credential_false_on_empty_value():
    """Don't even probe the network with a blank value — short circuit."""
    provider = _bus_with_handler(lambda req: pytest.fail("should not call"))
    assert not provider.validate_credential("JASPER_MTA_BUSTIME_KEY", "")
    assert not provider.validate_credential("JASPER_MTA_BUSTIME_KEY", "   ")


def test_nyc_bus_validate_credential_unknown_env_key_raises():
    provider = _bus_with_handler(lambda req: pytest.fail("should not call"))
    with pytest.raises(NotImplementedError):
        provider.validate_credential("UNKNOWN_KEY", "x")
