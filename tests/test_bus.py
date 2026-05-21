"""Unit tests for jasper.bus.BusClient — mock HTTP, no network."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from jasper.bus import BusArrival, BusClient


def _siri_response(visits):
    return {
        "Siri": {
            "ServiceDelivery": {
                "StopMonitoringDelivery": [{"MonitoredStopVisit": visits}],
            }
        }
    }


def _visit(route, dest, eta_iso, presentable="approaching", stops_away=0):
    return {
        "MonitoredVehicleJourney": {
            "PublishedLineName": route,
            "DestinationName": dest,
            "MonitoredCall": {
                "ExpectedArrivalTime": eta_iso,
                "Extensions": {
                    "Distances": {
                        "PresentableDistance": presentable,
                        "StopsFromCall": stops_away,
                        "DistanceFromCall": 100.0,
                    },
                },
            },
        }
    }


def _client_with(response_json, *, now: datetime, **kwargs):
    """Wire a BusClient to a mocked httpx transport that returns the given
    JSON for any GET. Clock is pinned to `now` so ETA→minutes math is
    deterministic."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_json)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    defaults = dict(
        stop_id="MTA_302680", api_key="key",
        configured_routes=None, http=http, clock=lambda: now,
    )
    defaults.update(kwargs)
    return BusClient(**defaults)


@pytest.mark.asyncio
async def test_enabled_requires_key_and_stop():
    assert BusClient(stop_id="", api_key="key").enabled is False
    assert BusClient(stop_id="MTA_X", api_key="").enabled is False
    assert BusClient(stop_id="MTA_X", api_key="key").enabled is True


@pytest.mark.asyncio
async def test_strips_MTA_prefix_from_stop_id():
    c = BusClient(stop_id="MTA_302680", api_key="k")
    assert c._stop_id == "302680"
    c2 = BusClient(stop_id="302680", api_key="k")
    assert c2._stop_id == "302680"


@pytest.mark.asyncio
async def test_returns_empty_when_disabled():
    c = BusClient(stop_id="", api_key="")
    assert await c.get_arrivals() == []


@pytest.mark.asyncio
async def test_parses_arrival_minutes_from_eta():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:31:00+00:00",
               presentable="approaching", stops_away=0),
        _visit("B70", "DYKER HEIGHTS", "2026-05-21T16:35:30+00:00",
               presentable="1 stop away", stops_away=1),
    ])
    client = _client_with(response, now=now)
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert [a.route for a in arrivals] == ["B35", "B70"]
    assert arrivals[0].minutes_from_now == 1
    assert arrivals[1].minutes_from_now == 6  # 5.5 rounds to 6
    assert arrivals[0].presentable_distance == "approaching"


@pytest.mark.asyncio
async def test_filters_to_configured_routes_when_set():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:31:00+00:00"),
        _visit("B63", "BAY RIDGE", "2026-05-21T16:32:00+00:00"),
        _visit("B70", "DYKER HEIGHTS", "2026-05-21T16:33:00+00:00"),
    ])
    client = _client_with(response, now=now, configured_routes=["B35", "B70"])
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert [a.route for a in arrivals] == ["B35", "B70"]


@pytest.mark.asyncio
async def test_filters_to_explicit_route_arg():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:31:00+00:00"),
        _visit("B70", "DYKER HEIGHTS", "2026-05-21T16:33:00+00:00"),
    ])
    client = _client_with(response, now=now, configured_routes=["B35", "B70"])
    try:
        arrivals = await client.get_arrivals(route="B70")
    finally:
        await client.aclose()
    assert [a.route for a in arrivals] == ["B70"]


@pytest.mark.asyncio
async def test_drops_past_arrivals_beyond_grace_window():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        # 2 minutes ago — well past the 30s grace, should drop
        _visit("B35", "X", "2026-05-21T16:28:00+00:00"),
        # 5 seconds ago — within grace, should include (as 0 mins)
        _visit("B70", "Y", "2026-05-21T16:29:55+00:00"),
        # 10 mins in the future
        _visit("B35", "Z", "2026-05-21T16:40:00+00:00"),
    ])
    client = _client_with(response, now=now)
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert len(arrivals) == 2
    assert arrivals[0].route == "B70"
    assert arrivals[0].minutes_from_now == 0


@pytest.mark.asyncio
async def test_sorts_by_eta_ascending():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "X", "2026-05-21T16:40:00+00:00"),
        _visit("B70", "Y", "2026-05-21T16:32:00+00:00"),
        _visit("B35", "Z", "2026-05-21T16:35:00+00:00"),
    ])
    client = _client_with(response, now=now)
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert [a.minutes_from_now for a in arrivals] == [2, 5, 10]


@pytest.mark.asyncio
async def test_handles_http_error_returns_empty():
    def handler(request):
        return httpx.Response(503, text="upstream down")
    client = BusClient(
        stop_id="MTA_X", api_key="k",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert arrivals == []


@pytest.mark.asyncio
async def test_caches_results_within_window():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "X", "2026-05-21T16:32:00+00:00"),
    ])
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json=response)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = BusClient(
        stop_id="MTA_X", api_key="k", http=http, clock=lambda: now,
    )
    try:
        # First call hits HTTP
        a1 = await client.get_arrivals()
        # Second call within 20s should be served from cache
        a2 = await client.get_arrivals()
    finally:
        await client.aclose()
    assert call_count["n"] == 1
    assert a1 == a2
