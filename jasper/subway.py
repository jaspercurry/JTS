# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""NYC MTA real-time subway arrivals.

Two data paths, primary + fallback:

1. **Subway Now** (api.subwaynow.app) — third-party server (open source as
   blahblahblah-/subwaynow-server) that polls all 7 MTA GTFS-RT feeds
   every 15s and writes a station-keyed `routes_stop_at` index server-
   side. **One call returns every train at the station regardless of
   which feed group it came from** — including trains rerouted off
   their normal line during service changes (an N running on D tracks
   stays in the NQRW feed; Subway Now indexes it under the D station's
   stop_id anyway). This is what Tidbyt's goodservice app and HA's MTA
   integration both use; see prior-art research at v2 review time.

2. **MTA GTFS-Realtime direct** (api-endpoint.mta.info protobuf) —
   self-contained fallback used when Subway Now is slow/down. We parse
   the standard GTFS-Realtime fields with MobilityData's maintained
   bindings, poll each unique MTA line-group feed for the station's
   CSV-documented lines, and union the results. **Reroutes are not
   visible on this path** — an N at a D station would live in NQRW,
   which we wouldn't poll for a D-only station. Acceptable degradation:
   Subway Now is the primary for a reason, and reroutes are a rare
   overlap with Subway-Now outages.

Response shape (both paths):

  {
    "station": str,
    "directions_queried": ["N"] / ["S"] / ["N", "S"],
    "arrivals": [
      {
        "line": "D",
        "direction": "N",
        "direction_label": "Manhattan",
        "minutes_from_now": 3,
      },
      ...
    ],
    "source": "subwaynow" | "mta-gtfs",
  }

Arrivals are sorted by ETA ascending, capped at 4. The voice model
weaves them into a response naming line + direction per train so
the user knows which is which.

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

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from .log_event import log_event
from .transit._mta_stations import Station as StationInfo, stations_by_id

logger = logging.getLogger(__name__)


# Lines → which protobuf feed they live in. Used by the direct MTA fallback.
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

# How many arrivals to cap the response at, across both directions.
# Picked to fit a one-sentence voice answer ("D Manhattan in 3, N
# Manhattan in 7, D Coney in 5, N Coney in 12") without dragging.
ARRIVAL_LIMIT = 4

# Subway Now (Tidbyt's data source).
SUBWAYNOW_URL = "https://api.subwaynow.app/stops/{stop_id}"
SUBWAYNOW_AGENT = "jts-jasper"
# Tight timeout: if Subway Now is hanging, we'd rather fall through fast
# than make the user wait. The direct MTA fallback will pick up the slack.
SUBWAYNOW_TIMEOUT = 1.5
MTA_GTFS_TIMEOUT = 4.0
_NYCT_TRIP_DESCRIPTOR_FIELD = 1001


def _nyct_trip_descriptor_extension():
    """Register the one NYCT extension field needed for stable trip identity.

    The standard GTFS trip id can repeat during the daylight-saving repeated
    hour. MTA's field 1001 carries the operations train id that disambiguates
    those trips. Defining only that wire-compatible field keeps JTS on current
    protobuf bindings without vendoring nyct-gtfs's stale generated package.
    """
    from google.protobuf import descriptor_pb2, descriptor_pool
    from google.transit import gtfs_realtime_pb2

    pool = descriptor_pool.Default()
    try:
        return pool.FindExtensionByNumber(
            gtfs_realtime_pb2.TripDescriptor.DESCRIPTOR,
            _NYCT_TRIP_DESCRIPTOR_FIELD,
        )
    except KeyError:
        pass

    field_type = descriptor_pb2.FieldDescriptorProto
    schema = descriptor_pb2.FileDescriptorProto(
        name="jasper-nyct-trip-identity.proto",
        package="jasper.nyct",
        syntax="proto2",
        dependency=["gtfs-realtime.proto"],
    )
    message = schema.message_type.add(name="NyctTripDescriptor")
    message.field.add(
        name="train_id",
        number=1,
        label=field_type.LABEL_OPTIONAL,
        type=field_type.TYPE_STRING,
    )
    schema.extension.add(
        name="nyct_trip_descriptor",
        number=_NYCT_TRIP_DESCRIPTOR_FIELD,
        label=field_type.LABEL_OPTIONAL,
        type=field_type.TYPE_MESSAGE,
        type_name=".jasper.nyct.NyctTripDescriptor",
        extendee=".transit_realtime.TripDescriptor",
    )
    try:
        pool.AddSerializedFile(schema.SerializeToString())
    except TypeError:
        # Another first-load thread may have registered the same extension.
        pass
    return pool.FindExtensionByNumber(
        gtfs_realtime_pb2.TripDescriptor.DESCRIPTOR,
        _NYCT_TRIP_DESCRIPTOR_FIELD,
    )


