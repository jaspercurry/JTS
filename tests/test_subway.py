# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.subway.

Covers:
  - Pure helpers (station loading, direction aliases, feed URL mapping).
  - Pure extraction (`_extract_subwaynow` against fixture data).
  - End-to-end SubwayClient flow with mocked Subway Now responses + a
    fake MTA GTFS-Realtime feed for the fallback path.

v2 response shape:
    {
      station: str,
      directions_queried: list[str],     # ["N"] / ["S"] / ["N", "S"]
      arrivals: list[{
        line: str,                       # "D", "N", "4", etc.
        direction: str,                  # "N" or "S"
        direction_label: str,            # "Manhattan", "Coney Island", ...
        minutes_from_now: int,
      }],
      source: "subwaynow" | "mta-gtfs",
    }
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from pathlib import Path

from jasper.subway import (
    ARRIVAL_LIMIT,
    SubwayClient,
    _GTFSRealtimeFeed,
    _build_aliases,
    _expand_label,
    _load_stations,
    feed_url_for_line,
    normalise_direction,
)


_FIXTURES = Path(__file__).parent / "fixtures"


# --- Pure helpers ----------------------------------------------------------


def test_load_stations_includes_b12_for_9_av():
    """The bundled CSV must include 9 Av (B12) since that's the user's
    home station and the demo target. Verified live against
    api.subwaynow.app."""
    stations = _load_stations()
    assert "B12" in stations
    s = stations["B12"]
    assert s.name == "9 Av"
    assert s.borough == "Bk"
    assert "D" in s.lines
    assert s.north_label == "Manhattan"
    assert s.south_label == "Coney Island"


def test_expand_label_generates_natural_phrases():
    out = _expand_label("Manhattan")
    assert "manhattan" in out
    assert "toward manhattan" in out
    assert "to manhattan" in out
    assert "manhattan-bound" in out
    assert "manhattan bound" in out


def test_expand_label_handles_multiword():
    out = _expand_label("Coney Island")
    assert "coney island" in out
    assert "toward coney island" in out
    assert "coney island-bound" in out


def test_expand_label_empty_returns_empty():
    assert _expand_label("") == []
    assert _expand_label("   ") == []


def test_build_aliases_includes_universal_terms():
    aliases = _build_aliases(None)
    assert aliases["uptown"] == "N"
    assert aliases["downtown"] == "S"
    assert aliases["north"] == "N"
    assert aliases["south"] == "S"


def test_build_aliases_for_b12_maps_manhattan_and_coney():
    """At 9 Av, 'toward Manhattan' should resolve to N and 'toward Coney
    Island' to S. Station-aware aliasing is the whole point."""
    b12 = _load_stations()["B12"]
    aliases = _build_aliases(b12)
    assert aliases["manhattan"] == "N"
    assert aliases["toward manhattan"] == "N"
    assert aliases["manhattan-bound"] == "N"
    assert aliases["coney island"] == "S"
    assert aliases["toward coney island"] == "S"
    assert aliases["coney island-bound"] == "S"


def test_normalise_direction_known_term():
    aliases = {"uptown": "N", "downtown": "S"}
    assert normalise_direction("Uptown", aliases) == "N"
    assert normalise_direction("DOWNTOWN", aliases) == "S"
    assert normalise_direction("  uptown  ", aliases) == "N"


def test_normalise_direction_unknown_returns_none():
    aliases = {"uptown": "N"}
    assert normalise_direction("sideways", aliases) is None
    assert normalise_direction("", aliases) is None


def test_feed_url_for_each_line():
    for line in ["A", "C", "E"]:
        assert feed_url_for_line(line).endswith("nyct%2Fgtfs-ace")
    for line in ["B", "D", "F", "M"]:
        assert feed_url_for_line(line).endswith("nyct%2Fgtfs-bdfm")
    for line in ["N", "Q", "R", "W"]:
        assert feed_url_for_line(line).endswith("nyct%2Fgtfs-nqrw")
    assert feed_url_for_line("L").endswith("nyct%2Fgtfs-l")
    assert feed_url_for_line("G").endswith("nyct%2Fgtfs-g")
    for line in ["1", "4", "7"]:
        assert feed_url_for_line(line).endswith("nyct%2Fgtfs")
        assert "gtfs-" not in feed_url_for_line(line)


def test_feed_url_unknown_line_returns_none():
    assert feed_url_for_line("X") is None
    assert feed_url_for_line("") is None


# --- Subway Now extraction (pure) ------------------------------------------


