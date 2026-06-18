from __future__ import annotations

import httpx
import pytest

import jasper.weather as weather_module
from jasper.weather import (
    FORECAST_TTL_SECONDS,
    GEOCODE_CACHE_MAX,
    RAIN_PROBABILITY_THRESHOLD,
    USER_FACING_WEATHER_UNAVAILABLE,
    WMO_DESCRIPTIONS,
    WeatherClient,
    _build_summary,
    _describe,
    _next_rain_window,
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


def _hourly_block(
    date: str,
    start_hour: int,
    count: int,
    code: int = 0,
    prob: int = 10,
) -> dict:
    """Build a parallel-arrays hourly block starting at <date>T<HH>:00,
    `count` entries long, with predictable values for assertions.
    `code` and `prob` are constant across the block by default, which
    makes hourly-vs-daily aggregates internally consistent — pass an
    explicit hourly dict to `_open_meteo_response` for tests that
    need varying values across the day."""
    times = [f"{date}T{(start_hour + i) % 24:02d}:00" for i in range(count)]
    return {
        "time": times,
        "temperature_2m": [10.0 + i * 0.1 for i in range(count)],
        "weather_code": [code for _ in range(count)],
        "precipitation_probability": [prob for _ in range(count)],
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
        # Default: hourly covers the same `days` range as daily and
        # stays consistent with the daily aggregate (every hour of a
        # day gets that day's code/prob). Tests that need a
        # morning-rainy/afternoon-clear mismatch should pass an
        # explicit hourly dict.
        per_day_codes = [today_code, tomorrow_code] + [0] * max(0, days - 2)
        per_day_probs = [today_prob, tomorrow_prob] + [0] * max(0, days - 2)
        blocks = [
            _hourly_block(
                f"2024-05-{15 + i:02d}", 0, 24,
                code=per_day_codes[i], prob=per_day_probs[i],
            )
            for i in range(days)
        ]
        hourly = {
            "time": [t for b in blocks for t in b["time"]],
            "temperature_2m": [v for b in blocks for v in b["temperature_2m"]],
            "weather_code": [v for b in blocks for v in b["weather_code"]],
            "precipitation_probability": [
                v for b in blocks for v in b["precipitation_probability"]
            ],
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
    hours = s["hourly_forecast"]
    # First entry should be the 14:00 slot of today (current local hour).
    assert hours[0]["time"] == "2024-05-15T14:00"
    # Should span past midnight into tomorrow.
    assert hours[1]["time"] == "2024-05-15T15:00"
    assert any(h["time"].startswith("2024-05-16") for h in hours)
    # Each entry has the expected fields.
    assert "temperature" in hours[0]
    assert "condition" in hours[0]
    assert "precipitation_probability" in hours[0]


def test_build_summary_hourly_caps_at_168_hours():
    """Default 168-hour (7-day) window covers 'what time on Saturday'
    questions from any day of the week. Open-Meteo returns up to 14
    days of hourly data; we cap at 168."""
    s = _build_summary(_open_meteo_response(cur_time="2024-05-15T00:00"),
                       "Toronto", "celsius")
    hours = s["hourly_forecast"]
    assert len(hours) == 168
    # Last entry should be exactly 167 hours after the start.
    assert hours[167]["time"] == "2024-05-21T23:00"


def test_build_summary_hourly_starts_at_zero_when_current_time_unknown():
    s = _build_summary(_open_meteo_response(cur_time=None),
                       "Toronto", "celsius")
    hours = s["hourly_forecast"]
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
    assert s["hourly_forecast"] == []
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


def test_build_summary_today_reflects_remaining_hours_not_whole_day():
    """Open-Meteo's daily.precipitation_probability_max and weather_code
    aggregate over the WHOLE day, so morning rain that's already passed
    keeps showing up in `today` all afternoon. Verify today's summary
    reflects only the remaining hours from the current local time."""
    # Today has rain from 04:00–08:00 (70%, code 63), then clears.
    # Current time is 14:30 — rain is in the past.
    today_hours = {
        "time": [f"2024-05-15T{h:02d}:00" for h in range(24)],
        "temperature_2m": [12.0] * 24,
        "weather_code":
            [0, 0, 0, 0, 63, 63, 63, 63, 63, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "precipitation_probability":
            [0, 0, 0, 0, 70, 70, 70, 70, 70, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    }
    tomorrow_hours = _hourly_block("2024-05-16", 0, 24, code=1, prob=10)
    hourly = {
        k: today_hours[k] + tomorrow_hours[k]
        for k in ("time", "temperature_2m", "weather_code", "precipitation_probability")
    }
    s = _build_summary(
        _open_meteo_response(
            cur_time="2024-05-15T14:30",
            today_code=63, today_prob=70,  # daily aggregate still says rain
            hourly=hourly,
        ),
        "Brooklyn", "fahrenheit",
    )
    # Today's summary now reflects 14:00 onward (all clear, 0% prob),
    # NOT the morning's daily-aggregate rain.
    assert s["today"]["condition"] == "clear"
    assert s["today"]["precipitation_probability"] == 0
    assert s["today"]["will_rain"] is False
    # daily_next_14d[0] gets the same override (consistency with `today`).
    assert s["daily_next_14d"][0] == s["today"]
    # Tomorrow is unaffected (daily aggregate used, no override).
    assert s["tomorrow"]["condition"] == "mainly clear"


def test_today_override_picks_worst_remaining_code():
    """If rain is still coming later today, today's summary should
    surface it (code 63 = moderate rain) regardless of clear hours
    in between."""
    today_hours = {
        "time": [f"2024-05-15T{h:02d}:00" for h in range(24)],
        "temperature_2m": [12.0] * 24,
        # Clear now (14:00), drizzle (51) at 17, moderate rain (63) at 20.
        "weather_code":
            [0]*14 + [0, 0, 0, 51, 0, 0, 63, 0, 0, 0],
        "precipitation_probability":
            [0]*14 + [0, 0, 0, 40, 5, 5, 80, 5, 5, 5],
    }
    tomorrow_hours = _hourly_block("2024-05-16", 0, 24, code=0, prob=0)
    hourly = {
        k: today_hours[k] + tomorrow_hours[k]
        for k in ("time", "temperature_2m", "weather_code", "precipitation_probability")
    }
    s = _build_summary(
        _open_meteo_response(
            cur_time="2024-05-15T14:30",
            today_code=0, today_prob=0,  # daily aggregate doesn't see it
            hourly=hourly,
        ),
        "Brooklyn", "fahrenheit",
    )
    # Worst remaining-hour rainy code wins (63 > 51).
    assert s["today"]["condition"] == "moderate rain"
    assert s["today"]["precipitation_probability"] == 80
    assert s["today"]["will_rain"] is True


# --- next_rain_window ---


def _hourly_probs(probs: list[int], start_date: str = "2024-05-15", start_hour: int = 0) -> dict:
    """Build an hourly dict whose precipitation_probability varies per
    hour. ``probs`` is the prob value at each hour starting from
    ``start_date``T``start_hour``."""
    n = len(probs)
    times = []
    d = int(start_date.split("-")[2])
    h = start_hour
    for _ in range(n):
        times.append(f"2024-05-{d:02d}T{h:02d}:00")
        h += 1
        if h == 24:
            h = 0
            d += 1
    return {
        "time": times,
        "temperature_2m": [15.0] * n,
        "weather_code": [0] * n,
        "precipitation_probability": probs,
    }


def test_next_rain_window_finds_upcoming_block():
    # No rain through 16:00, then 70/80/60/10 — window is 17:00-20:00.
    hourly = _hourly_probs(
        [10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
         70, 80, 60, 10, 10, 10, 10],
        start_hour=0,
    )
    w = _next_rain_window(hourly, "2024-05-15T14:30")
    assert w is not None
    assert w["start"] == "2024-05-15T17:00"
    assert w["end"] == "2024-05-15T20:00"
    assert w["peak_probability"] == 80
    assert w["duration_hours"] == 3
    assert w["ends_after_forecast"] is False


def test_next_rain_window_returns_none_when_no_rain():
    hourly = _hourly_probs([5] * 24)
    assert _next_rain_window(hourly, "2024-05-15T08:00") is None


def test_next_rain_window_starts_at_current_hour_if_already_raining():
    # Currently 14:30; the 14:00 slot is 60% — window starts now.
    hourly = _hourly_probs(
        [10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
         60, 70, 50, 10, 10, 10, 10, 10, 10, 10],
        start_hour=0,
    )
    w = _next_rain_window(hourly, "2024-05-15T14:30")
    assert w is not None
    assert w["start"] == "2024-05-15T14:00"
    assert w["end"] == "2024-05-15T17:00"


def test_next_rain_window_clips_at_forecast_edge():
    # Rain starts at hour 22 of day 1 and continues to the end of the
    # 48-hour forecast — no dry hour to mark the end. The contract is
    # `end=None` + `ends_after_forecast=True` (both halves pinned).
    hourly = _hourly_probs(
        [10] * 22 + [70] * 26,
        start_hour=0,
    )
    w = _next_rain_window(hourly, "2024-05-15T20:00")
    assert w is not None
    assert w["start"] == "2024-05-15T22:00"
    assert w["end"] is None
    assert w["ends_after_forecast"] is True


def test_next_rain_window_skips_past_hours_before_current_time():
    # Heavy rain in the morning (already passed), clear afternoon —
    # should return None, not the past block.
    hourly = _hourly_probs(
        [80, 80, 80, 80, 80, 80,
         10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10],
        start_hour=0,
    )
    assert _next_rain_window(hourly, "2024-05-15T12:30") is None


def test_next_rain_window_handles_missing_data():
    assert _next_rain_window({}, "2024-05-15T12:00") is None
    assert _next_rain_window({"time": []}, "2024-05-15T12:00") is None
    assert _next_rain_window(_hourly_probs([70] * 4), None) is None


def test_build_summary_includes_next_rain_window():
    # The default fixture has today_prob=70 across all of today's
    # hours — so the window starts at the current hour and runs to
    # midnight (then tomorrow's prob=10 ends it).
    s = _build_summary(_open_meteo_response(), "Toronto", "celsius")
    w = s["next_rain_window"]
    assert w is not None
    assert w["start"] == "2024-05-15T14:00"
    # Tomorrow's 00:00 hour has prob=10 → window ends there.
    assert w["end"] == "2024-05-16T00:00"
    assert w["peak_probability"] == 70


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
async def test_get_weather_uses_default_coordinates_without_geocoding():
    geocode_calls = 0
    captured_forecast_query = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal geocode_calls
        if "geocoding-api" in str(request.url):
            geocode_calls += 1
            return httpx.Response(500)
        captured_forecast_query.append(dict(request.url.params))
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=72, cur_code=0,
            today_high=80, today_low=68, today_code=0, today_prob=0,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(
        default_location="11232",
        units="fahrenheit",
        default_lat=40.653,
        default_lon=-74.007,
        default_name="Sunset Park, Brooklyn",
        http=http,
    )
    try:
        result = await weather.get_weather()
        assert "error" not in result
        assert result["location"] == "Sunset Park, Brooklyn"
        assert result["units"] == "°F"
        assert geocode_calls == 0
        assert captured_forecast_query[0]["latitude"] == "40.653"
        assert captured_forecast_query[0]["longitude"] == "-74.007"
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


@pytest.mark.parametrize(
    ("query", "expected_name", "expected_country"),
    [
        ("Tampa, FL", "Tampa", "US"),
        ("Cortez, Florida", "Cortez", "US"),
        ("Cortez, Florida, USA", "Cortez", "US"),
        ("Denver CO", "denver", "US"),
        ("Paris France", "paris", "FR"),
        ("London Ontario", "london", "CA"),
        ("Buenos Aires, Argentina", "Buenos Aires, Argentina", ""),
    ],
)
@pytest.mark.asyncio
async def test_get_weather_parses_spoken_location_qualifiers(
    query: str,
    expected_name: str,
    expected_country: str,
):
    captured_geocode_query = []

    candidates = {
        "Tampa, FL": [
            {
                "name": "Tampa", "admin1": "Florida",
                "country": "United States", "country_code": "US",
                "latitude": 27.95, "longitude": -82.46, "population": 384000,
            },
        ],
        "Cortez, Florida": [
            {
                "name": "Cortez", "admin1": "Colorado",
                "country": "United States", "country_code": "US",
                "latitude": 37.35, "longitude": -108.58, "population": 8700,
            },
            {
                "name": "Cortez", "admin1": "Florida",
                "country": "United States", "country_code": "US",
                "latitude": 27.47, "longitude": -82.68, "population": 4400,
            },
        ],
        "Cortez, Florida, USA": [
            {
                "name": "Cortez", "admin1": "Colorado",
                "country": "United States", "country_code": "US",
                "latitude": 37.35, "longitude": -108.58, "population": 8700,
            },
            {
                "name": "Cortez", "admin1": "Florida",
                "country": "United States", "country_code": "US",
                "latitude": 27.47, "longitude": -82.68, "population": 4400,
            },
        ],
        "Denver CO": [
            {
                "name": "Denver", "admin1": "Colorado",
                "country": "United States", "country_code": "US",
                "latitude": 39.74, "longitude": -104.98, "population": 715000,
            },
        ],
        "Paris France": [
            {
                "name": "Paris", "country": "France", "country_code": "FR",
                "latitude": 48.85, "longitude": 2.35, "population": 2100000,
            },
        ],
        "London Ontario": [
            {
                "name": "London", "admin1": "England",
                "country": "United Kingdom", "country_code": "GB",
                "latitude": 51.51, "longitude": -0.13, "population": 8900000,
            },
            {
                "name": "London", "admin1": "Ontario",
                "country": "Canada", "country_code": "CA",
                "latitude": 42.98, "longitude": -81.23, "population": 420000,
            },
        ],
        "Buenos Aires, Argentina": [
            {
                "name": "Buenos Aires", "country": "Argentina",
                "country_code": "AR", "latitude": -34.61, "longitude": -58.38,
                "population": 2890000,
            },
            {
                "name": "Buenos Aires", "country": "Costa Rica",
                "country_code": "CR", "latitude": 9.17, "longitude": -83.33,
                "population": 27000,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            captured_geocode_query.append(dict(request.url.params))
            return httpx.Response(200, json={"results": candidates[query]})
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=15, cur_code=3,
            today_high=18, today_low=12, today_code=3, today_prob=20,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(default_location="Toronto", http=http)
    try:
        result = await weather.get_weather(location=query)
        assert "error" not in result
        assert captured_geocode_query[0]["name"] == expected_name
        if expected_country:
            assert captured_geocode_query[0]["countryCode"] == expected_country
        else:
            assert "countryCode" not in captured_geocode_query[0]
        if query.startswith("Cortez, Florida"):
            assert result["location"] == "Cortez, Florida"
        if query == "London Ontario":
            assert result["location"] == "London, Ontario"
        if query == "Buenos Aires, Argentina":
            assert result["location"] == "Buenos Aires, Argentina"
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
        assert "/weather/" in result["error"]
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


@pytest.mark.asyncio
async def test_get_weather_retries_transient_forecast_timeout_once():
    forecast_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forecast_calls
        assert "api.open-meteo.com" in str(request.url)
        forecast_calls += 1
        if forecast_calls == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=72, cur_code=0,
            today_high=80, today_low=68, today_code=0, today_prob=0,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(
        default_lat=40.653,
        default_lon=-74.007,
        default_name="Sunset Park, Brooklyn",
        http=http,
    )
    try:
        result = await weather.get_weather()
        assert "error" not in result
        assert result["location"] == "Sunset Park, Brooklyn"
        assert forecast_calls == 2
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_weather_timeout_error_includes_class_and_spoken_error():
    forecast_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forecast_calls
        forecast_calls += 1
        raise httpx.ReadTimeout("")

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(default_lat=40.653, default_lon=-74.007, http=http)
    try:
        result = await weather.get_weather()
        assert result["error"] == "weather lookup failed: ReadTimeout"
        assert result["spoken_error"] == USER_FACING_WEATHER_UNAVAILABLE
        assert forecast_calls == 2
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_weather_bad_json_returns_spoken_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(default_lat=40.653, default_lon=-74.007, http=http)
    try:
        result = await weather.get_weather()
        assert result["error"].startswith("weather lookup failed: JSONDecodeError")
        assert result["spoken_error"] == USER_FACING_WEATHER_UNAVAILABLE
    finally:
        await http.aclose()


def test_wmo_descriptions_complete():
    """Sanity check: the codes that appear in RAINY_CODES are all in
    WMO_DESCRIPTIONS (otherwise will_rain would say yes but the model
    would describe the condition as 'unknown')."""
    from jasper.weather import RAINY_CODES
    for code in RAINY_CODES:
        assert code in WMO_DESCRIPTIONS, f"code {code} missing from descriptions"


def test_get_weather_tool_routes_rain_timing_to_next_rain_window():
    """Regression for the bug where the model would answer 'when will
    it rain' with only the start time. PR #182 added the precomputed
    next_rain_window field + tool-description guidance, but missed
    updating SYSTEM_INSTRUCTION's get_weather block, which still only
    mentioned precipitation_probability + will_rain. The model
    followed the system instruction and skipped the end time.

    Post-Path-B (2026-05-23 / HANDOFF-prompting.md): the system
    instruction holds only cross-tool meta-rules. Per-tool conditional
    rules (including the rain-timing routing for get_weather) live in
    the tool's model-facing description. This test pins the guidance
    there so the regression can't slip back in."""
    import re
    from jasper.tools.weather import make_weather_tools

    weather_tools = make_weather_tools(weather=object())
    get_weather = next(t for t in weather_tools if t.name == "get_weather")
    desc = get_weather.description
    # Normalize whitespace so docstring wrapping doesn't break the
    # assertion (e.g., "BOTH\nendpoints" should still match the
    # phrase "BOTH endpoints").
    flat = re.sub(r"\s+", " ", desc)
    assert "next_rain_window" in flat
    assert "BOTH endpoints" in flat
    assert "ends_after_forecast" in flat
    assert "Tampa, Florida" in flat
    assert "Do not strip the qualifier" in flat
    assert "spoken_error" in flat
    assert "do not add technical details" in flat


# --- cache bounding + forecast TTL ------------------------------------
#
# Mirrors jasper.citibike's TTL-cache tests: drive the client through a
# MockTransport whose handler counts hits, and monkeypatch the module's
# `time` for the clock so TTL boundaries are deterministic.


class _FakeClock:
    """Stand-in for the module's `time` reference. Only `monotonic` is
    used by the cache; advance it explicitly to cross the TTL boundary."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _distinct_location_handler(geocode_calls: list[str]):
    """Handler that geocodes each distinct place name to its own coords
    (so each fills a separate cache key) and serves a trivial forecast."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            name = request.url.params["name"]
            geocode_calls.append(name)
            # Map the name to deterministic unique-ish coordinates.
            idx = abs(hash(name)) % 10000
            return httpx.Response(200, json={
                "results": [{
                    "name": name,
                    "latitude": 10.0 + idx * 0.001,
                    "longitude": 20.0 + idx * 0.001,
                }],
            })
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=15, cur_code=0,
            today_high=18, today_low=12, today_code=0, today_prob=0,
        ))

    return handler


@pytest.mark.asyncio
async def test_geocode_cache_evicts_oldest_at_cap():
    """The per-instance geocode cache is bounded at GEOCODE_CACHE_MAX with
    FIFO eviction — a long-lived daemon fielding many distinct place names
    must not grow it without bound."""
    geocode_calls: list[str] = []
    http = httpx.AsyncClient(
        transport=_mock_transport(_distinct_location_handler(geocode_calls))
    )
    weather = WeatherClient(http=http)
    try:
        # Fill exactly to the cap, then add three more distinct places.
        names = [f"Place{i}" for i in range(GEOCODE_CACHE_MAX + 3)]
        for name in names:
            assert "error" not in await weather.get_weather(location=name)
        # Cache never exceeds the cap.
        assert len(weather._geocode_cache) == GEOCODE_CACHE_MAX
        # The three oldest-inserted keys were evicted (FIFO)...
        for name in names[:3]:
            assert name.strip().lower() not in weather._geocode_cache
        # ...and the most-recent cap-worth of keys remain.
        for name in names[3:]:
            assert name.strip().lower() in weather._geocode_cache
        # Re-asking an evicted place re-geocodes (cold key), proving the
        # eviction actually dropped it rather than just hiding it.
        before = len(geocode_calls)
        await weather.get_weather(location=names[0])
        assert len(geocode_calls) == before + 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_forecast_cache_serves_second_call_within_ttl(monkeypatch):
    """A second weather query for the same location within the TTL is
    served from the forecast cache without re-hitting Open-Meteo."""
    clock = _FakeClock()
    monkeypatch.setattr(weather_module, "time", clock)
    forecast_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forecast_calls
        forecast_calls += 1
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=72, cur_code=0,
            today_high=80, today_low=68, today_code=0, today_prob=0,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(
        default_lat=40.653, default_lon=-74.007,
        default_name="Sunset Park, Brooklyn", http=http,
    )
    try:
        first = await weather.get_weather()
        # Advance the clock, but stay within the TTL window.
        clock.now += FORECAST_TTL_SECONDS - 1.0
        second = await weather.get_weather()
        assert "error" not in first
        assert second == first
        assert forecast_calls == 1, "second call should be cache-served"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_forecast_cache_refetches_after_ttl(monkeypatch):
    """Once the TTL has elapsed the entry is stale and the next query
    refetches the forecast."""
    clock = _FakeClock()
    monkeypatch.setattr(weather_module, "time", clock)
    forecast_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forecast_calls
        forecast_calls += 1
        return httpx.Response(200, json=_open_meteo_response(
            cur_temp=72, cur_code=0,
            today_high=80, today_low=68, today_code=0, today_prob=0,
        ))

    http = httpx.AsyncClient(transport=_mock_transport(handler))
    weather = WeatherClient(
        default_lat=40.653, default_lon=-74.007,
        default_name="Sunset Park, Brooklyn", http=http,
    )
    try:
        await weather.get_weather()
        # Advance the clock just past the TTL boundary.
        clock.now += FORECAST_TTL_SECONDS + 1.0
        await weather.get_weather()
        assert forecast_calls == 2, "call after TTL should refetch"
    finally:
        await http.aclose()
