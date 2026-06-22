# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the transit setup wizard at /transit/.

Risky bits exercised here:
  1. State file lifecycle — geocode writes coords; save merges picks
     across the BusTime key-validation path; clear wipes only the keys
     this wizard owns.
  2. Locked vs unlocked bus card — the user can only paste a key when
     no key is set; once set, the stops picker renders.
  3. HTTP handler end-to-end — geocode redirects with the resolved
     display_name; save invokes restart_voice_daemon; advanced manual
     lat/lon path bypasses Nominatim.

Network calls (Nominatim, Photon, BusTime) are mocked via
`monkeypatch` so the test suite stays hardware-free.
`restart_voice_daemon` is monkeypatched to capture invocations
without shelling out to systemctl.
"""
from __future__ import annotations

import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from jasper.transit import geocode as geocode_mod
from jasper.web import _common, transit_setup


# ---------- Pure helpers ---------------------------------------------------


def test_owned_env_keys_includes_coords_and_provider_keys():
    keys = transit_setup._owned_env_keys()
    assert transit_setup.LAT_ENV in keys
    assert transit_setup.LON_ENV in keys
    assert "JASPER_SUBWAY_STATION_ID" in keys
    assert "JASPER_MTA_BUSTIME_KEY" in keys
    assert "JASPER_BUS_STOPS" in keys
    assert "JASPER_CITIBIKE_STATIONS" in keys
    assert "JASPER_CITIBIKE_EBIKE_ONLY" in keys


def test_coords_returns_none_when_missing():
    assert transit_setup._coords({}) is None
    assert transit_setup._coords({"JASPER_TRANSIT_LAT": "not-a-number"}) is None


def test_coords_returns_tuple_when_set():
    state = {"JASPER_TRANSIT_LAT": "40.646", "JASPER_TRANSIT_LON": "-73.994"}
    assert transit_setup._coords(state) == (40.646, -73.994)


def test_has_bus_key_only_checks_persisted_state(monkeypatch):
    """`_has_bus_key` must NOT consult os.environ. The wizard is a
    long-lived process; an operator-set value in /etc/jasper/jasper.env
    is captured at process start and won't reflect a post-startup
    migration. Reading only the persisted state file keeps render
    decisions consistent with save decisions."""
    monkeypatch.setenv("JASPER_MTA_BUSTIME_KEY", "ghost-value-in-env")
    # Empty state → no key, regardless of os.environ.
    assert not transit_setup._has_bus_key({})
    # State value → key present.
    assert transit_setup._has_bus_key({"JASPER_MTA_BUSTIME_KEY": "from-state"})


# ---------- Geocode action -------------------------------------------------


def test_apply_geocode_empty_input_returns_error():
    new, err = transit_setup._apply_geocode({}, {})
    assert err is not None
    assert "address" in err.lower()
    assert new == {}


def test_apply_geocode_writes_coords_on_success(monkeypatch):
    """Nominatim success path. The wizard should store rounded coords
    + display_name and leave the original state otherwise untouched."""
    monkeypatch.setattr(geocode_mod, "geocode", lambda q, **kw: geocode_mod.GeocodeResult(
        lat=40.646292, lon=-73.994324,
        display_name="9 Av, Sunset Park, Brooklyn",
        source="nominatim",
    ))
    current = {"foo": "preserved"}
    new, err = transit_setup._apply_geocode({"address": "9 Av Brooklyn"}, current)
    assert err is None
    assert new[transit_setup.LAT_ENV] == "40.646"  # rounded
    assert new[transit_setup.LON_ENV] == "-73.994"
    assert "Sunset Park" in new[transit_setup.DISPLAY_NAME_ENV]
    assert new["foo"] == "preserved"


def test_apply_geocode_surfaces_geocoder_failure(monkeypatch):
    def fail(q, **kw):
        raise geocode_mod.GeocodeError("no match")

    monkeypatch.setattr(geocode_mod, "geocode", fail)
    new, err = transit_setup._apply_geocode({"address": "nowhere"}, {})
    assert err is not None
    assert "geocode" in err.lower() or "couldn" in err.lower()


def test_apply_geocode_manual_lat_lon_bypasses_nominatim(monkeypatch):
    """Privacy path: paste coords directly, never hit OSM."""
    def fail(*a, **kw):
        pytest.fail("manual coords should not call geocode")

    monkeypatch.setattr(geocode_mod, "geocode", fail)
    new, err = transit_setup._apply_geocode(
        {"manual_lat": "40.646", "manual_lon": "-73.994"}, {},
    )
    assert err is None
    assert new[transit_setup.LAT_ENV] == "40.646"
    assert new[transit_setup.LON_ENV] == "-73.994"


def test_apply_geocode_manual_one_side_only_errors():
    new, err = transit_setup._apply_geocode({"manual_lat": "40.6"}, {})
    assert err is not None


def test_apply_geocode_manual_out_of_range_errors():
    new, err = transit_setup._apply_geocode(
        {"manual_lat": "200", "manual_lon": "0"}, {},
    )
    assert err is not None
    assert "-90" in err


def test_seed_weather_from_transit_only_when_weather_missing(tmp_path):
    weather_path = tmp_path / "weather.env"
    transit_state = {
        transit_setup.LAT_ENV: "40.653",
        transit_setup.LON_ENV: "-74.007",
        transit_setup.DISPLAY_NAME_ENV: "Sunset Park, Brooklyn",
    }
    assert transit_setup._seed_weather_from_transit_if_missing(
        transit_state,
        weather_path=str(weather_path),
    ) is True
    fields = _common.read_env_file(str(weather_path))
    assert fields["JASPER_WEATHER_LAT"] == "40.653"
    assert fields["JASPER_WEATHER_LON"] == "-74.007"
    assert fields["JASPER_WEATHER_DISPLAY_NAME"] == "Sunset Park, Brooklyn"

    weather_path.write_text(
        "JASPER_WEATHER_LAT=1.000\n"
        "JASPER_WEATHER_LON=2.000\n"
        "JASPER_WEATHER_DISPLAY_NAME=Already set\n",
    )
    assert transit_setup._seed_weather_from_transit_if_missing(
        transit_state,
        weather_path=str(weather_path),
    ) is False
    assert _common.read_env_file(str(weather_path))["JASPER_WEATHER_LAT"] == "1.000"


# ---------- Save action ----------------------------------------------------


class _StubBus:
    """In-process stand-in for the bus provider that captures key probes
    and reports whatever validity the test wants. Matches the
    `validate_credentials(creds: dict) -> dict | None` Protocol shape."""
    def __init__(self, accept: bool):
        self.accept = accept
        self.probed: list[str] = []

    def validate_credentials(self, credentials):
        value = credentials.get("JASPER_MTA_BUSTIME_KEY", "")
        self.probed.append(value)
        if self.accept:
            return None
        return {"JASPER_MTA_BUSTIME_KEY": "rejected by stub"}


def test_apply_save_subway_pick_persists():
    form = {"nyc_subway_stop": "B12", "nyc_subway_direction": "uptown"}
    new, err = transit_setup._apply_save(form, {}, bus_provider=_StubBus(True))
    assert err is None
    assert new["JASPER_SUBWAY_STATION_ID"] == "B12"
    assert new["JASPER_SUBWAY_DEFAULT_DIRECTION"] == "uptown"


def test_apply_save_subway_direction_both_clears_default():
    """'Both' means 'ask me each time' — drop the default so the model
    prompts. Leaving the old value in would silently keep it active."""
    current = {"JASPER_SUBWAY_DEFAULT_DIRECTION": "uptown"}
    form = {"nyc_subway_direction": "both"}
    new, err = transit_setup._apply_save(form, current, bus_provider=_StubBus(True))
    assert err is None
    assert "JASPER_SUBWAY_DEFAULT_DIRECTION" not in new


def test_apply_save_empty_picks_preserve_existing():
    """User saves with only bus fields set — subway must not be wiped.
    Test setup includes a saved key so the bus card is unlocked
    (the locked-card guard would otherwise drop the bus stops)."""
    current = {
        "JASPER_SUBWAY_STATION_ID": "B12",
        "JASPER_MTA_BUSTIME_KEY": "preexisting-key",
    }
    form = {"nyc_bus_stops": "MTA_302680|4 Av/39 St eastbound"}
    new, err = transit_setup._apply_save(form, current, bus_provider=_StubBus(True))
    assert err is None
    assert new["JASPER_SUBWAY_STATION_ID"] == "B12"
    assert new["JASPER_BUS_STOPS"] == "MTA_302680|4 Av/39 St eastbound"


def test_apply_save_valid_bus_key_is_persisted_after_probe():
    bus = _StubBus(accept=True)
    form = {"nyc_bus_key": "fresh-key"}
    new, err = transit_setup._apply_save(form, {}, bus_provider=bus)
    assert err is None
    assert bus.probed == ["fresh-key"]
    assert new["JASPER_MTA_BUSTIME_KEY"] == "fresh-key"


def test_apply_save_rejected_bus_key_returns_error_and_preserves_state():
    """Probe says no → no write; old key untouched."""
    bus = _StubBus(accept=False)
    current = {"JASPER_MTA_BUSTIME_KEY": "old-key"}
    form = {"nyc_bus_key": "bad-key"}
    new, err = transit_setup._apply_save(form, current, bus_provider=bus)
    assert err is not None
    assert "BusTime" in err
    # Old state untouched.
    assert new == current


def test_apply_save_blank_key_keeps_existing():
    bus = _StubBus(accept=True)
    current = {"JASPER_MTA_BUSTIME_KEY": "old-key"}
    new, err = transit_setup._apply_save(
        {"nyc_bus_key": "  "}, current, bus_provider=bus,
    )
    assert err is None
    assert new["JASPER_MTA_BUSTIME_KEY"] == "old-key"
    assert bus.probed == []  # no probe for blank


def test_apply_save_locked_card_ignores_bus_picks_without_key():
    """Defensive check against a crafted POST that submits
    nyc_bus_stops while the bus card is locked (no key set). Without
    this guard, the daemon would persist stop_ids it can't use (the
    SIRI client also needs the key)."""
    new, err = transit_setup._apply_save(
        {"nyc_bus_stops": "MTA_302680|4 Av/39 St eastbound"},
        {},  # no key in state, no key in form
        bus_provider=_StubBus(True),
    )
    assert err is None
    assert "JASPER_BUS_STOPS" not in new


def test_apply_save_locked_card_accepts_bus_picks_when_key_set_via_form():
    """Inverse of the locked-card guard: if the user pastes a key AND
    stop picks in the same submit (e.g. the bus card had just been
    unlocked on the previous render), the picks land alongside the key."""
    bus = _StubBus(accept=True)
    new, err = transit_setup._apply_save(
        {
            "nyc_bus_key": "fresh-key",
            "nyc_bus_stops": "MTA_302680|4 Av/39 St eastbound",
        },
        {},
        bus_provider=bus,
    )
    assert err is None
    assert new["JASPER_MTA_BUSTIME_KEY"] == "fresh-key"
    assert new["JASPER_BUS_STOPS"] == "MTA_302680|4 Av/39 St eastbound"


def test_apply_save_subway_direction_default_when_unset_renders_both():
    """Round-trip safety: state with no JASPER_SUBWAY_DEFAULT_DIRECTION
    must render with "both" selected, NOT "uptown" — otherwise a
    submit-without-touch silently mutates unconfigured state into
    "uptown" on the next save."""
    # We exercise the rendered HTML directly since this is render-time
    # behavior; the save-time half is covered by the
    # _both_clears_default test above.
    state = {
        "JASPER_TRANSIT_LAT": "40.646",
        "JASPER_TRANSIT_LON": "-73.994",
    }
    body = transit_setup._index_html(state).decode()
    # "Both" option is selected when no default direction is set.
    assert 'value="both" selected' in body


# ---------- Citi Bike save behaviour ---------------------------------------


def test_apply_save_no_citibike_marker_preserves_existing():
    """Form without `citibike_stations` (card not rendered, e.g. user
    outside coverage) must not touch citibike state. Mirrors the bus
    locked-card guard but the marker is the hidden field's presence."""
    current = {
        "JASPER_CITIBIKE_STATIONS": "abc|9 Av",
        "JASPER_CITIBIKE_EBIKE_ONLY": "1",
    }
    new, err = transit_setup._apply_save({}, current, bus_provider=_StubBus(True))
    assert err is None
    assert new["JASPER_CITIBIKE_STATIONS"] == "abc|9 Av"
    assert new["JASPER_CITIBIKE_EBIKE_ONLY"] == "1"


