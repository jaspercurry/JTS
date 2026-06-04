"""Unit tests for jasper.bus.BusClient — mocked HTTP, no network.

v2 shape: BusClient holds a list of configured stops; every
get_arrivals call fans out, unions, sorts, caps. Each BusArrival
carries its own stop_id + stop_label.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from jasper.bus import BusClient, parse_bus_stops


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


def _client_with(
    response: dict,
    *,
    now: datetime,
    stop_ids: list[str] | None = None,
    stop_labels: dict[str, str] | None = None,
    api_key: str = "key",
):
    """Wire a BusClient to a mocked httpx transport that returns the
    given SIRI JSON for every configured stop. Single-stop tests use
    this; multi-stop tests with per-stop responses use
    `_multi_client_with` below."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return BusClient(
        stop_ids=stop_ids or ["MTA_302680"],
        api_key=api_key,
        stop_labels=stop_labels,
        http=http,
        clock=lambda: now,
    )


def _multi_client_with(
    response_by_stop: dict[str, dict],
    *,
    now: datetime,
    stop_ids: list[str],
    stop_labels: dict[str, str] | None = None,
    api_key: str = "key",
):
    """Wire a BusClient to a mocked transport that routes responses
    by `MonitoringRef`. Missing keys → empty SIRI response. Used by
    the multi-stop fan-out tests."""

    def handler(request: httpx.Request) -> httpx.Response:
        ref = request.url.params.get("MonitoringRef") or ""
        body = response_by_stop.get(ref, _siri_response([]))
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return BusClient(
        stop_ids=stop_ids,
        api_key=api_key,
        stop_labels=stop_labels,
        http=http,
        clock=lambda: now,
    )


# ---- parse_bus_stops helper ----------------------------------------------


def test_parse_bus_stops_single_id_no_label():
    assert parse_bus_stops("MTA_302680") == [("MTA_302680", "")]


def test_parse_bus_stops_id_with_label():
    out = parse_bus_stops("MTA_302680|4 Av/39 St eastbound")
    assert out == [("MTA_302680", "4 Av/39 St eastbound")]


def test_parse_bus_stops_multiple_stops():
    out = parse_bus_stops(
        "MTA_302680|4 Av/39 St eastbound,MTA_302682|4 Av/39 St westbound",
    )
    assert out == [
        ("MTA_302680", "4 Av/39 St eastbound"),
        ("MTA_302682", "4 Av/39 St westbound"),
    ]


def test_parse_bus_stops_empty_string_returns_empty():
    assert parse_bus_stops("") == []
    assert parse_bus_stops("  ") == []


def test_parse_bus_stops_tolerates_whitespace_and_empty_entries():
    out = parse_bus_stops("MTA_302680|A , , MTA_302682|B")
    assert out == [("MTA_302680", "A"), ("MTA_302682", "B")]


# ---- BusClient basics -----------------------------------------------------


def test_enabled_requires_key_and_at_least_one_stop():
    assert BusClient(stop_ids=[], api_key="key").enabled is False
    assert BusClient(stop_ids=["MTA_X"], api_key="").enabled is False
    assert BusClient(stop_ids=["MTA_X"], api_key="key").enabled is True


def test_strips_MTA_prefix_from_stop_ids():
    c = BusClient(stop_ids=["MTA_302680", "302681"], api_key="k")
    assert c.stop_ids == ("302680", "302681")


def test_skips_blank_stop_ids():
    """Blank entries (e.g. trailing comma in config) are ignored."""
    c = BusClient(stop_ids=["MTA_302680", "", " "], api_key="k")
    assert c.stop_ids == ("302680",)


def test_label_for_returns_configured_label():
    c = BusClient(
        stop_ids=["MTA_302680"],
        api_key="k",
        stop_labels={"MTA_302680": "4 Av/39 St eastbound"},
    )
    assert c.label_for("MTA_302680") == "4 Av/39 St eastbound"
    assert c.label_for("302680") == "4 Av/39 St eastbound"


def test_label_for_falls_back_to_bare_id_when_unset():
    c = BusClient(stop_ids=["MTA_302680"], api_key="k")
    assert c.label_for("MTA_302680") == "302680"


@pytest.mark.asyncio
async def test_returns_empty_when_disabled():
    c = BusClient(stop_ids=[], api_key="")
    assert await c.get_arrivals() == []


# ---- Single-stop arrivals -------------------------------------------------


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
    assert arrivals[0].stop_id == "302680"
    # No labels configured → falls back to the bare id.
    assert arrivals[0].stop_label == "302680"


@pytest.mark.asyncio
async def test_arrival_carries_configured_stop_label():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:31:00+00:00"),
    ])
    client = _client_with(
        response, now=now,
        stop_labels={"MTA_302680": "4 Av/39 St eastbound"},
    )
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert arrivals[0].stop_label == "4 Av/39 St eastbound"


@pytest.mark.asyncio
async def test_filters_to_explicit_route_arg():
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:31:00+00:00"),
        _visit("B70", "DYKER HEIGHTS", "2026-05-21T16:33:00+00:00"),
    ])
    client = _client_with(response, now=now)
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
    minutes = [a.minutes_from_now for a in arrivals]
    assert minutes == sorted(minutes)


@pytest.mark.asyncio
async def test_handles_http_error_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = BusClient(
        stop_ids=["MTA_302680"], api_key="k", http=http, clock=lambda: now,
    )
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    assert arrivals == []