@dataclass(frozen=True)
class _StopTimeUpdate:
    stop_id: str
    arrival: datetime | None
    departure: datetime | None


@dataclass(frozen=True)
class _Trip:
    route_id: str
    underway: bool
    stop_time_updates: tuple[_StopTimeUpdate, ...]


class _GTFSRealtimeFeed:
    """Thin adapter over the standard GTFS-Realtime schema.

    JTS only reads the feed timestamp, route id, vehicle timestamp,
    stop id, and arrival/departure time. Keeping that parsing local
    avoids nyct-gtfs's generated NYCT-extension bindings, which hard-pin
    the end-of-life protobuf 4.25.3 runtime for APIs removed in protobuf
    5. Unknown NYCT extension fields remain safely ignored by protobuf.
    """

    def __init__(self, line: str) -> None:
        url = feed_url_for_line(line)
        if url is None:
            raise ValueError(f"unknown subway line: {line}")
        self._url = url
        self.last_generated: datetime | None = None
        self._trips: tuple[_Trip, ...] = ()
        self.refresh()

    @classmethod
    def from_bytes(cls, payload: bytes) -> _GTFSRealtimeFeed:
        """Build from a recorded feed without making a network call."""
        feed = cls.__new__(cls)
        feed._url = "recorded://mta-gtfs"
        feed.last_generated = None
        feed._trips = ()
        feed._load(payload)
        return feed

    @staticmethod
    def _trip_key(trip, nyct_extension) -> tuple[str, ...]:
        if trip.HasExtension(nyct_extension):
            train_id = trip.Extensions[nyct_extension].train_id
            if train_id:
                # Preserve nyct-gtfs's pairing contract: MTA may vary the
                # train-id prefix between TripUpdate and VehiclePosition,
                # while the final seven characters identify the train.
                return ("nyct", trip.trip_id, train_id[-7:])
        return (
            "gtfs",
            trip.trip_id,
            trip.route_id,
            trip.start_date,
            trip.start_time,
        )

    @staticmethod
    def _event_time(event) -> datetime | None:
        if not event.HasField("time") or event.time <= 0:
            return None
        return datetime.fromtimestamp(event.time)

    def _load(self, payload: bytes) -> None:
        from google.transit import gtfs_realtime_pb2

        nyct_extension = _nyct_trip_descriptor_extension()
        message = gtfs_realtime_pb2.FeedMessage()
        message.ParseFromString(payload)
        if not message.HasField("header") or not message.header.HasField("timestamp"):
            raise ValueError("MTA GTFS-Realtime feed has no header timestamp")

        header_timestamp = int(message.header.timestamp)
        self.last_generated = datetime.fromtimestamp(header_timestamp)

        vehicle_timestamps: dict[tuple[str, ...], int] = {}
        for entity in message.entity:
            if not entity.HasField("vehicle"):
                continue
            vehicle = entity.vehicle
            if not vehicle.HasField("trip") or not vehicle.HasField("timestamp"):
                continue
            vehicle_timestamps[
                self._trip_key(vehicle.trip, nyct_extension)
            ] = int(vehicle.timestamp)

        trips: list[_Trip] = []
        for entity in message.entity:
            if not entity.HasField("trip_update"):
                continue
            update = entity.trip_update
            if not update.HasField("trip"):
                continue
            trip = update.trip
            vehicle_timestamp = vehicle_timestamps.get(
                self._trip_key(trip, nyct_extension)
            )
            # Match nyct-gtfs's `underway=True` contract: MTA publishes
            # VehiclePosition before some B-division departures, but future-
            # dated positions are not underway. Allow one minute for clock skew.
            underway = (
                vehicle_timestamp is not None
                and vehicle_timestamp <= header_timestamp + 60
            )
            stop_updates: list[_StopTimeUpdate] = []
            for stop in update.stop_time_update:
                arrival = self._event_time(stop.arrival) if stop.HasField("arrival") else None
                departure = (
                    self._event_time(stop.departure)
                    if stop.HasField("departure")
                    else None
                )
                stop_updates.append(
                    _StopTimeUpdate(
                        stop_id=stop.stop_id,
                        arrival=arrival,
                        departure=departure,
                    )
                )
            trips.append(
                _Trip(
                    route_id=trip.route_id,
                    underway=underway,
                    stop_time_updates=tuple(stop_updates),
                )
            )
        self._trips = tuple(trips)

    def refresh(self) -> None:
        response = httpx.get(self._url, timeout=MTA_GTFS_TIMEOUT)
        response.raise_for_status()
        self._load(response.content)

    def filter_trips(
        self,
        *,
        line_id: str,
        headed_for_stop_id: str,
        underway: bool,
    ) -> list[_Trip]:
        return [
            trip
            for trip in self._trips
            if trip.route_id == line_id
            and trip.underway is underway
            and any(
                stop.stop_id == headed_for_stop_id
                for stop in trip.stop_time_updates
            )
        ]