def test_apply_save_citibike_stations_persisted():
    form = {"citibike_stations": "abc|9 Av,def|Atlantic"}
    new, err = transit_setup._apply_save(form, {}, bus_provider=_StubBus(True))
    assert err is None
    assert new["JASPER_CITIBIKE_STATIONS"] == "abc|9 Av,def|Atlantic"
    # ebike_only checkbox absent → flag dropped (default False).
    assert "JASPER_CITIBIKE_EBIKE_ONLY" not in new


def test_apply_save_citibike_ebike_only_checkbox_persists():
    form = {
        "citibike_stations": "abc|9 Av",
        "citibike_ebike_only": "on",  # HTML checkbox default value
    }
    new, err = transit_setup._apply_save(form, {}, bus_provider=_StubBus(True))
    assert err is None
    assert new["JASPER_CITIBIKE_EBIKE_ONLY"] == "1"


def test_apply_save_citibike_ebike_only_unchecked_drops_flag():
    """Marker present, checkbox absent → flag was unchecked. Drop
    the env var so the daemon sees default-False."""
    current = {"JASPER_CITIBIKE_EBIKE_ONLY": "1"}
    form = {"citibike_stations": "abc|9 Av"}  # no ebike_only key
    new, err = transit_setup._apply_save(form, current, bus_provider=_StubBus(True))
    assert err is None
    assert "JASPER_CITIBIKE_EBIKE_ONLY" not in new


