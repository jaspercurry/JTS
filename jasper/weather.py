"""Weather lookup via Open-Meteo (free, no API key, no rate-limit
problems for personal use). Two endpoints:

- Geocoding: place name → lat/lon
- Forecast: lat/lon → current conditions + today's high/low + rain
  probability + condition codes

Geocoding results are cached in-memory keyed by lowercased place name,
so repeat queries for the same location only cost one HTTP call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes. Source:
# https://open-meteo.com/en/docs (Variables → Weather Code).
WMO_DESCRIPTIONS: dict[int, str] = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}

# A weather code is "rainy" if a reasonable person would say "yes, it's
# raining" or "it's going to rain." Used for the will_rain_today boolean.
RAINY_CODES = frozenset({51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99})

# Threshold above which "yes, it'll rain" is the answer regardless of
# weather code. Open-Meteo's precipitation_probability_max is 0..100.
RAIN_PROBABILITY_THRESHOLD = 30


@dataclass
class _Location:
    name: str
    lat: float
    lon: float


def _describe(code: int | None) -> str:
    if code is None:
        return "unknown"
    return WMO_DESCRIPTIONS.get(int(code), "unknown")


def _will_rain(daily_code: int | None, precip_prob: int | None) -> bool:
    """A track is rainy if either the daily condition code is rain-ish OR
    precipitation probability crosses the threshold. Use OR so light rain
    showers with low confidence still surface."""
    if daily_code is not None and int(daily_code) in RAINY_CODES:
        return True
    if precip_prob is not None and int(precip_prob) >= RAIN_PROBABILITY_THRESHOLD:
        return True
    return False


def _hourly_next_24h(hourly: dict, current_time: str | None) -> list[dict]:
    """Slice the hourly forecast to the next 24 hours starting from the
    current local hour. Open-Meteo's hourly array starts at 00:00 today
    (location-local) and extends through the forecast period; we match
    by 'YYYY-MM-DDTHH' prefix to find the current hour and take the
    next 24 entries."""
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    probs = hourly.get("precipitation_probability") or []
    if not times:
        return []

    start_idx = 0
    if current_time:
        # current_time looks like "2024-05-15T14:30"; hourly times are
        # "2024-05-15T14:00". Match on the "YYYY-MM-DDTHH" prefix.
        prefix = current_time[:13]
        for i, t in enumerate(times):
            if isinstance(t, str) and t.startswith(prefix):
                start_idx = i
                break

    end_idx = min(start_idx + 24, len(times))
    out = []
    for i in range(start_idx, end_idx):
        out.append({
            "time": times[i],
            "temperature": temps[i] if i < len(temps) else None,
            "condition": _describe(codes[i] if i < len(codes) else None),
            "precipitation_probability": probs[i] if i < len(probs) else None,
        })
    return out


def _daily_summary(daily: dict, idx: int, override: dict | None = None) -> dict:
    """Pull one day's worth of summary out of Open-Meteo's parallel arrays.
    Includes ISO date so the model can compute day-of-week for 'this week'
    / 'next week' / 'on Friday' style questions.

    `override` lets the caller replace `weather_code` and/or
    `precipitation_probability` (used for today's entry to swap in the
    remaining-hours aggregate; see `_today_override`)."""
    def _at(key):
        v = daily.get(key) or []
        return v[idx] if len(v) > idx else None

    code = _at("weather_code")
    prob = _at("precipitation_probability_max")
    if override:
        if override.get("weather_code") is not None:
            code = override["weather_code"]
        if override.get("precipitation_probability") is not None:
            prob = override["precipitation_probability"]
    return {
        "date": _at("time"),
        "temperature_high": _at("temperature_2m_max"),
        "temperature_low": _at("temperature_2m_min"),
        "condition": _describe(code),
        "precipitation_probability": prob,
        "will_rain": _will_rain(code, prob),
    }


def _today_override(hourly: dict, current_time: str | None) -> dict:
    """Compute remaining-hours-of-today aggregates from the hourly
    forecast: max precipitation_probability and worst weather code
    across hours from `current_time` to end-of-today.

    Open-Meteo's daily.precipitation_probability_max and weather_code
    cover the WHOLE day; on a morning-rain day they keep saying
    'today: 70% rain' all afternoon even when remaining hours are 0%.
    The voice tool's `today` summary uses this to override the daily
    aggregate so 'will it rain today' answers about what's still
    coming. Returns {} when hourly data is missing."""
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    if not current_time or not times:
        return {}
    today_date = current_time[:10]
    cur_hour = current_time[:13]
    max_prob: int | None = None
    worst_rainy: int | None = None
    fallback_code: int | None = None
    for i, t in enumerate(times):
        if not isinstance(t, str) or not t.startswith(today_date) or t < cur_hour:
            continue
        if i < len(probs) and probs[i] is not None:
            p = int(probs[i])
            if max_prob is None or p > max_prob:
                max_prob = p
        if i < len(codes) and codes[i] is not None:
            c = int(codes[i])
            if c in RAINY_CODES:
                if worst_rainy is None or c > worst_rainy:
                    worst_rainy = c
            elif fallback_code is None or c > fallback_code:
                fallback_code = c
    code = worst_rainy if worst_rainy is not None else fallback_code
    return {"precipitation_probability": max_prob, "weather_code": code}


def _daily_array(
    daily: dict,
    max_days: int = 14,
    today_override: dict | None = None,
) -> list[dict]:
    """Build a list of daily summaries for the next N days (capped by what
    Open-Meteo returned). Each entry is the same shape as today/tomorrow.
    `today_override` is applied to entry 0 only (rest-of-today aggregate)."""
    times = daily.get("time") or []
    n = min(len(times), max_days)
    return [
        _daily_summary(daily, i, today_override if i == 0 else None)
        for i in range(n)
    ]


def _build_summary(forecast: dict, location_name: str, units: str) -> dict:
    """Transform Open-Meteo's response shape into the dict shape the voice
    model expects. Nested by time horizon so the model picks the relevant
    sub-object based on the user's question:

      'what's the weather now?'         → response['now']
      'what's the weather today?'       → response['today']
      'what's the weather tomorrow?'    → response['tomorrow']
      'this evening?' / 'tonight?' /
      'tomorrow morning?'               → response['hourly_next_24h'],
                                          filter by hour vs current_local_time

    Defensive about missing fields — Open-Meteo's response schema is
    stable but a malformed/empty one shouldn't crash."""
    cur = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    hourly = forecast.get("hourly") or {}

    cur_code = cur.get("weather_code")
    cur_time = cur.get("time")
    today_over = _today_override(hourly, cur_time)

    return {
        "location": location_name,
        "current_local_time": cur_time,
        "units": "°F" if units == "fahrenheit" else "°C",
        "now": {
            "temperature": cur.get("temperature_2m"),
            "condition": _describe(cur_code),
        },
        "today": _daily_summary(daily, 0, today_over),
        "tomorrow": _daily_summary(daily, 1),
        "hourly_next_24h": _hourly_next_24h(hourly, cur_time),
        # Indexed 0..13 starting today. For 'this week' / 'next week' /
        # 'on Friday' questions, slice this by the date field.
        "daily_next_14d": _daily_array(daily, 14, today_over),
    }


