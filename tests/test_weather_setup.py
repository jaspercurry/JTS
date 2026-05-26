"""Tests for the /weather/ setup wizard."""
from __future__ import annotations

import threading
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from jasper.transit import geocode as geocode_mod
from jasper.web import _common, weather_setup


def test_apply_save_geocodes_and_writes_weather_location(monkeypatch):
    monkeypatch.setattr(geocode_mod, "geocode", lambda q, **kw: geocode_mod.GeocodeResult(
        lat=27.9506,
        lon=-82.4572,
        display_name="Tampa, Hillsborough County, Florida, United States",
        source="nominatim",
    ))
    new, err = weather_setup._apply_save(
        {"location": "Tampa, FL", "units": "fahrenheit"},
        {},
        transit_state={},
    )
    assert err is None
    assert new[weather_setup.LAT_ENV] == "27.951"
    assert new[weather_setup.LON_ENV] == "-82.457"
    assert "Tampa" in new[weather_setup.DISPLAY_NAME_ENV]
    assert new[weather_setup.DEFAULT_LOCATION_ENV] == new[weather_setup.DISPLAY_NAME_ENV]
    assert new[weather_setup.UNITS_ENV] == "fahrenheit"


def test_apply_save_can_seed_from_transit_location_without_geocoding(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("empty weather save should not geocode")

    monkeypatch.setattr(geocode_mod, "geocode", fail)
    transit_state = {
        "JASPER_TRANSIT_LAT": "40.653",
        "JASPER_TRANSIT_LON": "-74.007",
        "JASPER_TRANSIT_DISPLAY_NAME": "Sunset Park, Brooklyn",
    }
    new, err = weather_setup._apply_save(
        {"units": "celsius"},
        {},
        transit_state=transit_state,
    )
    assert err is None
    assert new[weather_setup.LAT_ENV] == "40.653"
    assert new[weather_setup.LON_ENV] == "-74.007"
    assert new[weather_setup.DISPLAY_NAME_ENV] == "Sunset Park, Brooklyn"


def test_seed_transit_from_weather_only_when_transit_missing(tmp_path):
    transit_path = tmp_path / "transit.env"
    weather_state = {
        weather_setup.LAT_ENV: "40.653",
        weather_setup.LON_ENV: "-74.007",
        weather_setup.DISPLAY_NAME_ENV: "Sunset Park, Brooklyn",
    }
    assert weather_setup._seed_transit_from_weather_if_missing(
        weather_state,
        transit_path=str(transit_path),
    ) is True
    fields = _common.read_env_file(str(transit_path))
    assert fields["JASPER_TRANSIT_LAT"] == "40.653"
    assert fields["JASPER_TRANSIT_LON"] == "-74.007"
    assert fields["JASPER_TRANSIT_DISPLAY_NAME"] == "Sunset Park, Brooklyn"

    transit_path.write_text(
        "JASPER_TRANSIT_LAT=1.000\n"
        "JASPER_TRANSIT_LON=2.000\n"
        "JASPER_TRANSIT_DISPLAY_NAME=Already set\n",
    )
    assert weather_setup._seed_transit_from_weather_if_missing(
        weather_state,
        transit_path=str(transit_path),
    ) is False
    assert _common.read_env_file(str(transit_path))["JASPER_TRANSIT_LAT"] == "1.000"


def test_weather_handler_save_writes_env_and_restarts(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode_mod, "geocode", lambda q, **kw: geocode_mod.GeocodeResult(
        lat=48.8566,
        lon=2.3522,
        display_name="Paris, France",
        source="nominatim",
    ))
    restart_calls = []
    monkeypatch.setattr(weather_setup, "restart_voice_daemon", lambda: restart_calls.append(1))

    state_path = tmp_path / "weather.env"
    transit_path = tmp_path / "transit.env"
    srv = weather_setup.make_server(
        ("127.0.0.1", 0),
        state_path=str(state_path),
        transit_path=str(transit_path),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.open(base + "/").read()
        csrf = ""
        for cookie in jar:
            if cookie.name == _common.CSRF_COOKIE_NAME:
                csrf = cookie.value
        data = urllib.parse.urlencode({
            "csrf_token": csrf,
            "location": "Paris, France",
            "units": "celsius",
        }).encode()
        opener.open(
            urllib.request.Request(base + "/save", data=data, method="POST"),
        )
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)

    fields = _common.read_env_file(str(state_path))
    assert fields[weather_setup.DISPLAY_NAME_ENV] == "Paris, France"
    assert fields[weather_setup.UNITS_ENV] == "celsius"
    assert restart_calls == [1]
    assert _common.read_env_file(str(transit_path))["JASPER_TRANSIT_DISPLAY_NAME"] == "Paris, France"