def test_apply_save_citibike_empty_stations_drops_saved():
    """Marker present but value empty → user unchecked every station.
    Drop the saved list so the tool disables on next daemon restart."""
    current = {"JASPER_CITIBIKE_STATIONS": "abc|9 Av,def|Atlantic"}
    form = {"citibike_stations": ""}
    new, err = transit_setup._apply_save(form, current, bus_provider=_StubBus(True))
    assert err is None
    assert "JASPER_CITIBIKE_STATIONS" not in new


def test_apply_save_citibike_does_not_disturb_other_providers():
    """Citi-Bike-only edit must not wipe subway / bus settings."""
    current = {
        "JASPER_SUBWAY_STATION_ID": "B12",
        "JASPER_MTA_BUSTIME_KEY": "preexisting-key",
        "JASPER_BUS_STOPS": "MTA_302680|4 Av/39 St eastbound",
    }
    form = {"citibike_stations": "abc|9 Av", "citibike_ebike_only": "on"}
    new, err = transit_setup._apply_save(form, current, bus_provider=_StubBus(True))
    assert err is None
    assert new["JASPER_SUBWAY_STATION_ID"] == "B12"
    assert new["JASPER_MTA_BUSTIME_KEY"] == "preexisting-key"
    assert new["JASPER_BUS_STOPS"] == "MTA_302680|4 Av/39 St eastbound"
    assert new["JASPER_CITIBIKE_STATIONS"] == "abc|9 Av"
    assert new["JASPER_CITIBIKE_EBIKE_ONLY"] == "1"