def _sn_response(
    ref_ts: int,
    north_trips: list[tuple[str, int]] | None = None,
    south_trips: list[tuple[str, int]] | None = None,
) -> dict:
    """Build a minimal Subway Now-shaped response. Each trip tuple is
    (route_id, eta_offset_seconds_from_ref_ts)."""
    def _make(route_id, offset):
        return {
            "route_id": route_id,
            "estimated_current_stop_arrival_time": ref_ts + offset,
            "current_stop_arrival_time": ref_ts + offset,
        }
    return {
        "id": "B12",
        "name": "9 Av",
        "secondary_name": None,
        "timestamp": ref_ts,
        "upcoming_trips": {
            "north": [_make(r, o) for r, o in (north_trips or [])],
            "south": [_make(r, o) for r, o in (south_trips or [])],
        },
    }


def _bound_client(station_id: str = "B12") -> SubwayClient:
    """A bare SubwayClient — useful when we just need its `_extract_subwaynow`
    instance method bound to a station for `direction_label` lookup."""
    return SubwayClient(station_id=station_id)


def test_extract_subwaynow_returns_north_and_south_when_both_directions_queried():
    """Both buckets get walked when directions=['N','S']. This is the
    v2 default for any 'next train' query at a station configured for
    both directions."""
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60), ("N", 7 * 60)],
        south_trips=[("D", 5 * 60)],
    )
    arrivals = _bound_client()._extract_subwaynow(data, ["N", "S"], "")
    # Sorted by ETA, capped at ARRIVAL_LIMIT.
    minutes = [a["minutes_from_now"] for a in arrivals]
    assert minutes == sorted(minutes)
    # All three should fit under the cap.
    assert len(arrivals) == 3
    lines = {a["line"] for a in arrivals}
    assert lines == {"D", "N"}
    # Direction labels come from the bundled CSV for B12.
    labels = {a["direction_label"] for a in arrivals}
    assert labels == {"Manhattan", "Coney Island"}


def test_extract_subwaynow_returns_only_requested_direction():
    """When directions=['N'], south bucket is ignored entirely.
    Critical for the "northbound only" default config."""
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60)],
        south_trips=[("D", 5 * 60), ("N", 7 * 60)],
    )
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "")
    assert len(arrivals) == 1
    assert arrivals[0]["direction"] == "N"
    assert arrivals[0]["line"] == "D"


def test_extract_subwaynow_returns_all_lines_at_station_by_default():
    """v2 behaviour: empty `line_filter` returns every train including
    rerouted ones from other lines (an N at a D station). This is the
    fix for the v1 client filtering out reroutes."""
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60), ("N", 5 * 60)],  # N reroute at B12
    )
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "")
    lines = sorted(a["line"] for a in arrivals)
    assert lines == ["D", "N"]


def test_extract_subwaynow_explicit_line_filter_narrows():
    """User asks 'next D' — only D arrivals should come back even if
    N is also in the bucket. The line filter is a post-fetch narrow."""
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60), ("N", 5 * 60)],
    )
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "D")
    assert len(arrivals) == 1
    assert arrivals[0]["line"] == "D"


def test_extract_subwaynow_skips_past_arrivals():
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", -120), ("D", 8 * 60)],  # one in the past
    )
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "")
    minutes = [a["minutes_from_now"] for a in arrivals]
    assert minutes == [8]


def test_extract_subwaynow_caps_at_arrival_limit():
    ref = 1_777_857_000
    # Build more arrivals than the cap allows.
    many = [("D", m * 60) for m in range(1, ARRIVAL_LIMIT + 3)]
    data = _sn_response(ref_ts=ref, north_trips=many)
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "")
    assert len(arrivals) == ARRIVAL_LIMIT


def test_extract_subwaynow_uses_response_timestamp_not_local_clock():
    """Minutes-from-now are computed against the response's `timestamp`
    field so client clock skew doesn't lie to the user."""
    ref = 1_777_857_000  # arbitrary "server time"
    data = _sn_response(ref_ts=ref, north_trips=[("D", 6 * 60)])
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "")
    assert arrivals[0]["minutes_from_now"] == 6


def test_extract_subwaynow_falls_back_to_scheduled_when_estimated_missing():
    """estimated_current_stop_arrival_time is the smoothed value;
    current_stop_arrival_time is the schedule. Use the smoothed one
    when present, fall through to the schedule otherwise."""
    ref = 1_777_857_000
    trip = {
        "route_id": "D",
        "current_stop_arrival_time": ref + 9 * 60,
        # no estimated_current_stop_arrival_time
    }
    data = {
        "id": "B12", "name": "9 Av", "timestamp": ref,
        "upcoming_trips": {"north": [trip], "south": []},
    }
    arrivals = _bound_client()._extract_subwaynow(data, ["N"], "")
    assert arrivals[0]["minutes_from_now"] == 9


