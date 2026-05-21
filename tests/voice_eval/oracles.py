"""Independent ground-truth fetchers for the voice-eval harness.

Each oracle hits the underlying data source directly — bypassing
`jasper/` entirely — and returns native Python values. The test
compares the tool's response to the oracle's value; the oracle and
the tool's backing service can be the same API (e.g. both call
Subway Now), the point is that the *call path* is independent so a
bug in our adapter shows up as a divergence.

No shared base class, no Protocol — these are plain functions.
Comparison rules live in the tests that use them, since each rule
is unavoidably tool-specific (minutes within ±1, ISO timestamps
within a minute, set membership, etc.). When this file grows past
~300 lines, split into a package with one module per tool.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable

import httpx


# ---- subway ---------------------------------------------------------

_SUBWAYNOW_URL = "https://api.subwaynow.app/stops/{stop_id}"
_SUBWAYNOW_AGENT = "jts-voice-eval"


async def subway_arrivals(
    station: str,
    line: str,
    direction: str,
    *,
    limit: int = 3,
    http: httpx.AsyncClient | None = None,
) -> list[int]:
    """Return the next `limit` minutes-from-now for trains matching
    (line, direction) at `station` (GTFS stop id, e.g. "B12").

    `direction` is "N" (north / uptown / Manhattan-bound at 9 Av) or
    "S" (south / downtown / Coney-Island-bound at 9 Av) — matching
    the chip's vocabulary in `jasper/subway.py`.

    Uses Subway Now (the same primary source `jasper.subway` uses)
    so direct comparison to the tool's response shape is meaningful.
    For an end-to-end *independent* oracle (different API path), call
    MTA's GTFS-RT protobuf directly — left for V2 if Subway Now
    becomes the SPOT bug source."""
    owns = http is None
    client = http or httpx.AsyncClient(timeout=5.0)
    try:
        url = _SUBWAYNOW_URL.format(stop_id=station)
        r = await client.get(url, headers={"User-Agent": _SUBWAYNOW_AGENT})
        r.raise_for_status()
        data = r.json()
    finally:
        if owns:
            await client.aclose()

    now = int(time.time())
    bucket = "north" if direction.upper() == "N" else "south"
    trips = data.get("upcoming_trips", {}).get(bucket, []) or []

    minutes: list[int] = []
    for t in trips:
        if t.get("route_id") != line:
            continue
        eta = t.get("estimated_current_stop_arrival_time")
        if eta is None:
            eta = t.get("current_stop_arrival_time")
        if eta is None:
            continue
        delta = (int(eta) - now) / 60
        if delta <= 0:
            continue
        minutes.append(round(delta))
    minutes.sort()
    return minutes[:limit]


# ---- weather --------------------------------------------------------

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


async def weather_sunset(
    location: str, *, http: httpx.AsyncClient | None = None,
) -> datetime | None:
    """Return today's sunset for `location` as a timezone-aware
    `datetime` in the location's local TZ. Returns `None` if either
    geocode or forecast fails (the test then knows to skip).

    Hits Open-Meteo with `daily=sunset` directly — does NOT route
    through `jasper.weather`. That's the whole point of the oracle:
    we're verifying our weather tool returns matching data, not
    re-running the same code."""
    owns = http is None
    client = http or httpx.AsyncClient(timeout=5.0)
    try:
        g = await client.get(_GEOCODE_URL, params={
            "name": location, "count": 1, "language": "en",
        })
        g.raise_for_status()
        results = (g.json().get("results") or [])
        if not results:
            return None
        lat = float(results[0]["latitude"])
        lon = float(results[0]["longitude"])

        f = await client.get(_FORECAST_URL, params={
            "latitude": lat,
            "longitude": lon,
            "daily": "sunset",
            "timezone": "auto",
            "forecast_days": 1,
        })
        f.raise_for_status()
        sunsets = (f.json().get("daily") or {}).get("sunset") or []
        if not sunsets:
            return None
        # Open-Meteo returns naive local-time ISO strings ("2026-05-21T20:14")
        # since we asked for timezone=auto. They are local to the location.
        return datetime.fromisoformat(sunsets[0])
    finally:
        if owns:
            await client.aclose()


# ---- time -----------------------------------------------------------

def time_now_local() -> datetime:
    """Wall-clock now in the caller's local timezone, timezone-aware.

    Trivial — its job is to be the canonical reference the time tool
    is compared against. If the Pi's clock drifts, both the daemon
    and this oracle drift together, which is what we want: the test
    catches *model hallucination* (a stale system-prompt time), not
    NTP failures (which a separate health check should catch)."""
    return datetime.now().astimezone()


# ---- comparison helpers --------------------------------------------

def minutes_match(
    actual: Iterable[int], expected: Iterable[int], *, tol: int = 1,
) -> bool:
    """True iff each `actual[i]` is within `tol` minutes of `expected[i]`,
    same length. Used for subway arrival comparison.

    Tolerance is symmetric: tol=1 means "within ±1 minute." That
    matches the natural rounding noise between a tool call at T and
    an oracle call at T+50ms — both round to the same minute most of
    the time but can disagree by 1 across a minute boundary."""
    a = list(actual)
    e = list(expected)
    if len(a) != len(e):
        return False
    return all(abs(av - ev) <= tol for av, ev in zip(a, e))


def time_within_seconds(a: datetime, b: datetime, *, seconds: int = 60) -> bool:
    """True iff two timestamps are within `seconds` of each other.
    Used for the time and sunset assertions."""
    return abs((a - b).total_seconds()) <= seconds
