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


def test_nyc_subway_validate_credentials_empty_succeeds():
    """Keyless provider: an empty credentials dict is OK (nothing to
    validate). Returning None per the Protocol's success contract."""
    assert nyc_subway.PROVIDER.validate_credentials({}) is None


def test_nyc_subway_validate_credentials_rejects_unknown_keys():
    """Keyless provider given keys it doesn't own — programmer error
    on the caller's side. Reported per-key rather than raised so the
    wizard's flow doesn't 500."""
    errors = nyc_subway.PROVIDER.validate_credentials({"FOO": "x"})
    assert errors == {"FOO": "nyc_subway is keyless"}


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


def test_nyc_bus_find_stops_parses_routes_as_dict_list():
    """MTA's BusTime production response embeds `routes` as a list of
    route dicts (not string IDs in a separate references block).
    Regression for the "unhashable type: dict" crash that surfaced
    live: dict-shape routes must be parsed by extracting shortName
    directly, not by route_map lookup."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {
                "stops": [
                    {
                        "id": "MTA_302680",
                        "name": "4 AV/39 ST",
                        "lat": 40.6533, "lon": -73.9994,
                        "direction": "E",
                        # MTA BusTime production shape: list of dicts,
                        # no separate references block.
                        "routes": [
                            {"id": "MTA NYCT_B35", "shortName": "B35"},
                            {"id": "MTA NYCT_B70", "shortName": "B70"},
                        ],
                    },
                ],
            },
        })

    provider = _bus_with_handler(handler)
    stops = provider.find_stops_near(
        40.65, -73.999, credentials={"JASPER_MTA_BUSTIME_KEY": "k"}, count=5,
    )
    assert len(stops) == 1
    assert stops[0].lines == ("B35", "B70")
    assert "B35/B70" in stops[0].display_name


def test_nyc_bus_find_stops_handles_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = _bus_with_handler(handler)
    with pytest.raises(transit.TransitError, match="BusTime"):
        provider.find_stops_near(
            40.65, -73.99, credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
        )


def test_nyc_bus_validate_credentials_none_on_oba_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "agencies-with-coverage.json" in request.url.path
        return httpx.Response(200, json={"code": 200, "data": []})

    provider = _bus_with_handler(handler)
    assert provider.validate_credentials(
        {"JASPER_MTA_BUSTIME_KEY": "good-key"},
    ) is None


def test_nyc_bus_validate_credentials_reports_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _bus_with_handler(handler)
    errors = provider.validate_credentials(
        {"JASPER_MTA_BUSTIME_KEY": "bad-key"},
    )
    assert errors and "JASPER_MTA_BUSTIME_KEY" in errors
    assert "401" in errors["JASPER_MTA_BUSTIME_KEY"]


def test_nyc_bus_validate_credentials_reports_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    provider = _bus_with_handler(handler)
    errors = provider.validate_credentials({"JASPER_MTA_BUSTIME_KEY": "key"})
    assert errors and "unreachable" in errors["JASPER_MTA_BUSTIME_KEY"]


def test_nyc_bus_validate_credentials_empty_value_rejected():
    """Don't even probe the network with a blank value — short circuit."""
    provider = _bus_with_handler(lambda req: pytest.fail("should not call"))
    errors = provider.validate_credentials({"JASPER_MTA_BUSTIME_KEY": ""})
    assert errors == {"JASPER_MTA_BUSTIME_KEY": "key is empty"}


def test_nyc_bus_validate_credentials_unknown_key_raises():
    """Unknown env_key = programming error (typo in caller). The
    Protocol allows raising for unknown keys; cred-rejection is the
    user-facing path and uses the error dict."""
    provider = _bus_with_handler(lambda req: pytest.fail("should not call"))
    with pytest.raises(NotImplementedError, match="UNKNOWN_KEY"):
        provider.validate_credentials({"UNKNOWN_KEY": "x"})


# ---- enumerate_live_routes ------------------------------------------------
#
# The motivating use case for v2: OBA's static `routes` field for a stop
# lagged real-world dispatch (e.g. B70 newly serving 4 Av/39 St didn't
# appear in OBA's stop record). SIRI is ground truth — the wizard probes
# every candidate stop to enumerate the routes actually dispatching there.

def _siri_visit(line: str) -> dict:
    return {"MonitoredVehicleJourney": {"PublishedLineName": line}}


def _siri_envelope(visits: list[dict]) -> dict:
    return {
        "Siri": {
            "ServiceDelivery": {
                "StopMonitoringDelivery": [{"MonitoredStopVisit": visits}],
            },
        },
    }