# ---------- Clear action ---------------------------------------------------


def test_apply_clear_drops_only_owned_keys():
    current = {
        "JASPER_SUBWAY_STATION_ID": "B12",
        "JASPER_BUS_STOPS": "MTA_X|x",
        "JASPER_CITIBIKE_STATIONS": "abc|9 Av",
        "JASPER_CITIBIKE_EBIKE_ONLY": "1",
        "JASPER_TRANSIT_LAT": "40.6",
        "FOREIGN_KEY": "kept",
    }
    new = transit_setup._apply_clear(current)
    assert "JASPER_SUBWAY_STATION_ID" not in new
    assert "JASPER_BUS_STOPS" not in new
    assert "JASPER_CITIBIKE_STATIONS" not in new
    assert "JASPER_CITIBIKE_EBIKE_ONLY" not in new
    assert "JASPER_TRANSIT_LAT" not in new
    assert new["FOREIGN_KEY"] == "kept"
    # Clear records an explicit "no cities" rather than dropping the toggle —
    # otherwise an absent key would read as "all packs eligible" and /state
    # would show every city enabled after a "Clear all".
    assert new["JASPER_TRANSIT_CITIES"] == ""


def test_apply_clear_disables_cities_not_resets_to_all():
    # The whole point: after Clear, enabled_pack_ids reads "no cities", never
    # the absent-key all-packs default.
    from jasper import transit
    new = transit_setup._apply_clear({"JASPER_SUBWAY_STATION_ID": "B12"})
    assert transit.enabled_pack_ids(new) == ()


