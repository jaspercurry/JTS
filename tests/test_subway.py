from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jasper.subway import (
    LINE_TO_FEED,
    StationInfo,
    SubwayClient,
    _build_aliases,
    _expand_label,
    _load_stations,
    arrivals_in_minutes,
    feed_url_for_line,
    normalise_direction,
)


# --- pure helpers ---


def test_load_stations_includes_b16():
    """The bundled CSV must include 9 Av (B16) since that's the user's
    home station and the demo target. If this regresses (someone deletes
    the row), the daemon would fail to start with subway enabled."""
    stations = _load_stations()
    assert "B16" in stations
    s = stations["B16"]
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


def test_build_aliases_for_b16_maps_manhattan_and_coney():
    """At 9 Av, 'toward Manhattan' should resolve to N and 'toward Coney
    Island' to S. This is the whole point of station-aware aliasing."""
    b16 = _load_stations()["B16"]
    aliases = _build_aliases(b16)
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
    # Numbered lines use the unsuffixed feed.
    for line in ["1", "4", "7"]:
        assert feed_url_for_line(line).endswith("nyct%2Fgtfs")
        assert "gtfs-" not in feed_url_for_line(line)


def test_feed_url_unknown_line_returns_none():
    assert feed_url_for_line("X") is None
    assert feed_url_for_line("") is None


def test_arrivals_in_minutes_filters_past_and_sorts():
    now = datetime(2024, 1, 1, 12, 0, 0)
    times = [
        now - timedelta(minutes=2),    # past — drop
        now + timedelta(minutes=12),
        now + timedelta(minutes=5),
        now + timedelta(minutes=19),
        now + timedelta(seconds=30),   # rounds to 0 or 1, kept
    ]
    out = arrivals_in_minutes(times, now, limit=3)
    # Sorted ascending, capped at 3.
    assert out == sorted(out)
    assert len(out) == 3
    assert out[0] >= 0


def test_arrivals_in_minutes_empty_when_no_future():
    now = datetime(2024, 1, 1, 12, 0, 0)
    times = [now - timedelta(minutes=5), now - timedelta(minutes=1)]
    assert arrivals_in_minutes(times, now) == []


# --- SubwayClient with mocked feed ---


class _FakeStopTimeUpdate:
    def __init__(self, stop_id, arrival):
        self.stop_id = stop_id
        self.arrival = arrival
        self.departure = None


class _FakeTrip:
    def __init__(self, updates):
        self.stop_time_updates = updates


class _FakeFeed:
    """Stand-in for nyct_gtfs.NYCTFeed."""
    def __init__(self, line: str, last_generated: datetime, trips: list[_FakeTrip]):
        self.line = line
        self.last_generated = last_generated
        self._trips = trips

    def refresh(self):
        # No-op; tests preconfigure the trips.
        pass

    def filter_trips(self, line_id, headed_for_stop_id, underway):
        # Tests pre-filter their fake trips so we just return them all.
        return self._trips


def _client(now, last_generated, trips, station_id="B16",
            default_direction="uptown", lines=None):
    feed = _FakeFeed("D", last_generated, trips)
    return SubwayClient(
        station_id=station_id,
        default_direction=default_direction,
        configured_lines=lines,
        feed_factory=lambda line: feed,
        clock=lambda: now,
    )


def test_get_arrivals_happy_path_b16_uptown_d():
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([
        _FakeStopTimeUpdate("B16N", now + timedelta(minutes=5)),
    ]), _FakeTrip([
        _FakeStopTimeUpdate("B16N", now + timedelta(minutes=12)),
    ]), _FakeTrip([
        _FakeStopTimeUpdate("B16N", now + timedelta(minutes=19)),
    ])]
    client = _client(now, now, trips)
    result = client.get_arrivals("D")
    assert "error" not in result
    assert result["line"] == "D"
    assert result["direction"] == "N"
    assert result["direction_label"] == "Manhattan"
    assert result["station"] == "9 Av"
    assert result["next_arrivals_minutes"] == [5, 12, 19]


def test_get_arrivals_toward_coney_resolves_south():
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([
        _FakeStopTimeUpdate("B16S", now + timedelta(minutes=7)),
    ])]
    client = _client(now, now, trips)
    result = client.get_arrivals("D", direction="toward Coney")
    # 'toward Coney' isn't a built-in alias but Coney Island is in the
    # station's south_label so 'toward coney island' would resolve. 'toward
    # Coney' alone isn't expanded — verifies the case where unrecognised
    # direction returns an error.
    assert "error" in result
    assert "didn't recognise" in result["error"]


def test_get_arrivals_toward_coney_island_full_name_works():
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([
        _FakeStopTimeUpdate("B16S", now + timedelta(minutes=7)),
    ])]
    client = _client(now, now, trips)
    result = client.get_arrivals("D", direction="toward Coney Island")
    assert result.get("direction") == "S"
    assert result.get("direction_label") == "Coney Island"
    assert result.get("next_arrivals_minutes") == [7]


def test_get_arrivals_filters_to_correct_platform():
    """Trips with the wrong platform stop_id (e.g. B16S when we want N)
    must be filtered out — proves we're matching on stop_id, not just
    blindly returning all trip arrivals."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([
        _FakeStopTimeUpdate("B16S", now + timedelta(minutes=3)),  # wrong dir
        _FakeStopTimeUpdate("B16N", now + timedelta(minutes=8)),  # right
    ])]
    client = _client(now, now, trips)
    result = client.get_arrivals("D", direction="uptown")
    assert result["next_arrivals_minutes"] == [8]


def test_get_arrivals_no_upcoming_trains_returns_empty_list():
    now = datetime(2024, 1, 1, 12, 0, 0)
    trips = [_FakeTrip([
        _FakeStopTimeUpdate("B16N", now - timedelta(minutes=2)),  # past
    ])]
    client = _client(now, now, trips)
    result = client.get_arrivals("D")
    assert result["next_arrivals_minutes"] == []


def test_get_arrivals_stale_feed_returns_error():
    now = datetime(2024, 1, 1, 12, 0, 0)
    very_old = now - timedelta(minutes=10)
    trips = [_FakeTrip([
        _FakeStopTimeUpdate("B16N", now + timedelta(minutes=5)),
    ])]
    client = _client(now, very_old, trips)
    result = client.get_arrivals("D")
    assert "error" in result
    assert "stale" in result["error"]


def test_get_arrivals_unknown_line_returns_error():
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now, now, [])
    result = client.get_arrivals("X")
    assert "error" in result
    assert "unknown" in result["error"].lower()


def test_get_arrivals_line_not_at_station_returns_error():
    """If user configures lines=['D'] (which is correct for B16), asking
    for the F train should return a friendly error rather than a confusing
    'no trains found.'"""
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now, now, [], lines=["D"])
    result = client.get_arrivals("F")
    assert "error" in result
    assert "doesn't stop" in result["error"]


def test_get_arrivals_unknown_station_returns_error():
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now, now, [], station_id="ZZZ")
    result = client.get_arrivals("D")
    assert "error" in result
    assert "ZZZ" in result["error"]


def test_get_arrivals_unrecognised_direction_returns_helpful_error():
    now = datetime(2024, 1, 1, 12, 0, 0)
    client = _client(now, now, [])
    result = client.get_arrivals("D", direction="sideways")
    assert "error" in result
    # Error should mention the station's actual labels so user knows what
    # to say next time.
    assert "Manhattan" in result["error"]
    assert "Coney Island" in result["error"]