def test_extract_subwaynow_empty_upcoming_trips_returns_empty():
    data = {
        "id": "B12", "name": "9 Av", "timestamp": 0,
        "upcoming_trips": {"north": [], "south": []},
    }
    assert _bound_client()._extract_subwaynow(data, ["N", "S"], "") == []


# --- SubwayClient end-to-end with mocked Subway Now + MTA GTFS-RT ----------


class _FakeStopTimeUpdate:
    def __init__(self, stop_id, arrival):
        self.stop_id = stop_id
        self.arrival = arrival
        self.departure = None


class _FakeTrip:
    def __init__(self, updates):
        self.stop_time_updates = updates


class _FakeFeed:
    def __init__(self, last_generated, trips):
        self.last_generated = last_generated
        self._trips = trips

    def refresh(self):
        pass

    def filter_trips(self, line_id, headed_for_stop_id, underway):
        return self._trips


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _client(
    now,
    *,
    last_generated=None,
    trips=None,
    sn_response=None,
    sn_raises=None,
    station_id="B12",
    default_direction="",
):
    feed = _FakeFeed(last_generated or now, trips or [])

    def http_get(url, params):
        if sn_raises is not None:
            raise sn_raises
        if sn_response is not None:
            return _FakeResponse(sn_response)
        return _FakeResponse({}, status_code=500)

    return SubwayClient(
        station_id=station_id,
        default_direction=default_direction,
        feed_factory=lambda line: feed,
        http_get=http_get,
        clock=lambda: now,
    )


def test_default_direction_uptown_returns_only_north():
    """Configured uptown + bare 'next train' → northbound only.
    This is the user's default at B12."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 4 * 60)],
        south_trips=[("D", 5 * 60)],
    )
    client = _client(now, sn_response=sn, default_direction="uptown")
    result = client.get_arrivals()
    assert result["source"] == "subwaynow"
    assert result["directions_queried"] == ["N"]
    assert len(result["arrivals"]) == 1
    assert result["arrivals"][0]["direction"] == "N"


def test_default_direction_both_returns_both_directions():
    """Configured "both" (empty string in env) + bare query → both
    directions surface in one merged answer."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60)],
        south_trips=[("D", 7 * 60)],
    )
    client = _client(now, sn_response=sn, default_direction="")
    result = client.get_arrivals()
    assert result["directions_queried"] == ["N", "S"]
    directions = sorted(a["direction"] for a in result["arrivals"])
    assert directions == ["N", "S"]


def test_explicit_direction_both_overrides_configured_default():
    """User says 'both directions' → both, regardless of what was
    configured."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60)],
        south_trips=[("D", 7 * 60)],
    )
    client = _client(now, sn_response=sn, default_direction="uptown")
    result = client.get_arrivals(direction="both")
    assert result["directions_queried"] == ["N", "S"]
    assert len(result["arrivals"]) == 2


def test_explicit_southbound_overrides_default_uptown():
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 3 * 60)],
        south_trips=[("D", 7 * 60)],
    )
    client = _client(now, sn_response=sn, default_direction="uptown")
    result = client.get_arrivals(direction="toward Coney Island")
    assert result["directions_queried"] == ["S"]
    assert all(a["direction"] == "S" for a in result["arrivals"])


def test_subwaynow_failure_falls_back_to_mta_gtfs():
    """When Subway Now raises, we silently use MTA GTFS-RT and report the
    source. Fallback only sees the station's CSV-documented lines (no
    reroutes), per the documented degradation."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([_FakeStopTimeUpdate("B12N", now + timedelta(minutes=8))])]
    client = _client(
        now, last_generated=now, trips=trips,
        sn_raises=RuntimeError("subway now down"),
        default_direction="uptown",
    )
    result = client.get_arrivals()
    assert result["source"] == "mta-gtfs"
    assert len(result["arrivals"]) == 1
    assert result["arrivals"][0]["minutes_from_now"] == 8
    assert result["arrivals"][0]["line"] == "D"


