"""NYC MTA real-time subway arrivals via the public GTFS-Realtime feeds.

No API key needed (MTA dropped the requirement in 2021). Per-line-group
protobuf feeds at api-endpoint.mta.info; the `nyct-gtfs` library wraps
the URL mapping and protobuf parsing. We layer:

- A 20-second in-memory cache per feed (subway feeds regenerate every
  ~15-30s, so re-polling on rapid voice queries is wasteful).
- Direction normalisation: voice users say 'uptown', 'toward Manhattan',
  'toward Coney', 'downtown' etc., not 'N' or 'S'. We map those to MTA's
  N/S using a base alias table plus the station's own
  north_label/south_label from MTA's official station data
  ('Manhattan' / 'Coney Island' for 9 Av) so terms like 'toward Coney'
  resolve correctly at the user's home station.
- Stale-feed detection: if the protobuf timestamp is >120s old we
  return an error rather than confidently-wrong arrivals.
"""
from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)


# Lines → which protobuf feed they live in. From MTA developer docs.
# Empty string means the unsuffixed feed (1234567 + GS shuttle).
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

# Maximum protobuf staleness before we say "data unavailable".
STALE_AFTER_SEC = 120
# Cache window: re-pull the feed if older than this. Keeps voice-driven
# repeated queries cheap. MTA regenerates every ~15-30s anyway.
CACHE_WINDOW_SEC = 20


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
    """Thin wrapper over nyct-gtfs that handles caching, direction
    normalisation, and station validation."""

    def __init__(
        self,
        station_id: str,
        default_direction: str = "uptown",
        configured_lines: list[str] | None = None,
        feed_factory=None,           # injectable for tests; defaults to nyct_gtfs.NYCTFeed
        clock=None,                  # injectable for tests; defaults to datetime.now
    ) -> None:
        self._stations = _load_stations()
        self._station_id = station_id
        self._station = self._stations.get(station_id)
        self._aliases = _build_aliases(self._station)
        self._default_direction = default_direction
        # Lines the user explicitly says serve their station — used to
        # validate voice-requested lines and produce a friendly error if
        # the user asks about a line that doesn't stop here.
        self._lines = (
            tuple(configured_lines) if configured_lines
            else (self._station.lines if self._station else ())
        )
        self._feed_factory = feed_factory
        self._clock = clock or (lambda: datetime.now())
        # cache: line -> (feed_object, last_refresh_monotonic)
        self._cache: dict[str, tuple[object, float]] = {}

    @property
    def station_name(self) -> str:
        return self._station.name if self._station else self._station_id

    def _get_feed(self, line: str):
        """Return a refreshed NYCTFeed for `line`. Throttled by CACHE_WINDOW_SEC."""
        now = time.monotonic()
        cached = self._cache.get(line)
        if cached is not None:
            feed, last = cached
            if now - last < CACHE_WINDOW_SEC:
                return feed
            try:
                feed.refresh()
                self._cache[line] = (feed, now)
                return feed
            except Exception as e:  # noqa: BLE001
                logger.warning("feed refresh failed for %s: %s", line, e)
                # Fall through to construct a fresh one
        if self._feed_factory is None:
            from nyct_gtfs import NYCTFeed
            feed = NYCTFeed(line)
        else:
            feed = self._feed_factory(line)
        self._cache[line] = (feed, now)
        return feed

    def get_arrivals(self, line: str, direction: str = "") -> dict:
        """Return next arrivals for `line` heading `direction` from the
        configured station. Direction defaults to the configured default
        when empty."""
        if self._station is None:
            return {
                "error": (
                    f"station_id '{self._station_id}' isn't in the bundled "
                    "stations data — add it to jasper/data/mta_stations.csv"
                ),
            }

        line = (line or "").strip().upper()
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

        platform_id = f"{self._station_id}{ns}"
        try:
            feed = self._get_feed(line)
        except Exception as e:  # noqa: BLE001
            return {"error": f"couldn't reach MTA feed: {e}"}

        last_gen = getattr(feed, "last_generated", None)
        now = self._clock()
        if last_gen is not None:
            try:
                age = (now - last_gen).total_seconds()
                if age > STALE_AFTER_SEC:
                    return {
                        "error": (
                            f"MTA feed is stale ({int(age)}s old) — try again."
                        ),
                    }
            except TypeError:
                pass

        try:
            trips = feed.filter_trips(
                line_id=line, headed_for_stop_id=platform_id, underway=True,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": f"feed filter failed: {e}"}

        arrival_times: list[datetime] = []
        for trip in trips:
            for stu in getattr(trip, "stop_time_updates", []) or []:
                if getattr(stu, "stop_id", None) != platform_id:
                    continue
                ts = getattr(stu, "arrival", None) or getattr(stu, "departure", None)
                if ts is not None:
                    arrival_times.append(ts)

        minutes = arrivals_in_minutes(arrival_times, now)
        direction_label = (
            self._station.north_label if ns == "N" else self._station.south_label
        )
        return {
            "line": line,
            "direction": ns,
            "direction_label": direction_label,
            "station": self.station_name,
            "next_arrivals_minutes": minutes,
        }
