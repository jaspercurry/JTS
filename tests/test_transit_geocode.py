# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `jasper.transit.geocode`.

The geocoder fans out across two OSM-backed services (Nominatim primary,
Photon fallback). All network calls are mocked via httpx.MockTransport
so the test suite stays hardware-free and deterministic. The module-
level cache + rate limiter are reset before each test so order
independence holds.
"""
from __future__ import annotations

import httpx
import pytest

from jasper.transit import geocode as gc


@pytest.fixture(autouse=True)
def _reset_geocode_state():
    """Wipe the module-level cache + last-call timestamp between tests
    so cache hits and rate-limit timing are deterministic per-test."""
    gc._reset_cache_for_tests()
    yield
    gc._reset_cache_for_tests()


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_geocode_nominatim_success_returns_parsed_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "nominatim.openstreetmap.org"
        assert request.url.params.get("q") == "9 Av Brooklyn"
        return httpx.Response(200, json=[{
            "lat": "40.646292", "lon": "-73.994324",
            "display_name": "9 Av, Sunset Park, Brooklyn, NY",
        }])

    result = gc.geocode("9 Av Brooklyn", http=_client_with(handler))
    assert result.lat == pytest.approx(40.646292)
    assert result.lon == pytest.approx(-73.994324)
    assert "Sunset Park" in result.display_name
    assert result.source == "nominatim"


def test_geocode_uses_cache_on_repeat_call():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[{
            "lat": "1.0", "lon": "2.0", "display_name": "x",
        }])

    client = _client_with(handler)
    gc.geocode("addr", http=client)
    second = gc.geocode("addr", http=client)
    # Whitespace + case differences should still hit cache.
    third = gc.geocode("  Addr  ", http=client)

    assert calls["n"] == 1
    assert second.source == "cache"
    assert third.source == "cache"


def test_geocode_falls_back_to_photon_on_nominatim_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[])  # no match
        assert request.url.host == "photon.komoot.io"
        return httpx.Response(200, json={
            "features": [{
                "geometry": {"coordinates": [13.4, 52.5]},
                "properties": {"name": "Berlin", "country": "Germany"},
            }],
        })

    result = gc.geocode("Berlin", http=_client_with(handler))
    assert result.source == "photon"
    assert result.lat == pytest.approx(52.5)
    assert result.lon == pytest.approx(13.4)
    assert "Berlin" in result.display_name


def test_geocode_falls_back_to_photon_on_nominatim_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(503)
        return httpx.Response(200, json={
            "features": [{
                "geometry": {"coordinates": [-73.9, 40.7]},
                "properties": {"name": "NYC"},
            }],
        })

    result = gc.geocode("anything", http=_client_with(handler))
    assert result.source == "photon"


def test_geocode_raises_when_both_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(gc.GeocodeError) as exc:
        gc.geocode("nowhere", http=_client_with(handler))
    msg = str(exc.value)
    # Composed message names both services so user can self-diagnose.
    assert "nominatim" in msg.lower()
    assert "photon" in msg.lower()


def test_geocode_raises_on_empty_query():
    with pytest.raises(gc.GeocodeError, match="empty"):
        gc.geocode("   ")


def test_geocode_handles_malformed_nominatim_response():
    """Missing lat/lon keys → fall through to photon, not crash."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[{"display_name": "x"}])
        return httpx.Response(200, json={
            "features": [{
                "geometry": {"coordinates": [1.0, 2.0]},
                "properties": {},
            }],
        })

    result = gc.geocode("ambiguous", http=_client_with(handler))
    assert result.source == "photon"


def test_geocode_handles_malformed_photon_coords():
    """Photon returning weird coords shape → GeocodeError, not crash."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={
            "features": [{
                "geometry": {"coordinates": ["not", "numbers"]},
                "properties": {},
            }],
        })

    with pytest.raises(gc.GeocodeError):
        gc.geocode("weird", http=_client_with(handler))


def test_round_coord_three_decimals():
    """Privacy: coords stored at ~110 m precision, not house-level."""
    assert gc.round_coord(40.646292) == 40.646
    assert gc.round_coord(-73.994324) == -73.994


def test_throttle_serialises_successive_calls(monkeypatch):
    """Module-level throttle blocks sub-second consecutive calls. We
    don't sleep in tests; instead we patch monotonic + sleep and check
    that throttle would have called sleep with a positive value.

    Each _throttle() reads monotonic() twice (once for elapsed-check,
    once to update _last_call_mono after any sleep), hence 4 values
    across 2 calls. Sequence: 100.0/100.0 (first call, sets baseline),
    100.2/101.2 (second call, sees 0.2 s elapsed, requests 0.8 s sleep,
    then resets baseline)."""
    times = iter([100.0, 100.0, 100.2, 101.2])
    monkeypatch.setattr(gc.time, "monotonic", lambda: next(times))
    sleeps: list[float] = []
    monkeypatch.setattr(gc.time, "sleep", lambda s: sleeps.append(s))

    gc._throttle()
    gc._throttle()
    assert len(sleeps) == 1
    assert 0.7 < sleeps[0] <= 1.0