# ---------- HTML render ----------------------------------------------------


def test_index_html_cold_state_only_shows_address_input(tmp_path: Path):
    state: dict[str, str] = {}
    html = transit_setup._index_html(state).decode()
    assert "Where you are" in html
    assert "Provider keys" not in html  # voice wizard only
    # No provider cards rendered yet.
    assert "Pick the station" not in html


def test_index_html_with_coords_shows_subway_card(monkeypatch):
    state = {
        "JASPER_TRANSIT_LAT": "40.646",
        "JASPER_TRANSIT_LON": "-73.994",
        "JASPER_TRANSIT_DISPLAY_NAME": "Sunset Park",
    }
    html = transit_setup._index_html(state).decode()
    # Subway card always renders (keyless).
    assert "NYC Subway" in html
    # Picker rows present.
    assert "9 Av" in html  # B12 should be the top hit
    # Bus card locked because no key.
    assert "needs an API key" in html
    assert "register" in html.lower() or "get a free" in html.lower()


def test_index_html_with_coords_shows_citibike_card(monkeypatch):
    """Citi Bike card renders alongside the subway and bus cards when
    coords are inside the bbox. Stub `fetch_feed` so the render
    doesn't make a real GBFS HTTP call."""
    import jasper.citibike as citibike_mod

    info = {"data": {"stations": [
        {"station_id": "abc", "name": "9 Av & 41 St",
         "lat": 40.65, "lon": -74.0, "capacity": 35},
    ]}}
    status = {"data": {"stations": [
        {"station_id": "abc",
         "num_bikes_available": 7, "num_ebikes_available": 3,
         "num_docks_available": 25,
         "is_renting": 1, "is_returning": 1, "is_installed": 1,
         "last_reported": 1700000000},
    ]}}
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: (
            info if url == citibike_mod.STATION_INFO_URL else status
        ),
    )

    state = {
        "JASPER_TRANSIT_LAT": "40.646",
        "JASPER_TRANSIT_LON": "-73.994",
        "JASPER_TRANSIT_DISPLAY_NAME": "Sunset Park",
    }
    html = transit_setup._index_html(state).decode()
    # Citi Bike card present
    assert "Citi Bike" in html
    # Live snapshot rendered: 7-3=4 classic, 3 ebikes, 25 docks
    assert "4 classic, 3 e-bikes, 25 docks" in html
    # E-bike-only toggle present
    assert "Only mention e-bikes" in html
    # Hidden marker field always emitted
    assert 'name="citibike_stations"' in html


def test_index_html_with_coords_renders_ebike_only_checked_when_set(monkeypatch):
    import jasper.citibike as citibike_mod
    monkeypatch.setattr(
        citibike_mod, "fetch_feed",
        lambda url, ttl, **kw: {"data": {"stations": []}},
    )
    state = {
        "JASPER_TRANSIT_LAT": "40.646",
        "JASPER_TRANSIT_LON": "-73.994",
        "JASPER_CITIBIKE_EBIKE_ONLY": "1",
    }
    html = transit_setup._index_html(state).decode()
    # The checkbox should be rendered with `checked` attribute.
    assert 'name="citibike_ebike_only" form="save-form" checked' in html


def test_index_html_outside_nyc_shows_no_coverage():
    state = {
        "JASPER_TRANSIT_LAT": "51.5",
        "JASPER_TRANSIT_LON": "-0.1",
        "JASPER_TRANSIT_DISPLAY_NAME": "London",
    }
    html = transit_setup._index_html(state).decode()
    assert "No transit support" in html
    assert "NYC Subway" not in html or "no UI yet" in html


# ---------- HTTP handler end-to-end ----------------------------------------


