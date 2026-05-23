"""Tests for jasper.citibike and jasper.transit.providers.citibike.

All hardware-free; httpx network calls mocked via MockTransport. The
module-level GBFS cache is cleared between every test by the autouse
fixture below so cache hits in one test don't bleed into another.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from jasper.citibike import (
    INFO_TTL_SECONDS,
    STATION_INFO_URL,
    STATION_STATUS_URL,
    STATUS_TTL_SECONDS,
    CitiBikeClient,
    StationStatus,
    clear_cache,
    fetch_feed,
    format_saved_stations,
    normalize_station_name,
    parse_saved_stations,
)
from jasper.transit.base import TransitError, TransitProvider
from jasper.transit.providers.citibike import CITIBIKE_BBOX, PROVIDER


@pytest.fixture(autouse=True)
def _clear_gbfs_cache():
    clear_cache()
    yield
    clear_cache()


# --- Test helpers -----------------------------------------------------


def _station_info(
    sid: str, name: str, lat: float, lon: float, **extra: Any
) -> dict:
    base: dict[str, Any] = {
        "station_id": sid, "name": name, "lat": lat, "lon": lon,
        "capacity": 30,
    }
    base.update(extra)
    return base


def _station_status(
    sid: str,
    *,
    bikes: int = 5,
    ebikes: int = 2,
    docks: int = 10,
    renting: int = 1,
    installed: int = 1,
    age_seconds: int = 20,
) -> dict:
    return {
        "station_id": sid,
        "num_bikes_available": bikes,
        "num_ebikes_available": ebikes,
        "num_docks_available": docks,
        "is_renting": renting,
        "is_returning": 1,
        "is_installed": installed,
        "last_reported": int(time.time()) - age_seconds,
    }


def _gbfs_envelope(*stations: dict) -> dict:
    return {"data": {"stations": list(stations)}}


def _mock_client(responses: dict[str, dict | int]) -> httpx.Client:
    """httpx.Client backed by MockTransport. Values may be JSON dicts
    (200 + body) or ints (returned as HTTP status with empty body)."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        v = responses.get(url)
        if isinstance(v, dict):
            return httpx.Response(200, json=v)
        if isinstance(v, int):
            return httpx.Response(v)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- parse_saved_stations / format_saved_stations ---------------------


def test_parse_empty_returns_empty():
    assert parse_saved_stations("") == []
    assert parse_saved_stations("   ") == []


def test_parse_happy_path():
    got = parse_saved_stations("abc|9 Av,def|Atlantic")
    assert got == [("abc", "9 Av"), ("def", "Atlantic")]


def test_parse_tolerates_whitespace():
    got = parse_saved_stations("  abc | 9 Av  ,  def | Atlantic  ")
    assert got == [("abc", "9 Av"), ("def", "Atlantic")]


def test_parse_bare_id_uses_id_as_label():
    got = parse_saved_stations("abc-uuid,def|Atlantic")
    assert got == [("abc-uuid", "abc-uuid"), ("def", "Atlantic")]


def test_parse_skips_empty_id():
    got = parse_saved_stations("abc|9 Av,|orphan_label,def|D")
    assert got == [("abc", "9 Av"), ("def", "D")]


def test_parse_empty_label_falls_back_to_id():
    got = parse_saved_stations("abc|")
    assert got == [("abc", "abc")]


def test_format_round_trip():
    saved = [("abc", "9 Av"), ("def", "Atlantic & Smith")]
    assert parse_saved_stations(format_saved_stations(saved)) == saved


# --- normalize_station_name: speech-friendly label expansion ----------