def test_enumerate_live_routes_dedups_and_preserves_first_seen_order():
    """SIRI returns one visit per imminent bus, often repeating the same
    route (multiple B35s arriving back-to-back). We dedupe and keep the
    order of first appearance so the displayed list reads naturally."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "stop-monitoring.json" in request.url.path
        assert request.url.params.get("MonitoringRef") == "302680"
        assert request.url.params.get("key") == "k"
        return httpx.Response(200, json=_siri_envelope([
            _siri_visit("B70"),
            _siri_visit("B35"),
            _siri_visit("B70"),  # dup
            _siri_visit("B35"),  # dup
        ]))

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    assert routes == ("B70", "B35")


def test_enumerate_live_routes_strips_MTA_prefix_from_stop_id():
    seen_refs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_refs.append(request.url.params.get("MonitoringRef") or "")
        return httpx.Response(200, json=_siri_envelope([]))

    provider = _bus_with_handler(handler)
    provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    provider.enumerate_live_routes(
        "302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    # Both inputs hit SIRI with the bare numeric stop ID.
    assert seen_refs == ["302680", "302680"]


def test_enumerate_live_routes_empty_visits_returns_empty_tuple():
    """A quiet stop with no buses in the next ~30 min — the wizard
    falls back to OBA's static `routes` field for display."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_siri_envelope([]))

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    assert routes == ()


def test_enumerate_live_routes_returns_empty_on_401():
    """Bad key → quiet failure. The validate_credentials path is the
    one that surfaces "key rejected" to the user; the route probe is
    informational and should never break the wizard render."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "bad"},
    )
    assert routes == ()


def test_enumerate_live_routes_returns_empty_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    assert routes == ()


def test_enumerate_live_routes_returns_empty_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    assert routes == ()


def test_enumerate_live_routes_returns_empty_on_malformed_envelope():
    """SIRI returns JSON but the expected nested path is missing —
    don't crash, fall through to empty."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    assert routes == ()


def test_enumerate_live_routes_returns_empty_when_no_credentials():
    """No key passed → don't probe at all. Matches the wizard's
    "key required" lock-state."""
    provider = _bus_with_handler(lambda req: pytest.fail("should not call"))
    assert provider.enumerate_live_routes("MTA_302680") == ()
    assert provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": ""},
    ) == ()


def test_enumerate_live_routes_returns_empty_for_blank_stop_id():
    """Defensive: caller passed an empty stop_id (probably a
    misparse upstream)."""
    provider = _bus_with_handler(lambda req: pytest.fail("should not call"))
    assert provider.enumerate_live_routes(
        "", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    ) == ()


def test_enumerate_live_routes_skips_visits_with_blank_line_names():
    """SIRI occasionally emits a visit without PublishedLineName
    (incomplete trip data). Don't include "" in the routes list."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_siri_envelope([
            _siri_visit("B35"),
            {"MonitoredVehicleJourney": {}},  # missing PublishedLineName
            {"MonitoredVehicleJourney": {"PublishedLineName": "   "}},
            _siri_visit("B70"),
        ]))

    provider = _bus_with_handler(handler)
    routes = provider.enumerate_live_routes(
        "MTA_302680", credentials={"JASPER_MTA_BUSTIME_KEY": "k"},
    )
    assert routes == ("B35", "B70")


# ---- scrub_secrets --------------------------------------------------------
#
# Defense against `httpx.HTTPError.__str__` interpolating the full URL
# (including `?key=SECRET`) into user-facing error banners and journalctl.
# Regression for B2 from the staff-engineer review.

def test_scrub_secrets_redacts_key_param_in_url():
    from jasper.transit.base import scrub_secrets
    url = "https://bustime-classic.mta.info/api/?key=ABC-DEF-123&MonitoringRef=302680"
    assert scrub_secrets(url) == (
        "https://bustime-classic.mta.info/api/?key=***&MonitoringRef=302680"
    )


def test_scrub_secrets_redacts_key_when_not_first_param():
    from jasper.transit.base import scrub_secrets
    url = "https://example.com/?lat=40.65&key=ABC123&lon=-73.99"
    assert "ABC123" not in scrub_secrets(url)
    assert "key=***" in scrub_secrets(url)


def test_scrub_secrets_handles_httpx_error_repr():
    """Real httpx exceptions stringify with the full URL embedded —
    this test pins the wrapping to ensure scrubbing reaches it."""
    from jasper.transit.base import scrub_secrets
    fake_repr = (
        "HTTPStatusError(\"Server error '500' for url "
        "'https://bustime-classic.mta.info/api/?key=SECRET_VAL&x=y'\")"
    )
    out = scrub_secrets(fake_repr)
    assert "SECRET_VAL" not in out
    assert "key=***" in out


def test_scrub_secrets_does_not_match_prose_mentions_of_key():
    """The regex requires a `?` or `&` boundary so we don't shred
    sentences that happen to contain the word 'key'."""
    from jasper.transit.base import scrub_secrets
    msg = "the bus card asks for an MTA BusTime API key in the wizard"
    assert scrub_secrets(msg) == msg


# ---- Cache resilience (M2) ------------------------------------------------
#
# Tests for BusClient live in test_bus.py — this one verifies the
# provider side hasn't regressed: validate_credentials must propagate
# the error message without caching a False answer.