# The runtime arrivals client uses the same `Station` dataclass as
# the provider; `StationInfo` is re-exported above as an alias for
# backwards-compat with any callers that imported it before the
# shared module landed.


def _load_stations() -> dict[str, StationInfo]:
    """Stop-id-keyed view of the bundled stations CSV.

    Resource lookup, open, decode, or iteration failures become ``{}`` rather
    than raising. The CSV reader remains permissive, so parseable incomplete
    rows can survive with degraded optional metadata. ``jasper-voice`` boots
    through subway provider import and ``SubwayClient`` construction; an
    uncaught resource failure would take down the whole voice loop.
    """
    return stations_by_id()


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


class SubwayClient:
    """Subway Now (primary) + direct MTA GTFS fallback arrivals.

    Direction handling:
      - `direction="both"` or `""` with no configured default → both N+S
      - `direction="uptown"` / `"north"` / station-specific label → N
      - `direction="downtown"` / `"south"` / station-specific label → S
      - `direction=""` with a configured default → use the default

    Line handling:
      - `line=""` → all lines stopping at the station (Subway Now path
        sees reroutes; the MTA fallback covers the station's
        CSV-documented lines only)
      - `line="D"` → filter to that line post-fetch
    """

    def __init__(
        self,
        station_id: str,
        default_direction: str = "",
        feed_factory=None,                  # injectable; defaults to _GTFSRealtimeFeed
        http_get=None,                      # injectable; defaults to httpx.get
        clock=None,                         # injectable; defaults to datetime.now
    ) -> None:
        self._stations = _load_stations()
        self._station_id = station_id
        self._station = self._stations.get(station_id)
        self._aliases = _build_aliases(self._station)
        # Empty string → "both directions" — the wizard's "Both" radio
        # writes an empty env value for exactly this case.
        self._default_direction = default_direction
        self._feed_factory = feed_factory
        self._http_get = http_get or self._default_http_get
        self._clock = clock or (lambda: datetime.now())
        # Direct MTA feed cache: line -> (feed, last_refresh_monotonic)
        self._feed_cache: dict[str, tuple[object, float]] = {}
        # Subway Now response cache: stop_id -> (json_dict, last_refresh_monotonic)
        self._sn_cache: dict[str, tuple[dict, float]] = {}

    @property
    def station_name(self) -> str:
        return self._station.name if self._station else self._station_id

    @staticmethod
    def _default_http_get(url: str, params: dict) -> httpx.Response:
        return httpx.get(url, params=params, timeout=SUBWAYNOW_TIMEOUT)

    def get_arrivals(self, line: str = "", direction: str = "") -> dict:
        """Return next arrivals at the configured station.

        Defaults: line="" → all lines, direction="" → the configured
        default (or both if no default). Both default to "all" to
        match the user's natural "next train" question."""
        validated = self._validate(line, direction)
        if "error" in validated:
            return validated
        line_filter: str = validated["line"]
        directions: list[str] = validated["directions"]

        arrivals = self._arrivals_via_subwaynow(directions, line_filter)
        source = "subwaynow"
        if arrivals is None:
            arrivals = self._arrivals_via_mta_gtfs(directions, line_filter)
            source = "mta-gtfs"
        if arrivals is None:
            log_event(
                logger,
                "transit.subway.arrivals.error",
                station=self._station_id,
                reason="all-sources-down",
                level=logging.WARNING,
            )
            return {"error": "couldn't reach MTA data sources"}

        log_event(
            logger,
            "transit.subway.arrivals.ok",
            station=self._station_id,
            source=source,
            n=len(arrivals),
            directions="+".join(directions),
        )
        return self._wrap(arrivals, directions, source)

    def _validate(self, line: str, direction: str) -> dict:
        if self._station is None:
            return {
                "error": (
                    f"station_id '{self._station_id}' isn't in the bundled "
                    "stations data — add it to jasper/data/mta_stations.csv"
                ),
            }
        line = (line or "").strip().upper()
        if line and line not in LINE_TO_FEED:
            return {"error": f"unknown subway line: {line}"}

        dir_text = (direction or "").strip().lower()
        if dir_text == "both":
            directions = ["N", "S"]
        elif dir_text:
            ns = normalise_direction(dir_text, self._aliases)
            if ns is None:
                return {
                    "error": (
                        f"didn't recognise direction '{direction}'. Try "
                        "'uptown', 'downtown', 'both', or one of: "
                        f"{self._station.north_label}, {self._station.south_label}."
                    ),
                }
            directions = [ns]
        else:
            # Empty direction → use configured default. Configured
            # default empty (or 'both') → both directions.
            configured = (self._default_direction or "").strip().lower()
            if not configured or configured == "both":
                directions = ["N", "S"]
            else:
                ns = normalise_direction(configured, self._aliases)
                directions = [ns] if ns else ["N", "S"]

        return {"line": line, "directions": directions}

    def _direction_label(self, ns: str) -> str:
        if self._station is None:
            return ns
        return self._station.north_label if ns == "N" else self._station.south_label

    def _wrap(
        self,
        arrivals: list[dict],
        directions: list[str],
        source: str,
    ) -> dict:
        return {
            "station": self.station_name,
            "directions_queried": directions,
            "arrivals": arrivals,
            "source": source,
        }

    # --- Subway Now path ------------------------------------------------

    def _arrivals_via_subwaynow(
        self,
        directions: list[str],
        line_filter: str,
    ) -> list[dict] | None:
        """Try the Subway Now API. Returns a list of arrival dicts
        (may be empty == "no upcoming trains") or None to signal
        "couldn't get an answer here, try the next path." Subway Now's
        station endpoint already aggregates across all 7 MTA feeds —
        we just iterate, optionally line-filter, and shape."""
        try:
            data = self._fetch_subwaynow_cached(self._station_id)
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "transit.subway.fetch.error",
                station=self._station_id,
                source="subwaynow",
                err=repr(e),
            )
            return None
        if not isinstance(data, dict):
            log_event(
                logger,
                "transit.subway.fetch.error",
                station=self._station_id,
                source="subwaynow",
                err="non-dict",
            )
            return None
        try:
            return self._extract_subwaynow(data, directions, line_filter)
        except (KeyError, TypeError, ValueError) as e:
            log_event(
                logger,
                "transit.subway.parse.error",
                station=self._station_id,
                source="subwaynow",
                err=repr(e),
            )
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

    def _extract_subwaynow(
        self,
        data: dict,
        directions: list[str],
        line_filter: str,
    ) -> list[dict]:
        """Iterate `upcoming_trips.{north,south}` for each requested
        direction. Each trip carries its own `route_id` already; we
        emit every trip in the buckets so trains rerouted from other
        lines surface alongside regulars. Optional explicit
        `line_filter` from the tool call narrows post-fetch."""
        ref_ts = int(data["timestamp"])
        arrivals: list[dict] = []
        for ns in directions:
            bucket = "north" if ns == "N" else "south"
            trips = (data.get("upcoming_trips") or {}).get(bucket) or []
            direction_label = self._direction_label(ns)
            for trip in trips:
                route = (trip.get("route_id") or "").strip().upper()
                if not route:
                    continue
                if line_filter and route != line_filter:
                    continue
                eta = trip.get("estimated_current_stop_arrival_time")
                if eta is None:
                    eta = trip.get("current_stop_arrival_time")
                if eta is None:
                    continue
                delta = int(float(eta) - ref_ts)
                if delta <= 0:
                    continue
                arrivals.append({
                    "line": route,
                    "direction": ns,
                    "direction_label": direction_label,
                    "minutes_from_now": round(delta / 60),
                })
        arrivals.sort(key=lambda a: a["minutes_from_now"])
        return arrivals[:ARRIVAL_LIMIT]

    # --- direct MTA GTFS-Realtime fallback -------------------------------

    def _arrivals_via_mta_gtfs(
        self,
        directions: list[str],
        line_filter: str,
    ) -> list[dict] | None:
        """Fallback. Iterates the station's CSV-documented lines (or
        just `line_filter` if set), loads each unique feed, filters by
        platform_id. Reroutes from other lines are NOT visible — that's
        the documented cost of falling back to per-feed polling.

        Returns a list of arrival dicts, or None if every feed was
        unreachable (signalling complete failure)."""
        if self._station is None:
            return None

        # Which lines to poll? Explicit filter wins; otherwise the
        # station's CSV-documented lines.
        if line_filter:
            lines_to_poll: tuple[str, ...] = (line_filter,)
        else:
            lines_to_poll = self._station.lines
        if not lines_to_poll:
            return []

        # Deduplicate by feed group so we don't load BDFM twice for a
        # D+F station. The representative line per group is just the
        # one we'll hand the feed factory; the factory itself maps
        # line → feed group internally.
        feeds_to_load: dict[str, str] = {}
        for line in lines_to_poll:
            group = LINE_TO_FEED.get(line)
            if group is None:
                continue
            feeds_to_load[group] = line

        now = self._clock()
        arrivals: list[dict] = []
        any_feed_succeeded = False

        for group, representative_line in feeds_to_load.items():
            try:
                feed = self._get_feed(representative_line)
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "transit.subway.fetch.error",
                    station=self._station_id,
                    source="mta-gtfs",
                    feed=group,
                    err=repr(e),
                )
                continue

            last_gen = getattr(feed, "last_generated", None)
            if last_gen is not None:
                try:
                    age = (now - last_gen).total_seconds()
                    if age > STALE_AFTER_SEC:
                        log_event(
                            logger,
                            "transit.subway.feed.stale",
                            station=self._station_id,
                            feed=group,
                            age_s=int(age),
                        )
                        continue
                except TypeError:
                    pass

            any_feed_succeeded = True

            # Within the feed, query each line in the group that we
            # care about, for each direction.
            for line in lines_to_poll:
                if LINE_TO_FEED.get(line) != group:
                    continue
                for ns in directions:
                    platform_id = f"{self._station_id}{ns}"
                    direction_label = self._direction_label(ns)
                    try:
                        trips = feed.filter_trips(
                            line_id=line,
                            headed_for_stop_id=platform_id,
                            underway=True,
                        )
                    except Exception as e:  # noqa: BLE001
                        log_event(
                            logger,
                            "transit.subway.filter.error",
                            line=line,
                            platform=platform_id,
                            err=repr(e),
                        )
                        continue
                    for trip in trips:
                        for stu in getattr(trip, "stop_time_updates", []) or []:
                            if getattr(stu, "stop_id", None) != platform_id:
                                continue
                            ts = (
                                getattr(stu, "arrival", None)
                                or getattr(stu, "departure", None)
                            )
                            if ts is None:
                                continue
                            try:
                                delta = (ts - now).total_seconds()
                            except TypeError:
                                continue
                            if delta <= 0:
                                continue
                            arrivals.append({
                                "line": line,
                                "direction": ns,
                                "direction_label": direction_label,
                                "minutes_from_now": round(delta / 60),
                            })

        if not any_feed_succeeded:
            return None

        arrivals.sort(key=lambda a: a["minutes_from_now"])
        return arrivals[:ARRIVAL_LIMIT]

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
            feed = _GTFSRealtimeFeed(line)
        else:
            feed = self._feed_factory(line)
        self._feed_cache[line] = (feed, now_mono)
        return feed