@pytest.mark.parametrize("raw,expected", [
    # Basic suffix expansion + ordinalize.
    ("9 Ave & 41 St", "9th Avenue and 41st Street"),
    ("9 Av & 41 St", "9th Avenue and 41st Street"),
    ("Broadway & W 41 St", "Broadway and West 41st Street"),
    ("62 Dr & 110 St", "62nd Drive and 110th Street"),
    ("4 Ave & E 12 St", "4th Avenue and East 12th Street"),
    # Compass directions both sides.
    ("W 35 St & 8 Ave", "West 35th Street and 8th Avenue"),
    ("E 22 St & Broadway", "East 22nd Street and Broadway"),
    # "St" → "Saint" when followed by a capitalized proper name.
    ("St James Pl", "Saint James Place"),
    ("St Marks Ave", "Saint Marks Avenue"),
    ("St Nicholas Ave", "Saint Nicholas Avenue"),
    # Names already containing "Saint" stay sensible.
    ("Saint Nicholas Ave", "Saint Nicholas Avenue"),
    # Avenues lettered A-Z (not directions) stay as letters.
    ("Avenue A", "Avenue A"),
    ("Av X & E 5 St", "Avenue X and East 5th Street"),
    ("Av W & Brighton", "Avenue W and Brighton"),  # Brooklyn's actual "Av W"
    # Ordinal edge cases.
    ("1 Ave", "1st Avenue"),
    ("2 Ave", "2nd Avenue"),
    ("3 Ave", "3rd Avenue"),
    ("11 St", "11th Street"),
    ("12 St", "12th Street"),
    ("13 St", "13th Street"),
    ("21 St", "21st Street"),
    ("22 Ave", "22nd Avenue"),
    ("101 St", "101st Street"),
    ("111 St", "111th Street"),
    ("127 St", "127th Street"),
    # Other abbreviations.
    ("Eastern Pkwy & Franklin Ave", "Eastern Parkway and Franklin Avenue"),
    ("Atlantic Ave & Smith St", "Atlantic Avenue and Smith Street"),
    ("Union Sq W & 14 St", "Union Square West and 14th Street"),
    ("Linden St & Knickerbocker Ave", "Linden Street and Knickerbocker Avenue"),
    # Trailing periods (operator-typed) tolerated.
    ("9 Av. & 41 St.", "9th Avenue and 41st Street"),
    # No abbreviations → passthrough (modulo double-space cleanup).
    ("Broadway", "Broadway"),
    ("Empire Stores", "Empire Stores"),
    # Empty / whitespace.
    ("", ""),
    ("   ", ""),
])
def test_normalize_station_name(raw: str, expected: str):
    assert normalize_station_name(raw) == expected


def test_normalize_idempotent_on_already_normalized():
    """A second pass through the normalizer shouldn't change anything.
    Catches regressions where e.g. ordinalize matches '41st Street'
    and turns it into '41stst Street' or similar."""
    samples = [
        "9th Avenue and 41st Street",
        "West 35th Street and 8th Avenue",
        "Saint James Place",
        "Avenue A",
        "Broadway",
    ]
    for s in samples:
        assert normalize_station_name(s) == s, f"changed: {s!r}"


# --- Filter matching against normalized + raw labels ------------------


def test_get_status_filter_matches_normalized_form(monkeypatch):
    """User says 'the 41st Street one' — filter is 'the 41st Street'
    and we should match against the normalized label even though the
    saved label is raw '9 Ave & 41 St'."""
    monkeypatch.setattr(
        "jasper.citibike.fetch_feed",
        lambda url, ttl, **kw: _gbfs_envelope(
            _station_info("abc", "9 Ave & 41 St", 40.65, -74.01),
        ) if url == STATION_INFO_URL else _gbfs_envelope(_station_status("abc")),
    )
    c = CitiBikeClient(saved_stations=[("abc", "9 Ave & 41 St")])
    out = c.get_status(station_filter="41st Street")
    assert len(out) == 1
    assert out[0].station_id == "abc"


def test_get_status_filter_matches_raw_form(monkeypatch):
    """User (or LLM passing the spoken phrase verbatim) says '9 Ave' —
    raw substring matches the saved label even though normalization
    would turn it into '9th Avenue'."""
    monkeypatch.setattr(
        "jasper.citibike.fetch_feed",
        lambda url, ttl, **kw: _gbfs_envelope(
            _station_info("abc", "9 Ave & 41 St", 40.65, -74.01),
        ) if url == STATION_INFO_URL else _gbfs_envelope(_station_status("abc")),
    )
    c = CitiBikeClient(saved_stations=[("abc", "9 Ave & 41 St")])
    assert len(c.get_status(station_filter="9 Ave")) == 1


def test_get_status_filter_matches_ordinal_only(monkeypatch):
    """User says '41st' alone — must match. Normalization doesn't
    add or remove '41st' from either side, so substring works."""
    monkeypatch.setattr(
        "jasper.citibike.fetch_feed",
        lambda url, ttl, **kw: _gbfs_envelope(
            _station_info("abc", "9 Ave & 41 St", 40.65, -74.01),
        ) if url == STATION_INFO_URL else _gbfs_envelope(_station_status("abc")),
    )
    c = CitiBikeClient(saved_stations=[("abc", "9 Ave & 41 St")])
    assert len(c.get_status(station_filter="41st")) == 1