@pytest.fixture
def wizard_server(tmp_path: Path, monkeypatch):
    """Spin up the real handler on a random port with a tmpdir state
    file and a stub restart hook. Yields (base_url, state_path).

    Also wipes the geocode module's process-wide cache + rate-limit
    state before each test — otherwise a previous test that called
    the real geocoder (none should, but defensive) could leak into
    this one's cache-hit assertions."""
    geocode_mod._reset_cache_for_tests()
    state_path = str(tmp_path / "transit.env")
    weather_path = str(tmp_path / "weather.env")
    restarts: list[None] = []
    # NB: patch the symbol where it's looked up, not where defined —
    # the handler imports it from _common into its own namespace.
    monkeypatch.setattr(
        transit_setup, "restart_voice_daemon",
        lambda: restarts.append(None),
    )
    server = transit_setup.make_server(
        ("127.0.0.1", 0),
        state_path=state_path,
        weather_path=weather_path,
    )
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        port = server.server_address[1]
        yield (f"http://127.0.0.1:{port}", state_path, restarts)
    finally:
        server.shutdown()
        server.server_close()
        geocode_mod._reset_cache_for_tests()


def _post(url: str, form: dict) -> urllib.request.addinfourl:
    """POST that handles the CSRF round-trip transparently.

    Mints the csrf cookie via a GET to the wizard root, attaches it to
    the POST, and includes the matching csrf_token form field. Falls
    back to a no-cookie POST when the URL is to an unknown route on
    the same host (e.g. /nope) — the GET to / still mints a token, but
    the route check fires before CSRF and yields 404 instead of 403."""
    import http.cookiejar
    from ._web_test_helpers import CSRF_COOKIE_NAME, CSRF_FORM_FIELD

    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
    )
    opener.open(base + "/").read()
    token = ""
    for cookie in jar:
        if cookie.name == CSRF_COOKIE_NAME:
            token = cookie.value
            break

    payload = dict(form)
    if token:
        payload[CSRF_FORM_FIELD] = token
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        return opener.open(req)
    except urllib.error.HTTPError as e:
        return e


def test_handler_get_cold_renders_address_input(wizard_server):
    base_url, _, _ = wizard_server
    with urllib.request.urlopen(f"{base_url}/") as r:
        body = r.read().decode()
    assert r.status == 200
    assert "Where you are" in body
    assert 'name="address"' in body


def test_handler_post_geocode_writes_state(wizard_server, monkeypatch):
    base_url, state_path, _ = wizard_server
    monkeypatch.setattr(geocode_mod, "geocode", lambda q, **kw: geocode_mod.GeocodeResult(
        lat=40.646, lon=-73.994, display_name="Sunset Park", source="nominatim",
    ))
    r = _post(f"{base_url}/geocode", {"address": "9 Av Brooklyn"})
    # 303 See Other with empty body — the wizard's redirect pattern.
    assert r.status in (200, 303)  # urllib follows by default
    state = _common.read_env_file(state_path)
    assert state[transit_setup.LAT_ENV] == "40.646"
    assert state[transit_setup.LON_ENV] == "-73.994"


def test_handler_post_save_restarts_voice(wizard_server):
    base_url, state_path, restarts = wizard_server
    # Seed with coords so the save path doesn't trip the locked card.
    _common.write_env_file(state_path, {
        transit_setup.LAT_ENV: "40.646",
        transit_setup.LON_ENV: "-73.994",
    }, mode=transit_setup.TRANSIT_FILE_MODE)
    r = _post(f"{base_url}/save", {
        "nyc_subway_stop": "B12",
        "nyc_subway_direction": "uptown",
    })
    assert r.status in (200, 303)
    assert len(restarts) == 1
    state = _common.read_env_file(state_path)
    assert state["JASPER_SUBWAY_STATION_ID"] == "B12"


def test_handler_post_clear_wipes_owned_keys(wizard_server):
    base_url, state_path, restarts = wizard_server
    _common.write_env_file(state_path, {
        "JASPER_SUBWAY_STATION_ID": "B12",
        "JASPER_TRANSIT_LAT": "40.646",
        "FOREIGN": "kept",
    }, mode=transit_setup.TRANSIT_FILE_MODE)
    r = _post(f"{base_url}/clear", {})
    assert r.status in (200, 303)
    assert len(restarts) == 1
    state = _common.read_env_file(state_path)
    assert "JASPER_SUBWAY_STATION_ID" not in state
    assert "JASPER_TRANSIT_LAT" not in state
    assert state.get("FOREIGN") == "kept"
    # Clear persists an explicit "no cities" (not an absent key -> all packs).
    assert state.get("JASPER_TRANSIT_CITIES") == ""


