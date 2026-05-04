"""NYC MTA real-time subway arrivals.

Two data paths, primary + fallback:

1. **Subway Now** (api.subwaynow.app) — third-party server (open source as
   blahblahblah-/goodservice-v2) that polls MTA's GTFS-RT every 15s and
   computes ETAs from observed segment travel times rather than the
   schedule-based arrivals MTA publishes. This is what Tidbyt uses; it
   produces noticeably more accurate countdowns on B-Division (B/D/F/M/N/Q/R)
   where MTA's own data is fixed-block-coarse.

2. **nyct-gtfs direct** (api-endpoint.mta.info GTFS-RT protobuf) —
   self-contained fallback used when Subway Now is slow/down. No API key.
   Same data Tidbyt would otherwise have access to, just without the
   smoothing.

Layered features common to both paths:
- 20-second in-memory cache to avoid re-polling on rapid voice queries.
- Direction normalisation: voice users say 'uptown', 'toward Manhattan',
  'toward Coney', etc. We map to N/S using a base alias table plus the
  station's own north_label/south_label from MTA's official station data
  ('Manhattan' / 'Coney Island' for 9 Av).
- Stale-feed detection: protobuf >120s old → return error rather than
  confidently-wrong arrivals.
"""
from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from importlib import resources

import httpx

logger = logging.getLogger(__name__)


# Lines → which protobuf feed they live in. Used by the nyct-gtfs fallback.
LINE_TO_FEED: dict[str, str] = {
    "A": "ace", "C": "ace", "E": "ace",
    "B": "bdfm", "D": "bdfm", "F": "bdfm", "M": "bdfm",
    "G": "g",
    "J": "jz", "Z": "jz",
    "L": "l",
    "N": "nqrw", "Q": "nqrw", "R": "nqrw", "W": "nqrw",
    "1": "", "2": "", "3": "", "4": "", "5": "", "6": "", "7": "",
    "GS": "",  # 42 St shuttle
}

# Direction aliases that hold regardless of station — universal NYC vocabulary.
_BASE_ALIASES: dict[str, str] = {
    "n": "N", "north": "N", "northbound": "N", "uptown": "N",
    "s": "S", "south": "S", "southbound": "S", "downtown": "S",
}

STALE_AFTER_SEC = 120
CACHE_WINDOW_SEC = 20

# Subway Now (Tidbyt's data source).
SUBWAYNOW_URL = "https://api.subwaynow.app/stops/{stop_id}"
SUBWAYNOW_AGENT = "jts-jasper"
# Tight timeout: if Subway Now is hanging, we'd rather fall through fast
# than make the user wait. nyct-gtfs fallback will pick up the slack.
SUBWAYNOW_TIMEOUT = 1.5


@dataclass(frozen=True)
class StationInfo:
    stop_id: str
    name: str
    borough: str
    lines: tuple[str, ...]
    north_label: str
    south_label: str


def _load_stations() -> dict[str, StationInfo]:
    """Read jasper/data/mta_stations.csv into a dict keyed by stop_id."""
    path = resources.files("jasper.data").joinpath("mta_stations.csv")
    out: dict[str, StationInfo] = {}
    with path.open("r", encoding="utf-8") as f:
        # Strip comment lines starting with '#'.
        rows = (line for line in f if not line.lstrip().startswith("#"))
        reader = csv.DictReader(rows)
        for row in reader:
            sid = (row.get("stop_id") or "").strip()
            if not sid:
                continue
            lines = tuple(
                t for t in (row.get("lines") or "").replace(";", " ").split()
            )
            out[sid] = StationInfo(
                stop_id=sid,
                name=(row.get("stop_name") or sid).strip(),
                borough=(row.get("borough") or "").strip(),
                lines=lines,
                north_label=(row.get("north_label") or "").strip(),
                south_label=(row.get("south_label") or "").strip(),
            )
    return out


def _build_aliases(station: StationInfo | None) -> dict[str, str]:
    """Combine the universal aliases with station-specific ones derived from
    MTA's north/south labels. Returns a flat lowercased lookup."""
    aliases = dict(_BASE_ALIASES)
    if station is None:
        return aliases
    if station.north_label:
        for alias in _expand_label(station.north_label):
            aliases[alias] = "N"
    if station.south_label:
        for alias in _expand_label(station.south_label):
            aliases[alias] = "S"
    return aliases


def _expand_label(label: str) -> list[str]:
    """Turn a station's direction label like 'Manhattan' or 'Coney Island'
    into the natural-language forms a voice user might say. 'Manhattan' →
    {'manhattan', 'toward manhattan', 'manhattan-bound', 'manhattan bound'}."""
    base = label.strip().lower()
    if not base:
        return []
    return [
        base,
        f"toward {base}",
        f"to {base}",
        f"{base}-bound",
        f"{base} bound",
    ]