def test_as_dict_label_is_normalized():
    """Verify the dict (what the LLM sees) uses normalized labels even
    though the dataclass field is raw."""
    s = StationStatus(
        station_id="abc", label="9 Ave & 41 St",
        classic_bikes=5, ebikes=2, docks=10,
        status="ok", last_reported_age_seconds=20,
    )
    d = s.as_dict()
    assert d["label"] == "9th Avenue and 41st Street"
    # Raw still accessible on the dataclass for non-speech consumers.
    assert s.label == "9 Ave & 41 St"


# --- fetch_feed: caching + stale-on-error -----------------------------


def test_fetch_feed_caches_within_ttl():
    payload = _gbfs_envelope()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=payload)
    client = httpx.Client(transport=httpx.MockTransport(handler))

    a = fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=client)
    b = fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=client)
    assert a == payload
    assert b == payload
    assert calls["n"] == 1, "second call should be cache-served"


def test_fetch_feed_zero_ttl_always_refetches():
    payload = _gbfs_envelope()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=payload)
    client = httpx.Client(transport=httpx.MockTransport(handler))

    fetch_feed(STATION_STATUS_URL, 0.0, client=client)
    fetch_feed(STATION_STATUS_URL, 0.0, client=client)
    assert calls["n"] == 2


def test_fetch_feed_separate_urls_cache_independently():
    info_payload = _gbfs_envelope(_station_info("abc", "X", 40.7, -74.0))
    status_payload = _gbfs_envelope(_station_status("abc"))
    client = _mock_client({
        STATION_INFO_URL: info_payload,
        STATION_STATUS_URL: status_payload,
    })
    assert fetch_feed(STATION_INFO_URL, INFO_TTL_SECONDS, client=client) == info_payload
    assert fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=client) == status_payload


def test_fetch_feed_raises_when_no_cache_and_network_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(TransitError, match="GBFS request failed"):
        fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=client)


def test_fetch_feed_serves_stale_on_error():
    payload = _gbfs_envelope(_station_info("abc", "X", 40.7, -74.0))
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json=payload)
    client = httpx.Client(transport=httpx.MockTransport(handler))

    # First call populates cache
    fetch_feed(STATION_STATUS_URL, 0.0, client=client)
    # Subsequent call: TTL=0 forces refetch but network is now failing.
    # Stale cached entry should be served.
    state["fail"] = True
    served = fetch_feed(STATION_STATUS_URL, 0.0, client=client)
    assert served == payload


def test_fetch_feed_parse_error_raises_when_no_cache():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="this is not json {[")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(TransitError):
        fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=client)


def test_fetch_feed_5xx_raises_when_no_cache():
    client = _mock_client({STATION_STATUS_URL: 503})
    with pytest.raises(TransitError):
        fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=client)


# --- CitiBikeClient: gating + status ----------------------------------


def test_client_disabled_when_no_saved_stations():
    c = CitiBikeClient(saved_stations=[])
    assert c.enabled is False


def test_client_enabled_with_one_station():
    c = CitiBikeClient(saved_stations=[("abc", "9 Av")])
    assert c.enabled is True


def test_client_ebike_only_default_off():
    c = CitiBikeClient(saved_stations=[("abc", "9 Av")])
    assert c.ebike_only is False


def test_client_ebike_only_passthrough():
    c = CitiBikeClient(saved_stations=[("abc", "9 Av")], ebike_only=True)
    assert c.ebike_only is True


def test_get_status_returns_per_saved_station_in_order():
    info = _gbfs_envelope(
        _station_info("abc", "9 Av & 41 St", 40.65, -74.01),
        _station_info("def", "Atlantic & Smith", 40.69, -73.99),
    )
    status = _gbfs_envelope(
        _station_status("abc", bikes=7, ebikes=3, docks=25),
        _station_status("def", bikes=2, ebikes=0, docks=12),
    )
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(
        saved_stations=[("abc", "9 Av"), ("def", "Atlantic")],
        http=http,
    )
    out = c.get_status()
    assert [s.label for s in out] == ["9 Av", "Atlantic"]
    abc = out[0]
    assert abc.classic_bikes == 4  # 7 total - 3 ebike
    assert abc.ebikes == 3
    assert abc.docks == 25
    assert abc.status == "ok"
    assert abc.last_reported_age_seconds >= 15  # we set 20 in helper


def test_get_status_flags_missing_station():
    info = _gbfs_envelope(_station_info("abc", "9 Av", 40.65, -74.01))
    status = _gbfs_envelope(_station_status("abc"))
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(
        saved_stations=[("abc", "9 Av"), ("ghost", "Gone Station")],
        http=http,
    )
    out = c.get_status()
    by_label = {s.label: s for s in out}
    assert by_label["9 Av"].status == "ok"
    assert by_label["Gone Station"].status == "missing"
    assert by_label["Gone Station"].classic_bikes == 0
    assert by_label["Gone Station"].ebikes == 0
    assert by_label["Gone Station"].docks == 0