@pytest.mark.asyncio
async def test_caches_results_within_window():
    """Repeat queries to the same stop within CACHE_WINDOW_SEC should
    only hit the network once. Each stop has its own cache so a query
    that fans across stops doesn't bust other stops' caches."""
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    response = _siri_response([
        _visit("B35", "X", "2026-05-21T16:31:00+00:00"),
    ])
    call_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=response)
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = BusClient(
        stop_ids=["MTA_302680"], api_key="k", http=http, clock=lambda: now,
    )
    try:
        await client.get_arrivals()
        await client.get_arrivals()
        await client.get_arrivals()
    finally:
        await client.aclose()
    assert call_count["n"] == 1


# ---- Multi-stop fan-out ---------------------------------------------------


@pytest.mark.asyncio
async def test_multi_stop_unions_arrivals_sorted_by_eta():
    """Two configured stops, each returning one bus. The union is
    sorted by ETA regardless of which stop each arrival came from."""
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    eastbound = _siri_response([
        _visit("B35", "BROWNSVILLE EB", "2026-05-21T16:35:00+00:00"),
    ])
    westbound = _siri_response([
        _visit("B35", "DYKER WB", "2026-05-21T16:32:00+00:00"),
    ])
    client = _multi_client_with(
        {"302680": eastbound, "302682": westbound},
        now=now,
        stop_ids=["MTA_302680", "MTA_302682"],
        stop_labels={
            "MTA_302680": "4 Av/39 St eastbound",
            "MTA_302682": "4 Av/39 St westbound",
        },
    )
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    # Westbound bus is closer, comes first.
    assert len(arrivals) == 2
    assert arrivals[0].stop_id == "302682"
    assert arrivals[0].stop_label == "4 Av/39 St westbound"
    assert arrivals[0].minutes_from_now < arrivals[1].minutes_from_now
    assert arrivals[1].stop_id == "302680"


@pytest.mark.asyncio
async def test_multi_stop_one_stop_fails_others_succeed():
    """Per-stop failure doesn't kill the whole query — the user still
    gets arrivals from the stops that responded."""
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    ok_response = _siri_response([
        _visit("B35", "X", "2026-05-21T16:35:00+00:00"),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        ref = request.url.params.get("MonitoringRef") or ""
        if ref == "302680":
            return httpx.Response(200, json=ok_response)
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = BusClient(
        stop_ids=["MTA_302680", "MTA_302682"],
        api_key="k", http=http, clock=lambda: now,
    )
    try:
        arrivals = await client.get_arrivals()
    finally:
        await client.aclose()
    # Only the OK stop returned a bus.
    assert len(arrivals) == 1
    assert arrivals[0].stop_id == "302680"


@pytest.mark.asyncio
async def test_multi_stop_caps_at_limit_across_stops():
    """The default limit caps the unioned arrivals across all stops
    — not per stop. Critical for one-sentence voice answers."""
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    many_buses = _siri_response([
        _visit("B35", f"x{m}", f"2026-05-21T16:3{m}:00+00:00")
        for m in range(1, 9)
    ])
    client = _multi_client_with(
        {"302680": many_buses, "302682": many_buses},
        now=now,
        stop_ids=["MTA_302680", "MTA_302682"],
    )
    try:
        arrivals = await client.get_arrivals(limit=4)
    finally:
        await client.aclose()
    assert len(arrivals) == 4


# ---- Cache resilience -----------------------------------------------------
#
# Regression for M2 from the staff-engineer review: pre-fix, a transient
# 503 / timeout / parse error cached an empty list, then the next call
# within the 20 s window returned "no buses" confidently from the cache.
# Post-fix, fetch failures bypass the cache.


@pytest.mark.asyncio
async def test_failed_fetch_does_not_poison_cache():
    """First call: upstream 503 → empty result, no cache entry.
    Second call: upstream recovers → fresh fetch returns arrivals.
    Without the fix, the second call would have hit the cached []
    and returned no buses."""
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    good = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:33:00+00:00"),
    ])
    call_state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_state["n"] += 1
        if call_state["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=good)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = BusClient(
        stop_ids=["MTA_302680"], api_key="k", http=http, clock=lambda: now,
    )
    try:
        first = await client.get_arrivals()
        second = await client.get_arrivals()
    finally:
        await client.aclose()
    assert first == []                  # nothing to show on failure
    assert len(second) == 1             # cache was NOT poisoned
    assert second[0].route == "B35"
    assert call_state["n"] == 2         # upstream was hit twice


@pytest.mark.asyncio
async def test_failed_fetch_serves_stale_cache_when_available():
    """Refinement of the above: if we previously had a good response
    cached, a transient failure on the next refresh should serve the
    stale entry (still within reason) rather than empty. Empty is the
    fallback only when there's nothing cached at all."""
    now = datetime(2026, 5, 21, 16, 30, 0, tzinfo=timezone.utc)
    good = _siri_response([
        _visit("B35", "BROWNSVILLE", "2026-05-21T16:33:00+00:00"),
    ])
    call_state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_state["n"] += 1
        if call_state["n"] == 1:
            return httpx.Response(200, json=good)
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = BusClient(
        stop_ids=["MTA_302680"], api_key="k", http=http, clock=lambda: now,
    )
    try:
        first = await client.get_arrivals()
        # Force a re-fetch by busting the time window — the in-memory
        # cache key is monotonic, so we have to drive a separate stop
        # to verify the staleness fallback. Simpler check: pre-populate
        # the cache via the first call, then bypass the window by
        # accessing the private cache (test-only).
        # Drop the time stamp so the cache appears expired.
        cache_entry = client._cache["302680"]
        client._cache["302680"] = (cache_entry[0] - 100.0, cache_entry[1])
        second = await client.get_arrivals()
    finally:
        await client.aclose()
    assert len(first) == 1
    # Cache stale + upstream failed → serve the stale value rather
    # than empty.
    assert len(second) == 1
    assert second[0].route == "B35"
