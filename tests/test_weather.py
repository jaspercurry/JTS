from __future__ import annotations

import httpx
import pytest

from jasper.weather import (
    RAIN_PROBABILITY_THRESHOLD,
    WMO_DESCRIPTIONS,
    WeatherClient,
    _build_summary,
    _describe,
    _will_rain,
)


# --- pure helpers ---


def test_describe_known_codes():
    assert _describe(0) == "clear"
    assert _describe(2) == "partly cloudy"
    assert _describe(63) == "moderate rain"
    assert _describe(95) == "thunderstorm"


def test_describe_unknown_code_returns_unknown():
    assert _describe(999) == "unknown"
    assert _describe(None) == "unknown"


def test_will_rain_high_probability_is_rain_regardless_of_code():
    # clear sky code (0) but 80% precipitation probability → will rain
    assert _will_rain(0, 80) is True


def test_will_rain_rainy_code_is_rain_regardless_of_probability():
    # rainy code with low probability → still rain
    assert _will_rain(63, 0) is True


def test_will_rain_clear_low_prob_is_no_rain():
    assert _will_rain(0, 5) is False
    assert _will_rain(2, 20) is False  # partly cloudy + below threshold


def test_will_rain_at_threshold():
    assert _will_rain(0, RAIN_PROBABILITY_THRESHOLD) is True


def test_will_rain_handles_none():
    assert _will_rain(None, None) is False
    assert _will_rain(None, 80) is True
    assert _will_rain(63, None) is True


# --- response transformation ---


def _open_meteo_response(
    cur_temp: float,
    cur_code: int,
    high: float,
    low: float,
    daily_code: int,
    precip_prob: int,
) -> dict:
    """Shape mirrors Open-Meteo's actual JSON for forecast_days=1."""
    return {
        "current": {"temperature_2m": cur_temp, "weather_code": cur_code},
        "daily": {
            "temperature_2m_max": [high],
            "temperature_2m_min": [low],
            "weather_code": [daily_code],
            "precipitation_probability_max": [precip_prob],
        },
    }


def test_build_summary_full_response():
    forecast = _open_meteo_response(
        cur_temp=18.5, cur_code=2, high=22.1, low=14.3,
        daily_code=63, precip_prob=70,
    )
    s = _build_summary(forecast, "Toronto, Ontario", "celsius")
    assert s["location"] == "Toronto, Ontario"
    assert s["temperature_now"] == 18.5
    assert s["temperature_high_today"] == 22.1
    assert s["temperature_low_today"] == 14.3
    assert s["condition_now"] == "partly cloudy"
    assert s["condition_today"] == "moderate rain"
    assert s["precipitation_probability_today"] == 70
    assert s["will_rain_today"] is True
    assert s["units"] == "°C"


def test_build_summary_fahrenheit_units():
    forecast = _open_meteo_response(
        cur_temp=65.3, cur_code=0, high=72, low=58, daily_code=0, precip_prob=0,
    )
    s = _build_summary(forecast, "Austin, Texas", "fahrenheit")
    assert s["units"] == "°F"
    assert s["will_rain_today"] is False


def test_build_summary_handles_empty_response():
    """If Open-Meteo returns malformed data, don't crash — return None
    fields so the model can say 'I don't know' rather than the daemon
    erroring out of a tool call."""
    s = _build_summary({}, "Nowhere", "celsius")
    assert s["location"] == "Nowhere"
    assert s["temperature_now"] is None
    assert s["condition_now"] == "unknown"
    assert s["will_rain_today"] is False


# --- WeatherClient with mock transport ---


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_weather_uses_default_location_when_empty():
    captured_geocode_query = []
    captured_forecast_query = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            captured_geocode_query.append(request.url.params["name"])
            return httpx.Response(200, json={
                "results": [{
                    "name": "Toronto", "admin1": "Ontario",
                    "latitude": 43.7, "longitude": -79.4,
                }],
            })
        captured_forecast_query.append(dict(request.url.params))
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=18.5, cur_code=2, high=22, low=14,
            daily_code=2, precip_prob=10,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(default_location="Toronto", units="celsius", http=http)
    try:
        result = await weather.get_weather()  # no location given
        assert "error" not in result
        assert result["location"] == "Toronto, Ontario"
        assert captured_geocode_query == ["Toronto"]
        assert "latitude" in captured_forecast_query[0]
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_weather_explicit_location_overrides_default():
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return httpx.Response(200, json={
                "results": [{
                    "name": "Paris", "country": "France",
                    "latitude": 48.85, "longitude": 2.35,
                }],
            })
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=15, cur_code=3, high=18, low=12, daily_code=3, precip_prob=20,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(default_location="Toronto", http=http)
    try:
        result = await weather.get_weather(location="Paris")
        assert result["location"] == "Paris, France"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_weather_caches_geocode():
    geocode_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal geocode_calls
        if "geocoding-api" in str(request.url):
            geocode_calls += 1
            return httpx.Response(200, json={
                "results": [{
                    "name": "Toronto", "latitude": 43.7, "longitude": -79.4,
                }],
            })
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=18, cur_code=0, high=22, low=14, daily_code=0, precip_prob=0,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(default_location="Toronto", http=http)
    try:
        await weather.get_weather()
        await weather.get_weather()
        await weather.get_weather()
        # First call geocodes; subsequent calls hit cache.
        assert geocode_calls == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_weather_no_default_no_arg_returns_error():
    weather = WeatherClient(default_location="", http=httpx.AsyncClient())
    try:
        result = await weather.get_weather()
        assert "error" in result
        assert "JASPER_DEFAULT_LOCATION" in result["error"]
    finally:
        await weather.aclose()


@pytest.mark.asyncio
async def test_get_weather_unknown_location_returns_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(http=http)
    try:
        result = await weather.get_weather(location="Atlantis")
        assert "error" in result
        assert "Atlantis" in result["error"]
    finally:
        await http.aclose()


def test_wmo_descriptions_complete():
    """Sanity check: the codes that appear in RAINY_CODES are all in
    WMO_DESCRIPTIONS (otherwise will_rain would say yes but the model
    would describe the condition as 'unknown')."""
    from jasper.weather import RAINY_CODES
    for code in RAINY_CODES:
        assert code in WMO_DESCRIPTIONS, f"code {code} missing from descriptions"