def normalise_direction(text: str, aliases: dict[str, str]) -> str | None:
    """Map a free-text direction phrase to 'N' or 'S'. Returns None if
    unrecognised — caller decides whether to error or fall back to a
    default."""
    if not text:
        return None
    key = text.strip().lower()
    return aliases.get(key)


def feed_url_for_line(line: str) -> str | None:
    """Return the GTFS-Realtime feed URL for a given subway line, or None
    if the line isn't recognised. NYCTFeed accepts a URL or a line letter,
    but having the URL gives us explicit control for caching."""
    if line not in LINE_TO_FEED:
        return None
    suffix = LINE_TO_FEED[line]
    base = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"
    return f"{base}-{suffix}" if suffix else base


def arrivals_in_minutes(
    arrival_times: list[datetime],
    now: datetime,
    limit: int = 3,
) -> list[int]:
    """Convert a list of arrival timestamps into 'minutes from now',
    keeping only future arrivals, rounded, sorted, capped at `limit`."""
    out = []
    for t in arrival_times:
        delta = (t - now).total_seconds()
        if delta <= 0:
            continue
        out.append(round(delta / 60))
    out.sort()
    return out[:limit]


class SubwayClient:
    """Subway Now (primary) + nyct-gtfs (fallback) subway arrivals."""

    def __init__(
        self,
        station_id: str,
        default_direction: str = "uptown",
        configured_lines: list[str] | None = None,
        feed_factory=None,                  # injectable; defaults to nyct_gtfs.NYCTFeed
        http_get=None,                      # injectable; defaults to httpx.get
        clock=None,                         # injectable; defaults to datetime.now
    ) -> None:
        self._stations = _load_stations()
        self._station_id = station_id
        self._station = self._stations.get(station_id)
        self._aliases = _build_aliases(self._station)
        self._default_direction = default_direction
        self._lines = (
            tuple(configured_lines) if configured_lines
            else (self._station.lines if self._station else ())
        )
        self._feed_factory = feed_factory
        self._http_get = http_get or self._default_http_get
        self._clock = clock or (lambda: datetime.now())
        # nyct-gtfs feed cache: line -> (feed, last_refresh_monotonic)
        self._feed_cache: dict[str, tuple[object, float]] = {}
        # Subway Now response cache: stop_id -> (json_dict, last_refresh_monotonic)
        self._sn_cache: dict[str, tuple[dict, float]] = {}

    @property
    def station_name(self) -> str:
        return self._station.name if self._station else self._station_id

    @staticmethod
    def _default_http_get(url: str, params: dict) -> httpx.Response:
        return httpx.get(url, params=params, timeout=SUBWAYNOW_TIMEOUT)

    def get_arrivals(self, line: str, direction: str = "") -> dict:
        """Return next arrivals for `line` in `direction` from the
        configured station. Tries Subway Now first; falls back to nyct-gtfs
        on any failure or unparseable response. Returns {error: ...} if
        validation fails or both paths are unavailable."""
        validated = self._validate(line, direction)
        if "error" in validated:
            return validated
        line = validated["line"]
        ns = validated["ns"]

        result = self._arrivals_via_subwaynow(line, ns)
        if result is not None:
            return self._wrap(line, ns, result, source="subwaynow")

        result = self._arrivals_via_nyct_gtfs(line, ns)
        if result is None:
            return {"error": "couldn't reach MTA data sources"}
        return self._wrap(line, ns, result, source="nyct-gtfs")

    def _validate(self, line: str, direction: str) -> dict:
        if self._station is None:
            return {
                "error": (
                    f"station_id '{self._station_id}' isn't in the bundled "
                    "stations data — add it to jasper/data/mta_stations.csv"
                ),
            }
        line = (line or "").strip().upper()
        if not line:
            # Empty line → default to the single configured line, if there
            # is only one. At a multi-line station we can't safely guess.
            if len(self._lines) == 1:
                line = self._lines[0]
            else:
                served = ", ".join(self._lines) or "(none configured)"
                return {
                    "error": (
                        f"which line? {self.station_name} serves: {served}"
                    ),
                }
        if line not in LINE_TO_FEED:
            return {"error": f"unknown subway line: {line}"}
        if self._lines and line not in self._lines:
            return {
                "error": (
                    f"the {line} train doesn't stop at {self.station_name} — "
                    f"served lines: {', '.join(self._lines)}"
                ),
            }
        direction_text = direction or self._default_direction
        ns = normalise_direction(direction_text, self._aliases)
        if ns is None:
            return {
                "error": (
                    f"didn't recognise direction '{direction_text}'. Try "
                    "'uptown', 'downtown', or one of: "
                    f"{self._station.north_label}, {self._station.south_label}."
                ),
            }
        return {"line": line, "ns": ns}

    def _wrap(self, line: str, ns: str, minutes: list[int], source: str) -> dict:
        direction_label = (
            self._station.north_label if ns == "N" else self._station.south_label
        )
        return {
            "line": line,
            "direction": ns,
            "direction_label": direction_label,
            "station": self.station_name,
            "next_arrivals_minutes": minutes,
            "source": source,
        }

    # --- Subway Now path ------------------------------------------------

    def _arrivals_via_subwaynow(self, line: str, ns: str) -> list[int] | None:
        """Try the Subway Now API. Returns a list of minutes-from-now (may
        be empty == "no upcoming trains"), or None to signal "couldn't get
        an answer here, try the next path."""
        try:
            data = self._fetch_subwaynow_cached(self._station_id)
        except Exception as e:  # noqa: BLE001
            logger.info("subwaynow fetch failed (will fall back): %s", e)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return self._extract_subwaynow(data, line, ns)
        except (KeyError, TypeError, ValueError) as e:
            logger.info("subwaynow parse failed (will fall back): %s", e)
            return None

    def _fetch_subwaynow_cached(self, stop_id: str) -> dict:
        now_mono = time.monotonic()
        cached = self._sn_cache.get(stop_id)
        if cached is not None and now_mono - cached[1] < CACHE_WINDOW_SEC:
            return cached[0]
        url = SUBWAYNOW_URL.format(stop_id=stop_id)
        r = self._http_get(url, {"agent": SUBWAYNOW_AGENT})
        r.raise_for_status()
        data = r.json()
        self._sn_cache[stop_id] = (data, now_mono)
        return data

    @staticmethod
    def _extract_subwaynow(data: dict, line: str, ns: str) -> list[int]:
        """Return the next arrivals (in minutes from the response's
        server-side timestamp, not the local clock — avoids client clock
        skew). Mirrors the extraction logic in tidbyt/community
        goodservice.star: prefer the realtime-extrapolated
        `estimated_current_stop_arrival_time`, computed against the
        top-level `timestamp`."""
        ref_ts = int(data["timestamp"])
        bucket = "north" if ns == "N" else "south"
        trips = (data.get("upcoming_trips") or {}).get(bucket) or []
        minutes: list[int] = []
        for trip in trips:
            if (trip.get("route_id") or "").upper() != line:
                continue
            eta = trip.get("estimated_current_stop_arrival_time")
            if eta is None:
                eta = trip.get("current_stop_arrival_time")
            if eta is None:
                continue
            delta = int(float(eta) - ref_ts)
            if delta <= 0:
                continue
            minutes.append(round(delta / 60))
        minutes.sort()
        return minutes[:3]

    # --- nyct-gtfs fallback ---------------------------------------------

    def _arrivals_via_nyct_gtfs(self, line: str, ns: str) -> list[int] | None:
        """Fallback. Returns a list of minutes-from-now, or None if the
        feed is unreachable / stale / errors out."""
        platform_id = f"{self._station_id}{ns}"
        try:
            feed = self._get_feed(line)
        except Exception as e:  # noqa: BLE001
            logger.info("nyct-gtfs feed unreachable: %s", e)
            return None

        last_gen = getattr(feed, "last_generated", None)
        now = self._clock()
        if last_gen is not None:
            try:
                age = (now - last_gen).total_seconds()
                if age > STALE_AFTER_SEC:
                    logger.info("nyct-gtfs feed stale (%ds)", int(age))
                    return None
            except TypeError:
                pass

        try:
            trips = feed.filter_trips(
                line_id=line, headed_for_stop_id=platform_id, underway=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.info("nyct-gtfs filter failed: %s", e)
            return None

        arrival_times: list[datetime] = []
        for trip in trips:
            for stu in getattr(trip, "stop_time_updates", []) or []:
                if getattr(stu, "stop_id", None) != platform_id:
                    continue
                ts = getattr(stu, "arrival", None) or getattr(stu, "departure", None)
                if ts is not None:
                    arrival_times.append(ts)
        return arrivals_in_minutes(arrival_times, now)

    def _get_feed(self, line: str):
        now_mono = time.monotonic()
        cached = self._feed_cache.get(line)
        if cached is not None:
            feed, last = cached
            if now_mono - last < CACHE_WINDOW_SEC:
                return feed
            try:
                feed.refresh()
                self._feed_cache[line] = (feed, now_mono)
                return feed
            except Exception as e:  # noqa: BLE001
                logger.warning("feed refresh failed for %s: %s", line, e)
        if self._feed_factory is None:
            from nyct_gtfs import NYCTFeed
            feed = NYCTFeed(line)
        else:
            feed = self._feed_factory(line)
        self._feed_cache[line] = (feed, now_mono)
        return feed
