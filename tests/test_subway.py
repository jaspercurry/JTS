from __future__ import annotations

import json
from datetime import datetime, timedelta

from jasper.subway import (
    LINE_TO_FEED,
    SubwayClient,
    _build_aliases,
    _expand_label,
    _load_stations,
    arrivals_in_minutes,
    feed_url_for_line,
    normalise_direction,
)


# --- pure helpers ---


def test_load_stations_includes_b12_for_9_av():
    """The bundled CSV must include 9 Av (B12) since that's the user's
    home station and the demo target. B12 is the West End line stop in
    Sunset Park, verified live against api.subwaynow.app — earlier
    research agents incorrectly suggested B15/B16 for this station."""
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
    assert aliases["uptown"] == "N"
    assert aliases["downtown"] == "S"
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


def test_arrivals_in_minutes_filters_past_and_sorts():
    now = datetime(2024, 1, 1, 12, 0, 0)
    times = [
        now - timedelta(minutes=2),
        now + timedelta(minutes=12),
        now + timedelta(minutes=5),
        now + timedelta(minutes=19),
        now + timedelta(seconds=30),
    ]
    out = arrivals_in_minutes(times, now, limit=3)
    assert out == sorted(out)
    assert len(out) == 3
    assert out[0] >= 0


def test_arrivals_in_minutes_empty_when_no_future():
    now = datetime(2024, 1, 1, 12, 0, 0)
    times = [now - timedelta(minutes=5), now - timedelta(minutes=1)]
    assert arrivals_in_minutes(times, now) == []


# --- Subway Now extraction (pure) ---


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


def test_extract_subwaynow_filters_by_route_and_direction():
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 5 * 60), ("D", 12 * 60), ("N", 4 * 60)],  # N filtered out
        south_trips=[("D", 7 * 60)],
    )
    out = SubwayClient._extract_subwaynow(data, "D", "N")
    assert out == [5, 12]


def test_extract_subwaynow_skips_past_arrivals():
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", -120), ("D", 8 * 60)],  # one in the past
    )
    assert SubwayClient._extract_subwaynow(data, "D", "N") == [8]


def test_extract_subwaynow_caps_at_three():
    ref = 1_777_857_000
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", m * 60) for m in (3, 9, 15, 22, 30)],
    )
    out = SubwayClient._extract_subwaynow(data, "D", "N")
    assert out == [3, 9, 15]


def test_extract_subwaynow_uses_response_timestamp_not_local_clock():
    """If the response timestamp differs significantly from local time,
    the minutes-from-now must be relative to the response timestamp.
    Otherwise client clock skew lies to the user."""
    ref = 1_777_857_000  # arbitrary "server time"
    data = _sn_response(
        ref_ts=ref,
        north_trips=[("D", 6 * 60)],
    )
    # Even though _extract_subwaynow doesn't see local time, this asserts
    # the math is anchored to the response's `timestamp` field.
    assert SubwayClient._extract_subwaynow(data, "D", "N") == [6]


def test_extract_subwaynow_handles_missing_estimated_falls_back_to_scheduled():
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
    assert SubwayClient._extract_subwaynow(data, "D", "N") == [9]


def test_extract_subwaynow_handles_empty_upcoming_trips():
    data = {"id": "B12", "name": "9 Av", "timestamp": 0,
            "upcoming_trips": {"north": [], "south": []}}
    assert SubwayClient._extract_subwaynow(data, "D", "N") == []


# --- SubwayClient end-to-end with mocked Subway Now + nyct-gtfs ---


class _FakeStopTimeUpdate:
    def __init__(self, stop_id, arrival):
        self.stop_id = stop_id
        self.arrival = arrival
        self.departure = None


class _FakeTrip:
    def __init__(self, updates):
        self.stop_time_updates = updates


class _FakeFeed:
    def __init__(self, line, last_generated, trips):
        self.line = line
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
    last_generated=None,
    trips=None,
    sn_response=None,
    sn_raises=None,
    station_id="B12",
    default_direction="uptown",
    lines=None,
):
    feed = _FakeFeed("D", last_generated or now, trips or [])

    def http_get(url, params):
        if sn_raises is not None:
            raise sn_raises
        if sn_response is not None:
            return _FakeResponse(sn_response)
        return _FakeResponse({}, status_code=500)

    return SubwayClient(
        station_id=station_id,
        default_direction=default_direction,
        configured_lines=lines,
        feed_factory=lambda line: feed,
        http_get=http_get,
        clock=lambda: now,
    )


def test_subwaynow_happy_path_returns_smoothed_arrivals():
    """Subway Now responds successfully — should be used and reported
    as the source. nyct-gtfs feed is not consulted."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref_ts,
        north_trips=[("D", 5 * 60), ("D", 12 * 60), ("D", 19 * 60)],
    )
    client = _client(now, sn_response=sn)
    result = client.get_arrivals("D")
    assert result["next_arrivals_minutes"] == [5, 12, 19]
    assert result["source"] == "subwaynow"
    assert result["station"] == "9 Av"
    assert result["direction_label"] == "Manhattan"


def test_subwaynow_failure_falls_back_to_nyct_gtfs():
    """When Subway Now raises, we should silently use nyct-gtfs and
    report the source. End-user gets arrivals either way."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([_FakeStopTimeUpdate("B12N", now + timedelta(minutes=8))])]
    client = _client(
        now, last_generated=now, trips=trips,
        sn_raises=RuntimeError("subway now down"),
    )
    result = client.get_arrivals("D")
    assert result["source"] == "nyct-gtfs"
    assert result["next_arrivals_minutes"] == [8]


