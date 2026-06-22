# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Weather lookup via Open-Meteo (free, no API key, no rate-limit
problems for personal use). Two endpoints:

- Geocoding: place name → lat/lon
- Forecast: lat/lon → current conditions + today's high/low + rain
  probability + condition codes

Both lookups are cached in-memory per `WeatherClient`:

- Geocoding results keyed by lowercased place name, so repeat queries
  for the same location only cost one HTTP call. The cache is bounded
  (`GEOCODE_CACHE_MAX`) with FIFO eviction so a long-lived daemon
  fielding many distinct place names can't grow it without bound.
- Forecast responses keyed by rounded lat/lon with a short TTL
  (`FORECAST_TTL_SECONDS`), so repeated weather questions about the
  same place within one session share a single fetch instead of
  re-hitting Open-Meteo each time. Mirrors the GBFS TTL cache in
  `jasper.citibike` (`_CacheEntry` + `time.monotonic`).
"""
from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz

from .log_event import log_event

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_ATTEMPTS = 2
USER_FACING_WEATHER_UNAVAILABLE = (
    "Sorry, I'm having trouble getting the weather right now. "
    "Please try again in a bit."
)

# Geocode cache cap. One household asks about a handful of places, but
# a long-lived daemon answering for guests / fielding mis-heard names
# could otherwise accumulate entries forever. 64 distinct places is far
# more than any real session needs; oldest-inserted entries (FIFO) are
# evicted past the cap.
GEOCODE_CACHE_MAX = 64

# Forecast response TTL. Open-Meteo's data updates on the order of
# minutes, and within one conversation a user often asks several
# weather questions ("what's it now", "will it rain later", "tomorrow")
# about the same place. 5 min is short enough that a follow-up minutes
# later still reflects current conditions while collapsing a burst of
# same-location questions into a single fetch. Keyed by rounded lat/lon
# so geocoded and default-coordinate lookups for the same spot share an
# entry.
FORECAST_TTL_SECONDS = 300.0

# Same FIFO cap rationale as GEOCODE_CACHE_MAX — a TTL alone bounds how
# stale an entry gets, not how many distinct locations accumulate, so
# cap the forecast cache too.
FORECAST_CACHE_MAX = 64

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


@dataclass(frozen=True)
class _CacheEntry:
    """One TTL-cached forecast response. Mirrors `jasper.citibike`'s
    `_CacheEntry`: `timestamp` is a `time.monotonic()` reading (immune
    to wall-clock jumps), `data` is the parsed Open-Meteo JSON."""
    timestamp: float
    data: dict


class WeatherResponseError(RuntimeError):
    """Open-Meteo responded, but not with usable JSON."""


@dataclass(frozen=True)
class _ParsedPlace:
    raw: str
    base: str
    search_name: str
    admin1: str = ""
    country_code: str = ""
    soft_qualifier: str = ""


@dataclass(frozen=True)
class _Candidate:
    name: str
    lat: float
    lon: float
    admin1: str = ""
    country: str = ""
    country_code: str = ""
    population: int = 0


_US_STATE_ABBR: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
US_STATES: dict[str, str] = {
    **{abbr.lower(): name for abbr, name in _US_STATE_ABBR.items()},
    **{name.lower(): name for name in _US_STATE_ABBR.values()},
}

_CA_PROVINCE_ABBR: dict[str, str] = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "PEI": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}
CA_PROVINCES: dict[str, str] = {
    **{abbr.lower(): name for abbr, name in _CA_PROVINCE_ABBR.items()},
    **{name.lower(): name for name in _CA_PROVINCE_ABBR.values()},
    "newfoundland": "Newfoundland and Labrador",
}

COUNTRIES: dict[str, tuple[str, str]] = {
    "us": ("US", "United States"),
    "usa": ("US", "United States"),
    "u s": ("US", "United States"),
    "u s a": ("US", "United States"),
    "united states": ("US", "United States"),
    "united states of america": ("US", "United States"),
    "america": ("US", "United States"),
    "canada": ("CA", "Canada"),
    "france": ("FR", "France"),
    "uk": ("GB", "United Kingdom"),
    "u k": ("GB", "United Kingdom"),
    "gb": ("GB", "United Kingdom"),
    "great britain": ("GB", "United Kingdom"),
    "united kingdom": ("GB", "United Kingdom"),
    "england": ("GB", "United Kingdom"),
    "ireland": ("IE", "Ireland"),
    "germany": ("DE", "Germany"),
    "deutschland": ("DE", "Germany"),
    "italy": ("IT", "Italy"),
    "spain": ("ES", "Spain"),
    "mexico": ("MX", "Mexico"),
    "australia": ("AU", "Australia"),
    "new zealand": ("NZ", "New Zealand"),
    "japan": ("JP", "Japan"),
}
COUNTRY_NAME_TO_CODE = {name: code for code, name in COUNTRIES.values()}


def _describe(code: int | None) -> str:
    if code is None:
        return "unknown"
    return WMO_DESCRIPTIONS.get(int(code), "unknown")


def _exception_summary(exc: BaseException) -> str:
    name = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"{name}: {response.status_code} {response.reason_phrase}"
    detail = str(exc).strip()
    return f"{name}: {detail}" if detail else name


def _upstream_summary(exc: BaseException) -> str:
    if isinstance(exc, WeatherResponseError):
        return str(exc)
    return _exception_summary(exc)


def _is_retryable_http_error(exc: httpx.HTTPError) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.RequestError)


def _upstream_failure_payload(error: str) -> dict:
    return {
        "error": error,
        "spoken_error": USER_FACING_WEATHER_UNAVAILABLE,
    }


def _norm(value: str) -> str:
    value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _suffix_match(value: str, mapping: dict[str, str]) -> tuple[str, str] | None:
    norm = _norm(value)
    matches = [
        (alias, canonical)
        for alias, canonical in mapping.items()
        if norm == alias or norm.endswith(f" {alias}")
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0]))


def _parse_place(place: str) -> _ParsedPlace:
    raw = place.strip()
    if not raw:
        return _ParsedPlace(raw="", base="", search_name="")

    pieces = [p.strip(" .") for p in re.split(r",+", raw) if p.strip(" .")]
    base = pieces[0] if pieces else raw
    qualifiers = " ".join(pieces[1:]).strip()

    admin1 = ""
    country_code = ""
    soft_qualifier = ""

    if qualifiers:
        unknown: list[str] = []
        for piece in pieces[1:]:
            norm_piece = _norm(piece)
            country = COUNTRIES.get(norm_piece)
            state = US_STATES.get(norm_piece)
            province = CA_PROVINCES.get(norm_piece)
            if country:
                country_code = country[0]
            elif state:
                admin1 = state
                country_code = "US"
            elif province:
                admin1 = province
                country_code = "CA"
            else:
                unknown.append(piece)
        # Unknown comma qualifiers are soft hints, not hard admin filters:
        # "Buenos Aires, Argentina" should still resolve without a full
        # bundled country list.
        soft_qualifier = " ".join(unknown).strip()
    else:
        country_match = _suffix_match(base, COUNTRIES)
        state_match = _suffix_match(base, US_STATES)
        province_match = _suffix_match(base, CA_PROVINCES)
        matches: list[tuple[int, str, str]] = []
        if country_match:
            matches.append((len(country_match[0]), "country", country_match[0]))
        if state_match:
            matches.append((len(state_match[0]), "state", state_match[0]))
        if province_match:
            matches.append((len(province_match[0]), "province", province_match[0]))
        if matches:
            _length, kind, alias = max(matches, key=lambda item: item[0])
            base = re.sub(
                rf"[\s,]+{re.escape(alias)}\.?$",
                "",
                _norm(base),
                flags=re.IGNORECASE,
            ).strip()
            if kind == "country":
                country_code = COUNTRIES[alias][0]
            elif kind == "state":
                admin1 = US_STATES[alias]
                country_code = "US"
            else:
                admin1 = CA_PROVINCES[alias]
                country_code = "CA"

    search_name = raw if soft_qualifier and not (admin1 or country_code) else base

    return _ParsedPlace(
        raw=raw,
        base=base.strip() or raw,
        search_name=search_name.strip() or raw,
        admin1=admin1,
        country_code=country_code,
        soft_qualifier=soft_qualifier,
    )


def _candidate_from_result(result: dict, fallback_name: str) -> _Candidate:
    country = str(result.get("country") or "")
    country_code = str(result.get("country_code") or "").upper()
    if not country_code and country:
        country_code = COUNTRY_NAME_TO_CODE.get(country, "")
    return _Candidate(
        name=str(result.get("name") or fallback_name),
        lat=float(result["latitude"]),
        lon=float(result["longitude"]),
        admin1=str(result.get("admin1") or ""),
        country=country,
        country_code=country_code,
        population=int(result.get("population") or 0),
    )


def _candidate_display_name(candidate: _Candidate) -> str:
    if candidate.admin1:
        return f"{candidate.name}, {candidate.admin1}"
    if candidate.country:
        return f"{candidate.name}, {candidate.country}"
    return candidate.name


def _candidate_score(parsed: _ParsedPlace, candidate: _Candidate) -> float | None:
    if parsed.country_code and candidate.country_code != parsed.country_code:
        return None
    if parsed.admin1 and _norm(candidate.admin1) != _norm(parsed.admin1):
        return None

    name_score = float(fuzz.WRatio(_norm(parsed.base), _norm(candidate.name)))
    score = name_score
    if _norm(parsed.base) == _norm(candidate.name):
        score += 25.0
    if parsed.admin1:
        score += 80.0
    if parsed.country_code:
        score += 30.0
    if candidate.population:
        score += min(15.0, math.log10(candidate.population) * 3.0)
    if parsed.soft_qualifier:
        soft = _norm(parsed.soft_qualifier)
        qualifier_score = max(
            (
                fuzz.WRatio(soft, _norm(value))
                for value in (
                    candidate.admin1,
                    candidate.country,
                    f"{candidate.admin1} {candidate.country}",
                )
                if value
            ),
            default=0,
        )
        if qualifier_score >= 90:
            score += 35.0
        elif qualifier_score >= 75:
            score += 15.0
    return score


def _will_rain(daily_code: int | None, precip_prob: int | None) -> bool:
    """A track is rainy if either the daily condition code is rain-ish OR
    precipitation probability crosses the threshold. Use OR so light rain
    showers with low confidence still surface."""
    if daily_code is not None and int(daily_code) in RAINY_CODES:
        return True
    if precip_prob is not None and int(precip_prob) >= RAIN_PROBABILITY_THRESHOLD:
        return True
    return False


def _hourly_forecast(
    hourly: dict,
    current_time: str | None,
    hours: int = 168,
) -> list[dict]:
    """Slice the hourly forecast to `hours` entries starting from the
    current local hour. Open-Meteo's hourly array starts at 00:00 today
    (location-local) and extends through the forecast period; we match
    by 'YYYY-MM-DDTHH' prefix to find the current hour.

    Default 168 hours = 7 days, enough to answer 'what time will it
    rain on Saturday' from any day of the week. Open-Meteo returns
    14*24 = 336 hourly entries with forecast_days=14, so we have headroom
    if we need to go longer."""
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

    end_idx = min(start_idx + hours, len(times))
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
        # ISO 8601 local-time strings from Open-Meteo when
        # `daily=sunrise,sunset` is requested. Returned verbatim; the
        # model converts to spoken form ("8:14 PM"). Null when the
        # forecast endpoint didn't include them (defensive for stale
        # caches or upstream changes).
        "sunrise": _at("sunrise"),
        "sunset": _at("sunset"),
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


def _next_rain_window(hourly: dict, current_time: str | None) -> dict | None:
    """Find the next contiguous block of hours where
    ``precipitation_probability >= RAIN_PROBABILITY_THRESHOLD``, starting
    from ``current_time``. ``None`` when no rain in the forecast.

    Return shape:
        start                  ISO 8601 hour of the first rainy hour
        end                    ISO 8601 hour of the first dry hour
                               AFTER the window — i.e. the rain has
                               stopped by this time. ``None`` when the
                               window runs to the edge of the forecast.
        peak_probability       max precipitation_probability across
                               the window
        duration_hours         end_idx - start_idx
        ends_after_forecast    True when rain continues past the last
                               hour we have data for. ``end`` is None
                               in this case; the model should phrase
                               the answer as "rain continues past
                               <last hour>" rather than quoting an
                               end time.

    The model needs both endpoints to answer "what time is it going to
    rain" — it wants start AND end, not just start. Letting the model
    scan ``hourly_forecast`` produced answers that gave only the start
    time."""
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    if not current_time or not times:
        return None
    cur_hour = current_time[:13]
    start_idx: int | None = None
    end_idx: int | None = None
    peak: int = 0
    for i, t in enumerate(times):
        if not isinstance(t, str) or t < cur_hour:
            continue
        prob = probs[i] if i < len(probs) else None
        if prob is None:
            continue
        p = int(prob)
        if start_idx is None:
            if p >= RAIN_PROBABILITY_THRESHOLD:
                start_idx = i
                peak = p
        elif p >= RAIN_PROBABILITY_THRESHOLD:
            peak = max(peak, p)
        else:
            end_idx = i
            break
    if start_idx is None:
        return None
    ends_after_forecast = end_idx is None
    if end_idx is None:
        end_idx = len(times)
    return {
        "start": times[start_idx],
        "end": times[end_idx] if end_idx < len(times) else None,
        "peak_probability": peak,
        "duration_hours": end_idx - start_idx,
        "ends_after_forecast": ends_after_forecast,
    }


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
      'tomorrow morning?' /
      'what time will it rain Sat?'     → response['hourly_forecast'],
                                          filter by date/hour vs current_local_time

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
        # 168 hours = 7 days from now. Covers 'what time on Saturday'
        # questions from any day of the week.
        "hourly_forecast": _hourly_forecast(hourly, cur_time),
        # Indexed 0..13 starting today. For 'this week' / 'next week' /
        # 'on Friday' questions, slice this by the date field.
        "daily_next_14d": _daily_array(daily, 14, today_over),
        # Precomputed answer to "when will it rain (start AND end)".
        # Null when no rain expected in the forecast window.
        "next_rain_window": _next_rain_window(hourly, cur_time),
    }


class WeatherClient:
    def __init__(
        self,
        default_location: str = "",
        units: str = "celsius",
        default_lat: float | None = None,
        default_lon: float | None = None,
        default_name: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._default = default_location
        self._units = units if units in {"celsius", "fahrenheit"} else "celsius"
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._owns_http = http is None
        self._geocode_cache: dict[str, _Location] = {}
        self._forecast_cache: dict[tuple[float, float], _CacheEntry] = {}
        self._default_location: _Location | None = None
        if default_lat is not None and default_lon is not None:
            self._default_location = _Location(
                name=(
                    default_name.strip()
                    or default_location.strip()
                    or "default location"
                ),
                lat=float(default_lat),
                lon=float(default_lon),
            )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get_json(self, label: str, url: str, params: dict) -> dict:
        last_exc: httpx.HTTPError | None = None
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            try:
                r = await self._http.get(url, params=params, timeout=5.0)
                r.raise_for_status()
                try:
                    return r.json()
                except ValueError as e:
                    raise WeatherResponseError(_exception_summary(e)) from e
            except httpx.HTTPError as e:
                last_exc = e
                retrying = (
                    attempt < HTTP_ATTEMPTS
                    and _is_retryable_http_error(e)
                )
                log_event(
                    logger,
                    "weather_http_error",
                    endpoint=label,
                    attempt=f"{attempt}/{HTTP_ATTEMPTS}",
                    retrying=retrying,
                    error=_exception_summary(e),
                    level=logging.WARNING,
                )
                if retrying:
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def _geocode(self, place: str) -> _Location | None:
        key = place.strip().lower()
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        parsed = _parse_place(place)
        if not parsed.base:
            return None
        params = {"name": parsed.search_name, "count": 20, "language": "en"}
        if parsed.country_code:
            params["countryCode"] = parsed.country_code
        data = await self._get_json("geocode", GEOCODE_URL, params)
        raw_results = data.get("results") or []
        candidates = [
            _candidate_from_result(res, parsed.base)
            for res in raw_results
            if res.get("latitude") is not None and res.get("longitude") is not None
        ]
        scored = [
            (score, candidate)
            for candidate in candidates
            if (score := _candidate_score(parsed, candidate)) is not None
        ]
        if not scored:
            log_event(
                logger,
                "weather_geocode",
                query=repr(parsed.raw),
                base=repr(parsed.base),
                admin1=repr(parsed.admin1),
                country=repr(parsed.country_code),
                soft=repr(parsed.soft_qualifier),
                candidates=len(candidates),
                outcome="no_match",
            )
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        res = scored[0][1]
        full_name = _candidate_display_name(res)
        loc = _Location(
            name=full_name,
            lat=res.lat,
            lon=res.lon,
        )
        # Bound the cache: evict the oldest-inserted entry (dicts keep
        # insertion order) once we're at the cap, before adding the new
        # one. FIFO is enough here — geocoding is idempotent, so a
        # re-fetch on a cold key is one cheap HTTP call, not a
        # correctness issue.
        while len(self._geocode_cache) >= GEOCODE_CACHE_MAX:
            self._geocode_cache.pop(next(iter(self._geocode_cache)))
        self._geocode_cache[key] = loc
        log_event(
            logger,
            "weather_geocode",
            query=repr(parsed.raw),
            base=repr(parsed.base),
            admin1=repr(parsed.admin1),
            country=repr(parsed.country_code),
            soft=repr(parsed.soft_qualifier),
            candidates=len(candidates),
            selected=repr(loc.name),
            outcome="ok",
        )
        return loc

    async def _forecast(self, loc: _Location) -> dict:
        # Short TTL cache keyed by rounded coords. `self._units` is fixed
        # per client, so it doesn't need to be in the key. Within the TTL
        # a burst of same-location questions shares one fetch; after it,
        # the entry is stale and we refetch. Mirrors jasper.citibike's
        # `fetch_feed` TTL hop. 4 decimals (~11 m) collapses geocoded and
        # default-coordinate lookups for the same spot onto one key.
        key = (round(loc.lat, 4), round(loc.lon, 4))
        entry = self._forecast_cache.get(key)
        if entry is not None and (
            time.monotonic() - entry.timestamp
        ) < FORECAST_TTL_SECONDS:
            return entry.data
        data = await self._get_json(
            "forecast",
            FORECAST_URL,
            {
                "latitude": loc.lat,
                "longitude": loc.lon,
                "current": "temperature_2m,weather_code",
                "hourly": (
                    "temperature_2m,weather_code,precipitation_probability"
                ),
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weather_code,"
                    "sunrise,sunset"
                ),
                "temperature_unit": self._units,
                "timezone": "auto",
                "forecast_days": 14,
            },
        )
        while len(self._forecast_cache) >= FORECAST_CACHE_MAX:
            self._forecast_cache.pop(next(iter(self._forecast_cache)))
        self._forecast_cache[key] = _CacheEntry(
            timestamp=time.monotonic(), data=data,
        )
        return data

    async def get_weather(self, location: str = "") -> dict:
        explicit_place = (location or "").strip()
        if explicit_place:
            try:
                loc = await self._geocode(explicit_place)
            except (httpx.HTTPError, WeatherResponseError) as e:
                return _upstream_failure_payload(
                    f"geocoding failed: {_upstream_summary(e)}"
                )
            if loc is None:
                return {"error": f"couldn't find location: {explicit_place}"}
        elif self._default_location is not None:
            loc = self._default_location
        else:
            place = self._default.strip()
            if not place:
                return {
                    "error": "no location specified and no weather default "
                    "configured (visit /weather/ to set one)",
                }
            try:
                loc = await self._geocode(place)
            except (httpx.HTTPError, WeatherResponseError) as e:
                return _upstream_failure_payload(
                    f"geocoding failed: {_upstream_summary(e)}"
                )
            if loc is None:
                return {"error": f"couldn't find location: {place}"}
        try:
            forecast = await self._forecast(loc)
        except (httpx.HTTPError, WeatherResponseError) as e:
            return _upstream_failure_payload(
                f"weather lookup failed: {_upstream_summary(e)}"
            )
        return _build_summary(forecast, loc.name, self._units)