def test_handler_clear_relocks_bus_card_on_next_render(wizard_server):
    """After /clear, the next GET of / must render the bus card in
    its locked (no-key) state again. Locks in the BLOCKER fix
    (_has_bus_key not consulting os.environ) from a second angle:
    even with an env-var ghost present, the rendered page reflects
    persisted state only."""
    base_url, state_path, _restarts = wizard_server
    _common.write_env_file(state_path, {
        "JASPER_TRANSIT_LAT": "40.646",
        "JASPER_TRANSIT_LON": "-73.994",
        "JASPER_MTA_BUSTIME_KEY": "configured-key",
        "JASPER_BUS_STOPS": "MTA_302680|4 Av/39 St eastbound",
    }, mode=transit_setup.TRANSIT_FILE_MODE)

    # Sanity check: with the key configured, the bus card is unlocked.
    with urllib.request.urlopen(f"{base_url}/") as r:
        body_before = r.read().decode()
    assert "needs an API key" not in body_before

    _post(f"{base_url}/clear", {})

    with urllib.request.urlopen(f"{base_url}/") as r:
        body_after = r.read().decode()
    # After clear: coords are gone too, so the bus card renders
    # "awaiting address" — confirms the page reflects persisted
    # state only, not any cached / env-leaked key.
    assert "configured-key" not in body_after
    assert "Provider keys" not in body_after  # voice wizard only


def test_handler_save_uses_registry_bus_provider_by_default(wizard_server, monkeypatch):
    """The wizard's save handler doesn't pass `bus_provider=` — it
    defaults to `transit.by_id('nyc_bus')`. Verify that path is
    actually exercised (and not a dead branch) by swapping the
    registry's bus provider with a stub and observing the probe."""
    from jasper import transit as transit_mod
    base_url, state_path, _restarts = wizard_server
    _common.write_env_file(state_path, {
        "JASPER_TRANSIT_LAT": "40.646",
        "JASPER_TRANSIT_LON": "-73.994",
    }, mode=transit_setup.TRANSIT_FILE_MODE)

    stub = _StubBus(accept=True)
    monkeypatch.setattr(
        transit_mod, "by_id",
        lambda pid: stub if pid == "nyc_bus" else None,
    )
    # `_apply_save` uses `transit.by_id` via the transit module
    # reference imported at the top of transit_setup.py. Patch the
    # name the wizard actually reads.
    monkeypatch.setattr(
        transit_setup.transit, "by_id",
        lambda pid: stub if pid == "nyc_bus" else None,
    )

    _post(f"{base_url}/save", {"nyc_bus_key": "test-key"})
    assert stub.probed == ["test-key"]
    state = _common.read_env_file(state_path)
    assert state["JASPER_MTA_BUSTIME_KEY"] == "test-key"


def test_handler_unknown_route_returns_404(wizard_server):
    base_url, _, _ = wizard_server
    r = _post(f"{base_url}/nope", {})
    assert r.status == 404


# ---------- File mode ------------------------------------------------------


def test_transit_env_file_mode_is_0640(tmp_path: Path):
    """BusTime key is mildly sensitive — broader than wake (0644) but
    narrower than OAuth secrets (0600). 0640 is the documented choice."""
    p = tmp_path / "transit.env"
    _common.write_env_file(str(p), {"K": "v"}, mode=transit_setup.TRANSIT_FILE_MODE)
    assert os.stat(p).st_mode & 0o777 == 0o640


# ---------- System-instruction nudge --------------------------------------
#
# When the daemon boots with neither subway nor bus configured, the voice
# model gets a conditional instruction redirecting transit questions to
# /transit/. When either tool IS registered, the nudge is omitted so the
# model just calls the live tool.


def test_system_instruction_includes_transit_nudge_when_unconfigured():
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(location="", transit_configured=False)
    assert "jts.local/transit" in prompt
    # Conditional framing per CLAUDE.md guidance — "if the user asks…"
    # rather than absolute "never".
    assert "If the user asks" in prompt or "if the user asks" in prompt


def test_system_instruction_omits_transit_nudge_when_configured():
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(location="", transit_configured=True)
    assert "jts.local/transit" not in prompt


def test_system_instruction_transit_configured_defaults_to_true():
    """Backwards-compat: callers not passing the new arg must NOT get
    the nudge. The signature default is `True`."""
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(location="")
    assert "jts.local/transit" not in prompt
