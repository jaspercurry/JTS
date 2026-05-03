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


def _build_summary(forecast: dict, location_name: str, units: str) -> dict:
    """Transform Open-Meteo's response shape into the dict shape the voice
    model expects. Defensive about missing fields — Open-Meteo's
    response schema is stable but a malformed/empty one shouldn't crash."""
    cur = forecast.get("current") or {}
    daily = forecast.get("daily") or {}

    def _first(key):
        v = daily.get(key) or []
        return v[0] if v else None

    cur_code = cur.get("weather_code")
    daily_code = _first("weather_code")
    precip_prob = _first("precipitation_probability_max")

    unit_label = "°F" if units == "fahrenheit" else "°C"

    return {
        "location": location_name,
        "temperature_now": cur.get("temperature_2m"),
        "temperature_high_today": _first("temperature_2m_max"),
        "temperature_low_today": _first("temperature_2m_min"),
        "condition_now": _describe(cur_code),
        "condition_today": _describe(daily_code),
        "precipitation_probability_today": precip_prob,
        "will_rain_today": _will_rain(daily_code, precip_prob),
        "units": unit_label,
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
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weather_code"
                ),
                "temperature_unit": self._units,
                "timezone": "auto",
                "forecast_days": 1,
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
