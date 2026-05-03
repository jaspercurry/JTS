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


def _hourly_block(date: str, start_hour: int, count: int) -> dict:
    """Build a parallel-arrays hourly block starting at <date>T<HH>:00,
    `count` entries long, with predictable values for assertions."""
    times = [f"{date}T{(start_hour + i) % 24:02d}:00" for i in range(count)]
    return {
        "time": times,
        "temperature_2m": [10.0 + i * 0.1 for i in range(count)],
        "weather_code": [0 for _ in range(count)],
        "precipitation_probability": [10 + i for i in range(count)],
    }


def _open_meteo_response(
    cur_temp: float = 18.5,
    cur_code: int = 2,
    cur_time: str = "2024-05-15T14:30",
    today_high: float = 22.1,
    today_low: float = 14.3,
    today_code: int = 63,
    today_prob: int = 70,
    tomorrow_high: float = 24.0,
    tomorrow_low: float = 16.0,
    tomorrow_code: int = 1,
    tomorrow_prob: int = 10,
    days: int = 14,
    hourly: dict | None = None,
) -> dict:
    """Shape mirrors Open-Meteo's actual JSON for forecast_days=N with
    hourly variables enabled. Defaults to the production 14-day shape;
    pass days=2 for legacy/short-forecast scenarios."""
    if hourly is None:
        today_hours = _hourly_block("2024-05-15", 0, 24)
        tomorrow_hours = _hourly_block("2024-05-16", 0, 24)
        hourly = {
            "time": today_hours["time"] + tomorrow_hours["time"],
            "temperature_2m": today_hours["temperature_2m"] + tomorrow_hours["temperature_2m"],
            "weather_code": today_hours["weather_code"] + tomorrow_hours["weather_code"],
            "precipitation_probability": (
                today_hours["precipitation_probability"]
                + tomorrow_hours["precipitation_probability"]
            ),
        }
    # Build daily arrays of length `days`. Index 0 uses today_*, index 1
    # uses tomorrow_*, indices 2..days-1 are filler with predictable values.
    dates = [f"2024-05-{15 + i:02d}" for i in range(days)]
    highs = [today_high, tomorrow_high] + [20.0 + i for i in range(days - 2)]
    lows = [today_low, tomorrow_low] + [10.0 + i for i in range(days - 2)]
    codes = [today_code, tomorrow_code] + [0 for _ in range(days - 2)]
    probs = [today_prob, tomorrow_prob] + [0 for _ in range(days - 2)]
    return {
        "current": {
            "temperature_2m": cur_temp,
            "weather_code": cur_code,
            "time": cur_time,
        },
        "daily": {
            "time": dates[:days],
            "temperature_2m_max": highs[:days],
            "temperature_2m_min": lows[:days],
            "weather_code": codes[:days],
            "precipitation_probability_max": probs[:days],
        },
        "hourly": hourly,
    }


def test_build_summary_now_today_tomorrow_blocks():
    s = _build_summary(_open_meteo_response(), "Toronto, Ontario", "celsius")
    assert s["location"] == "Toronto, Ontario"
    assert s["units"] == "°C"
    assert s["current_local_time"] == "2024-05-15T14:30"
    assert s["now"] == {"temperature": 18.5, "condition": "partly cloudy"}
    assert s["today"]["temperature_high"] == 22.1
    assert s["today"]["temperature_low"] == 14.3
    assert s["today"]["condition"] == "moderate rain"
    assert s["today"]["precipitation_probability"] == 70
    assert s["today"]["will_rain"] is True
    assert s["tomorrow"]["temperature_high"] == 24.0
    assert s["tomorrow"]["temperature_low"] == 16.0
    assert s["tomorrow"]["condition"] == "mainly clear"
    assert s["tomorrow"]["precipitation_probability"] == 10
    assert s["tomorrow"]["will_rain"] is False


def test_build_summary_hourly_starts_at_current_hour():
    s = _build_summary(_open_meteo_response(cur_time="2024-05-15T14:30"),
                       "Toronto", "celsius")
    hours = s["hourly_next_24h"]
    assert len(hours) == 24
    # First entry should be the 14:00 slot of today (current local hour).
    assert hours[0]["time"] == "2024-05-15T14:00"
    # Should span past midnight into tomorrow.
    assert hours[-1]["time"].startswith("2024-05-16")
    # Each entry has the expected fields.
    assert "temperature" in hours[0]
    assert "condition" in hours[0]
    assert "precipitation_probability" in hours[0]


def test_build_summary_hourly_starts_at_zero_when_current_time_unknown():
    s = _build_summary(_open_meteo_response(cur_time=None),
                       "Toronto", "celsius")
    hours = s["hourly_next_24h"]
    assert len(hours) == 24
    assert hours[0]["time"] == "2024-05-15T00:00"


def test_build_summary_fahrenheit_units():
    s = _build_summary(_open_meteo_response(today_code=0, today_prob=0),
                       "Austin, Texas", "fahrenheit")
    assert s["units"] == "°F"
    assert s["today"]["will_rain"] is False


def test_build_summary_handles_empty_response():
    """If Open-Meteo returns malformed data, don't crash — return None
    fields so the model can say 'I don't know' rather than the daemon
    erroring out of a tool call."""
    s = _build_summary({}, "Nowhere", "celsius")
    assert s["location"] == "Nowhere"
    assert s["now"]["temperature"] is None
    assert s["now"]["condition"] == "unknown"
    assert s["today"]["will_rain"] is False
    assert s["tomorrow"]["will_rain"] is False
    assert s["hourly_next_24h"] == []
    assert s["daily_next_14d"] == []


def test_build_summary_daily_next_14d_full_two_weeks():
    s = _build_summary(_open_meteo_response(days=14), "Toronto", "celsius")
    days = s["daily_next_14d"]
    assert len(days) == 14
    # Index 0 is today, dates increase by one each entry.
    assert days[0]["date"] == "2024-05-15"
    assert days[1]["date"] == "2024-05-16"
    assert days[13]["date"] == "2024-05-28"
    # Each entry has the same shape as today/tomorrow.
    for d in days:
        assert {"date", "temperature_high", "temperature_low",
                "condition", "precipitation_probability", "will_rain"} <= d.keys()


def test_build_summary_daily_next_14d_first_two_match_today_tomorrow():
    """Convenience fields today/tomorrow must agree with daily_next_14d[0:2]
    so the model can use either path interchangeably."""
    s = _build_summary(_open_meteo_response(days=14), "Toronto", "celsius")
    assert s["daily_next_14d"][0] == s["today"]
    assert s["daily_next_14d"][1] == s["tomorrow"]


def test_build_summary_daily_next_14d_caps_at_returned_length():
    """If Open-Meteo returns fewer days than requested, daily_next_14d
    truncates to what's actually available (no None-padding)."""
    s = _build_summary(_open_meteo_response(days=3), "Toronto", "celsius")
    assert len(s["daily_next_14d"]) == 3


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
            cur_temp=18.5, cur_code=2,
            today_high=22, today_low=14, today_code=2, today_prob=10,
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
            cur_temp=15, cur_code=3,
            today_high=18, today_low=12, today_code=3, today_prob=20,
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
            cur_temp=18, cur_code=0,
            today_high=22, today_low=14, today_code=0, today_prob=0,
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