def test_subwaynow_5xx_falls_back():
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([_FakeStopTimeUpdate("B12N", now + timedelta(minutes=4))])]
    client = _client(now, last_generated=now, trips=trips, sn_response=None)
    # sn_response=None makes _FakeResponse return 500
    result = client.get_arrivals("D")
    assert result["source"] == "nyct-gtfs"


def test_both_paths_fail_returns_error():
    """If Subway Now fails AND nyct-gtfs feed is stale, surface an error
    rather than confidently-wrong arrivals."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    very_old = now - timedelta(minutes=10)
    trips = [_FakeTrip([_FakeStopTimeUpdate("B12N", now + timedelta(minutes=5))])]
    client = _client(
        now, last_generated=very_old, trips=trips,
        sn_raises=RuntimeError("subway now down"),
    )
    result = client.get_arrivals("D")
    assert "error" in result


def test_subwaynow_path_filters_to_correct_direction():
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(
        ref_ts=ref_ts,
        north_trips=[("D", 3 * 60)],
        south_trips=[("D", 7 * 60)],
    )
    client = _client(now, sn_response=sn)
    n_result = client.get_arrivals("D", direction="uptown")
    s_result = client.get_arrivals("D", direction="toward Coney Island")
    assert n_result["next_arrivals_minutes"] == [3]
    assert n_result["direction"] == "N"
    assert s_result["next_arrivals_minutes"] == [7]
    assert s_result["direction"] == "S"


def test_validation_errors_short_circuit_before_either_path():
    """A line-not-at-station error must be returned immediately without
    triggering an HTTP fetch — saves time and avoids leaking station
    queries to the third-party API for invalid requests."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    fetched = []
    def http_get(url, params):
        fetched.append(url)
        return _FakeResponse({})
    client = SubwayClient(
        station_id="B12",
        configured_lines=["D"],
        feed_factory=lambda line: _FakeFeed("D", now, []),
        http_get=http_get,
        clock=lambda: now,
    )
    result = client.get_arrivals("F")  # F doesn't stop at 9 Av
    assert "error" in result
    assert fetched == []  # no HTTP call was made


def test_unknown_station_returns_error_without_fetching():
    now = datetime(2024, 1, 1, 12, 0, 0)
    fetched = []
    def http_get(url, params):
        fetched.append(url)
        return _FakeResponse({})
    client = SubwayClient(
        station_id="ZZZ",
        feed_factory=lambda line: _FakeFeed("D", now, []),
        http_get=http_get,
        clock=lambda: now,
    )
    result = client.get_arrivals("D")
    assert "error" in result
    assert "ZZZ" in result["error"]
    assert fetched == []


def test_subwaynow_response_cached_within_window():
    """Repeat queries to the same station within CACHE_WINDOW_SEC should
    only hit the network once."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(ref_ts=ref_ts, north_trips=[("D", 5 * 60)])
    fetched = []
    def http_get(url, params):
        fetched.append(url)
        return _FakeResponse(sn)
    client = SubwayClient(
        station_id="B12",
        feed_factory=lambda line: _FakeFeed("D", now, []),
        http_get=http_get,
        clock=lambda: now,
    )
    client.get_arrivals("D")
    client.get_arrivals("D")
    client.get_arrivals("D")
    assert len(fetched) == 1


def test_unrecognised_direction_returns_helpful_error():
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now)
    result = client.get_arrivals("D", direction="sideways")
    assert "error" in result
    assert "Manhattan" in result["error"]
    assert "Coney Island" in result["error"]


def test_bare_question_at_single_line_station_uses_default_line_and_direction():
    """'Hey Jarvis, when's the next train?' → tool gets ('', '') → at a
    single-line station with default direction 'uptown', resolves to
    the only line + N. Critical for the user's 90% common case."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(ref_ts=ref_ts, north_trips=[("D", 4 * 60)])
    client = _client(now, sn_response=sn, lines=["D"])
    result = client.get_arrivals("", "")
    assert "error" not in result
    assert result["line"] == "D"
    assert result["direction"] == "N"
    assert result["next_arrivals_minutes"] == [4]


def test_bare_question_at_multi_line_station_asks_which_line():
    """At a station with multiple lines, an empty `line` should produce a
    helpful error listing the served lines rather than picking one
    arbitrarily."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now, lines=["B", "D", "N", "Q", "R"])
    result = client.get_arrivals("", "")
    assert "error" in result
    assert "which line" in result["error"].lower()
    for served in ["B", "D", "N", "Q", "R"]:
        assert served in result["error"]


def test_explicit_line_still_works_at_single_line_station():
    """Sanity: providing the line explicitly should give the same answer
    as omitting it at a single-line station."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    ref_ts = int(now.timestamp())
    sn = _sn_response(ref_ts=ref_ts, north_trips=[("D", 6 * 60)])
    client = _client(now, sn_response=sn, lines=["D"])
    bare = client.get_arrivals("", "")
    explicit = client.get_arrivals("D", "uptown")
    assert bare["next_arrivals_minutes"] == explicit["next_arrivals_minutes"]
    assert bare["line"] == explicit["line"]