def test_get_status_flags_offline_when_not_renting():
    info = _gbfs_envelope(_station_info("abc", "9 Av", 40.65, -74.01))
    status = _gbfs_envelope(_station_status("abc", renting=0))
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(saved_stations=[("abc", "9 Av")], http=http)
    assert c.get_status()[0].status == "offline"


def test_get_status_flags_offline_when_not_installed():
    info = _gbfs_envelope(_station_info("abc", "9 Av", 40.65, -74.01))
    status = _gbfs_envelope(_station_status("abc", installed=0))
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(saved_stations=[("abc", "9 Av")], http=http)
    assert c.get_status()[0].status == "offline"


def test_get_status_filters_case_insensitive_substring():
    info = _gbfs_envelope(
        _station_info("a", "9 Av", 40.65, -74.01),
        _station_info("b", "Atlantic Av", 40.69, -73.99),
    )
    status = _gbfs_envelope(_station_status("a"), _station_status("b"))
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(
        saved_stations=[("a", "9 Av"), ("b", "Atlantic")],
        http=http,
    )
    out = c.get_status(station_filter="9 AV")
    assert [s.label for s in out] == ["9 Av"]
    out2 = c.get_status(station_filter="atl")
    assert [s.label for s in out2] == ["Atlantic"]


def test_get_status_no_match_returns_empty():
    info = _gbfs_envelope(_station_info("a", "9 Av", 40.65, -74.01))
    status = _gbfs_envelope(_station_status("a"))
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(saved_stations=[("a", "9 Av")], http=http)
    assert c.get_status(station_filter="nonexistent") == []


def test_get_status_classic_bikes_never_negative():
    # If GBFS ever reports ebikes > total_bikes (data glitch), the
    # subtraction shouldn't go negative.
    info = _gbfs_envelope(_station_info("a", "X", 40.7, -74.0))
    status = _gbfs_envelope(_station_status("a", bikes=2, ebikes=5))
    http = _mock_client({STATION_INFO_URL: info, STATION_STATUS_URL: status})
    c = CitiBikeClient(saved_stations=[("a", "X")], http=http)
    assert c.get_status()[0].classic_bikes == 0


def test_resolve_label_round_trips():
    c = CitiBikeClient(saved_stations=[("abc", "9 Av"), ("def", "Atlantic")])
    assert c.resolve_label("abc") == "9 Av"
    assert c.resolve_label("def") == "Atlantic"
    assert c.resolve_label("ghost") is None


# --- StationStatus.as_dict --------------------------------------------


def test_status_as_dict_includes_classic_by_default():
    s = StationStatus(
        station_id="abc", label="9 Av",
        classic_bikes=5, ebikes=2, docks=10,
        status="ok", last_reported_age_seconds=20,
    )
    d = s.as_dict()
    assert d["classic_bikes"] == 5
    assert d["ebikes"] == 2
    assert d["status"] == "ok"


def test_status_as_dict_drops_classic_when_ebike_only():
    s = StationStatus(
        station_id="abc", label="9 Av",
        classic_bikes=5, ebikes=2, docks=10,
        status="ok", last_reported_age_seconds=20,
    )
    d = s.as_dict(include_classic=False)
    assert "classic_bikes" not in d
    assert d["ebikes"] == 2
    assert d["docks"] == 10


# --- Provider (wizard side) -------------------------------------------


def test_provider_satisfies_protocol():
    assert isinstance(PROVIDER, TransitProvider)


def test_provider_metadata():
    assert PROVIDER.id == "citibike"
    assert PROVIDER.label == "Citi Bike"
    assert PROVIDER.kind == "bike"
    assert PROVIDER.credentials == ()
    assert "JASPER_CITIBIKE_STATIONS" in PROVIDER.env_keys
    assert "JASPER_CITIBIKE_EBIKE_ONLY" in PROVIDER.env_keys


def test_provider_bbox_covers_nyc_jc_hoboken():
    # Manhattan (Times Square area)
    assert CITIBIKE_BBOX.includes(40.7589, -73.9851)
    # Brooklyn (Park Slope)
    assert CITIBIKE_BBOX.includes(40.6712, -73.9806)
    # Jersey City (Exchange Place)
    assert CITIBIKE_BBOX.includes(40.7178, -74.0431)
    # Hoboken (Washington St)
    assert CITIBIKE_BBOX.includes(40.7440, -74.0324)