class WeatherClient:
    def __init__(
        self,
        default_location: str = "",
        units: str = "celsius",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._default = default_location
        self._units = units if units in {"celsius", "fahrenheit"} else "celsius"
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._owns_http = http is None
        self._geocode_cache: dict[str, _Location] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _geocode(self, place: str) -> _Location | None:
        key = place.strip().lower()
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        r = await self._http.get(
            GEOCODE_URL,
            params={"name": place, "count": 1, "language": "en"},
            timeout=5.0,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None
        res = results[0]
        admin = res.get("admin1") or ""
        country = res.get("country") or ""
        full_name = res.get("name", place)
        if admin:
            full_name = f"{full_name}, {admin}"
        elif country:
            full_name = f"{full_name}, {country}"
        loc = _Location(
            name=full_name,
            lat=float(res["latitude"]),
            lon=float(res["longitude"]),
        )
        self._geocode_cache[key] = loc
        return loc

    async def _forecast(self, loc: _Location) -> dict:
        r = await self._http.get(
            FORECAST_URL,
            params={
                "latitude": loc.lat,
                "longitude": loc.lon,
                "current": "temperature_2m,weather_code",
                "hourly": (
                    "temperature_2m,weather_code,precipitation_probability"
                ),
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weather_code"
                ),
                "temperature_unit": self._units,
                "timezone": "auto",
                "forecast_days": 14,
            },
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()

    async def get_weather(self, location: str = "") -> dict:
        place = (location or self._default or "").strip()
        if not place:
            return {
                "error": "no location specified and no default configured "
                "(set JASPER_DEFAULT_LOCATION to enable bare 'what's the "
                "weather' queries)",
            }
        try:
            loc = await self._geocode(place)
        except httpx.HTTPError as e:
            return {"error": f"geocoding failed: {e}"}
        if loc is None:
            return {"error": f"couldn't find location: {place}"}
        try:
            forecast = await self._forecast(loc)
        except httpx.HTTPError as e:
            return {"error": f"weather lookup failed: {e}"}
        return _build_summary(forecast, loc.name, self._units)