def test_mta_fallback_parses_recorded_gtfs_realtime_feed():
    """Parse real MTA wire bytes, not a hand-built protobuf-shaped fake.

    The fixture is a reduced recording of the public BDFM feed captured
    2026-07-11T22:30:05-04:00. It retains the real feed header plus the
    paired TripUpdate/VehiclePosition for a southbound D approaching B12.
    """
    encoded = (_FIXTURES / "mta_bdfm_b12_20260711.pb.b64").read_bytes()
    feed = _GTFSRealtimeFeed.from_bytes(base64.b64decode(encoded))
    assert feed.last_generated == datetime.fromtimestamp(1783825805)

    def subwaynow_down(url, params):
        raise RuntimeError("recorded fallback test")

    client = SubwayClient(
        station_id="B12",
        default_direction="downtown",
        feed_factory=lambda line: feed,
        http_get=subwaynow_down,
        clock=lambda: feed.last_generated,
    )
    result = client.get_arrivals(line="D")

    assert result["source"] == "mta-gtfs"
    assert result["arrivals"] == [
        {
            "line": "D",
            "direction": "S",
            "direction_label": "Coney Island",
            "minutes_from_now": 38,
        }
    ]


def test_subwaynow_5xx_falls_back():
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([_FakeStopTimeUpdate("B12N", now + timedelta(minutes=4))])]
    client = _client(
        now, last_generated=now, trips=trips,
        sn_response=None,  # falls through to 500
        default_direction="uptown",
    )
    result = client.get_arrivals()
    assert result["source"] == "mta-gtfs"


def test_both_paths_fail_returns_error():
    """Subway Now down + fallback feed stale → surface error rather
    than confidently-wrong arrivals."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    very_old = now - timedelta(minutes=10)
    trips = [_FakeTrip([_FakeStopTimeUpdate("B12N", now + timedelta(minutes=5))])]
    client = _client(
        now, last_generated=very_old, trips=trips,
        sn_raises=RuntimeError("subway now down"),
    )
    result = client.get_arrivals()
    assert "error" in result


def test_unknown_station_returns_error_without_fetching():
    now = datetime(2024, 1, 1, 12, 0, 0)
    fetched: list[str] = []
    def http_get(url, params):
        fetched.append(url)
        return _FakeResponse({})
    client = SubwayClient(
        station_id="ZZZ",
        feed_factory=lambda line: _FakeFeed(now, []),
        http_get=http_get,
        clock=lambda: now,
    )
    result = client.get_arrivals()
    assert "error" in result
    assert "ZZZ" in result["error"]
    assert fetched == []


def test_unknown_line_returns_error_without_fetching():
    now = datetime(2024, 1, 1, 12, 0, 0)
    fetched: list[str] = []
    def http_get(url, params):
        fetched.append(url)
        return _FakeResponse({})
    client = SubwayClient(
        station_id="B12",
        feed_factory=lambda line: _FakeFeed(now, []),
        http_get=http_get,
        clock=lambda: now,
    )
    result = client.get_arrivals(line="X")
    assert "error" in result
    assert "X" in result["error"]
    assert fetched == []


def test_subwaynow_response_cached_within_window():
    """Repeat queries to the same station within CACHE_WINDOW_SEC hit
    the network exactly once."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(ref_ts=ref_ts, north_trips=[("D", 5 * 60)])
    fetched: list[str] = []
    def http_get(url, params):
        fetched.append(url)
        return _FakeResponse(sn)
    client = SubwayClient(
        station_id="B12",
        default_direction="uptown",
        feed_factory=lambda line: _FakeFeed(now, []),
        http_get=http_get,
        clock=lambda: now,
    )
    client.get_arrivals()
    client.get_arrivals()
    client.get_arrivals()
    assert len(fetched) == 1


def test_unrecognised_direction_returns_helpful_error():
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now, default_direction="uptown")
    result = client.get_arrivals(direction="sideways")
    assert "error" in result
    assert "Manhattan" in result["error"]
    assert "Coney Island" in result["error"]


def test_bare_question_returns_all_lines_in_default_direction():
    """The user's primary use case: 'next train' with no specifics.
    Default direction (configured uptown) + every line at the station
    (so a rerouted N at the D station surfaces too)."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref_ts,
        north_trips=[("D", 4 * 60), ("N", 7 * 60)],
    )
    client = _client(now, sn_response=sn, default_direction="uptown")
    result = client.get_arrivals()
    assert "error" not in result
    assert result["directions_queried"] == ["N"]
    minutes = sorted(a["minutes_from_now"] for a in result["arrivals"])
    assert minutes == [4, 7]
    lines = sorted(a["line"] for a in result["arrivals"])
    assert lines == ["D", "N"]


def test_explicit_line_at_multiline_query_narrows():
    """User asks 'next D' explicitly — only D arrivals come back."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref_ts,
        north_trips=[("D", 4 * 60), ("N", 7 * 60)],
    )
    client = _client(now, sn_response=sn, default_direction="uptown")
    result = client.get_arrivals(line="D")
    lines = [a["line"] for a in result["arrivals"]]
    assert lines == ["D"]