def test_provider_bbox_excludes_out_of_area():
    assert not CITIBIKE_BBOX.includes(39.9526, -75.1652)  # Philadelphia
    assert not CITIBIKE_BBOX.includes(40.5000, -73.5000)  # Atlantic Ocean
    assert not CITIBIKE_BBOX.includes(41.0000, -74.0000)  # Bergen County, far north


def test_provider_validate_credentials_keyless():
    assert PROVIDER.validate_credentials({}) is None
    rejected = PROVIDER.validate_credentials({"WHATEVER": "foo"})
    assert rejected == {"WHATEVER": "citibike is keyless"}


def test_provider_find_stops_near_sorts_by_distance(monkeypatch):
    info = _gbfs_envelope(
        _station_info("far", "Far Station", 40.80, -73.90),
        _station_info("near", "Near Station", 40.65, -74.00),
        _station_info("med", "Medium Station", 40.70, -73.95),
    )
    status = _gbfs_envelope(
        _station_status("near"),
        _station_status("med"),
        _station_status("far"),
    )
    # Patch on the source module — the provider lazy-imports
    # fetch_feed from jasper.citibike inside find_stops_near to break
    # a startup-time import cycle, so the patch target must be the
    # source module, not the provider's namespace.
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: info if url == STATION_INFO_URL else status,
    )
    stops = PROVIDER.find_stops_near(40.66, -74.00, count=3)
    assert [s.stop_id for s in stops] == ["near", "med", "far"]


def test_provider_find_stops_near_excludes_uninstalled(monkeypatch):
    info = _gbfs_envelope(
        _station_info("on", "Active", 40.65, -74.00),
        _station_info("off", "Decommissioned", 40.66, -74.00),
    )
    status = _gbfs_envelope(
        _station_status("on"),
        _station_status("off", installed=0),
    )
    # Patch on the source module — the provider lazy-imports
    # fetch_feed from jasper.citibike inside find_stops_near to break
    # a startup-time import cycle, so the patch target must be the
    # source module, not the provider's namespace.
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: info if url == STATION_INFO_URL else status,
    )
    stops = PROVIDER.find_stops_near(40.66, -74.00, count=5)
    assert [s.stop_id for s in stops] == ["on"]


def test_provider_find_stops_near_excludes_status_missing(monkeypatch):
    # Station present in station_information.json but missing from
    # station_status.json — defensive; treat as not-installed.
    info = _gbfs_envelope(
        _station_info("on", "Active", 40.65, -74.00),
        _station_info("orphan", "Info Only", 40.66, -74.00),
    )
    status = _gbfs_envelope(_station_status("on"))
    # Patch on the source module — the provider lazy-imports
    # fetch_feed from jasper.citibike inside find_stops_near to break
    # a startup-time import cycle, so the patch target must be the
    # source module, not the provider's namespace.
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: info if url == STATION_INFO_URL else status,
    )
    stops = PROVIDER.find_stops_near(40.66, -74.00, count=5)
    assert [s.stop_id for s in stops] == ["on"]


def test_provider_find_stops_near_includes_snapshot(monkeypatch):
    info = _gbfs_envelope(_station_info("abc", "9 Av", 40.65, -74.00))
    status = _gbfs_envelope(_station_status("abc", bikes=7, ebikes=3, docks=25))
    # Patch on the source module — the provider lazy-imports
    # fetch_feed from jasper.citibike inside find_stops_near to break
    # a startup-time import cycle, so the patch target must be the
    # source module, not the provider's namespace.
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: info if url == STATION_INFO_URL else status,
    )
    stops = PROVIDER.find_stops_near(40.66, -74.00, count=1)
    assert len(stops) == 1
    assert stops[0].lines == ("4 classic, 3 e-bikes, 25 docks",)


def test_provider_find_stops_near_caps_at_count(monkeypatch):
    info_stations = [
        _station_info(f"s{i}", f"S{i}", 40.65 + i * 0.001, -74.00)
        for i in range(15)
    ]
    status_stations = [_station_status(f"s{i}") for i in range(15)]
    info = _gbfs_envelope(*info_stations)
    status = _gbfs_envelope(*status_stations)
    # Patch on the source module — the provider lazy-imports
    # fetch_feed from jasper.citibike inside find_stops_near to break
    # a startup-time import cycle, so the patch target must be the
    # source module, not the provider's namespace.
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: info if url == STATION_INFO_URL else status,
    )
    assert len(PROVIDER.find_stops_near(40.66, -74.00, count=5)) == 5
    assert len(PROVIDER.find_stops_near(40.66, -74.00, count=10)) == 10
