"""NYC MTA bus real-time arrivals via the BusTime SIRI API.

One source of truth, no fallback (unlike subway, which has both
Subway Now and nyct-gtfs). The MTA's BusTime is the canonical
authority for bus ETAs — it's what their own bus-prediction signs
use and what every NYC bus app pulls from. No third-party
alternative offers more useful data.

API endpoint:
  https://bustime-classic.mta.info/api/siri/stop-monitoring.json
    ?key=<MTA_BUSTIME_KEY>
    &MonitoringRef=<numeric-stop-id, e.g. 302680>
    &OperatorRef=MTA

Free, requires API key (signup at bustime.mta.info/wiki/Developers/Index,
~30 min manual approval). No published rate limits but reasonable
use expected. 20-second in-process cache here keeps us well under
any plausible ceiling for voice-rate queries.

Response surface (the bits we use, paraphrased):

  Siri.ServiceDelivery.StopMonitoringDelivery[0].MonitoredStopVisit[i]
    .MonitoredVehicleJourney
      .PublishedLineName        ("B35" / "B70" / etc.)
      .DestinationName          ("BROWNSVILLE M GASTON via CHURCH")
      .DirectionRef             ("0" or "1")
      .MonitoredCall
        .ExpectedArrivalTime    ISO 8601 wall-clock
        .Extensions.Distances
          .PresentableDistance  ("approaching" / "1 stop away" /
                                 "0.7 miles away" /
                                 "1.5 miles, 4 stops away")
          .StopsFromCall        numeric stops away
          .DistanceFromCall     metres away

The PresentableDistance is the NYC-specific format that gives users
genuinely useful info — "approaching" matters more than "arriving in
1 min" in practice. We pass it through so the voice answer can pick
the friendlier framing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


BUSTIME_URL = (
    "https://bustime-classic.mta.info/api/siri/stop-monitoring.json"
)
BUSTIME_AGENT = "jts-jasper/1.0"
BUSTIME_TIMEOUT = 3.0

# Cache the per-stop response for this long. Voice queries cluster
# (user asks twice in 10s) and SIRI updates every ~30s anyway.
CACHE_WINDOW_SEC = 20

# Reject buses that arrived more than this many seconds before "now".
# SIRI keeps showing a bus for a brief grace period after it passes —
# useful for analytics, confusing for voice ("next bus" should never
# include one that already left). 30 s of slack absorbs clock skew.
PAST_GRACE_SEC = 30


@dataclass
class BusArrival:
    """One upcoming bus visit at the configured stop."""
    route: str                  # "B35", "B70" — short name, voice-friendly
    destination: str            # raw DestinationName from MTA
    minutes_from_now: int       # rounded, never negative
    presentable_distance: str   # "approaching" / "0.7 miles away" / etc.
    stops_from_call: int | None  # numeric stops away if available

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "destination": self.destination,
            "minutes_from_now": self.minutes_from_now,
            "presentable_distance": self.presentable_distance,
            "stops_from_call": self.stops_from_call,
        }


class BusClient:
    """MTA BusTime SIRI arrivals at the speaker's configured bus stop.

    Stateless-ish — owns an httpx.AsyncClient and a small per-stop
    cache. The stop ID is set at construction (one stop per speaker
    for v1; future versions can configure multiple stops once the
    /buses wizard lands)."""

    def __init__(
        self,
        stop_id: str,
        api_key: str,
        configured_routes: list[str] | None = None,
        http: httpx.AsyncClient | None = None,
        clock=None,
    ) -> None:
        # Stop IDs in OneBusAway have an "MTA_" prefix (e.g. "MTA_302680")
        # but the SIRI MonitoringRef wants the bare numeric ("302680").
        # Accept both forms in config — strip the prefix here so the
        # operator can paste either.
        self._stop_id = stop_id.removeprefix("MTA_") if stop_id else ""
        self._api_key = api_key
        self._configured_routes = (
            tuple(r.strip().upper() for r in configured_routes if r.strip())
            if configured_routes else ()
        )
        self._http = http or httpx.AsyncClient(timeout=BUSTIME_TIMEOUT)
        self._owns_http = http is None
        self._clock = clock or (lambda: datetime.now().astimezone())
        # (timestamp_monotonic, list[BusArrival])
        self._cache: tuple[float, list[BusArrival]] | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def stop_id(self) -> str:
        """The normalized numeric stop id (no ``MTA_`` prefix). Read-only
        accessor for consumers (e.g. the bus tool) that want to surface
        the configured stop in their response."""
        return self._stop_id

    @property
    def enabled(self) -> bool:
        return bool(self._stop_id and self._api_key)

    async def get_arrivals(
        self, route: str = "", limit: int = 4,
    ) -> list[BusArrival]:
        """Return upcoming arrivals at the configured stop.

        `route` (optional): filter to a single short route name
        (e.g. "B35"). Empty string returns all configured routes.

        `limit`: maximum number of arrivals to return after filtering.
        4 is enough for "next few" answers without dragging the
        spoken response."""
        if not self.enabled:
            return []

        now_mono = time.monotonic()
        # Cache check
        if self._cache is not None:
            cached_at, cached = self._cache
            if now_mono - cached_at < CACHE_WINDOW_SEC:
                arrivals = cached
            else:
                arrivals = await self._fetch_and_parse()
                self._cache = (now_mono, arrivals)
        else:
            arrivals = await self._fetch_and_parse()
            self._cache = (now_mono, arrivals)

        # Filter
        target = route.strip().upper() if route else ""
        if target:
            arrivals = [a for a in arrivals if a.route.upper() == target]
        elif self._configured_routes:
            arrivals = [
                a for a in arrivals
                if a.route.upper() in self._configured_routes
            ]
        return arrivals[:limit]

    async def _fetch_and_parse(self) -> list[BusArrival]:
        params = {
            "key": self._api_key,
            "MonitoringRef": self._stop_id,
            "OperatorRef": "MTA",
        }
        headers = {"User-Agent": BUSTIME_AGENT}
        try:
            r = await self._http.get(BUSTIME_URL, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            logger.warning("bus: BusTime fetch failed: %r", e)
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("bus: BusTime JSON parse failed: %r", e)
            return []

        sd = (
            data.get("Siri", {})
            .get("ServiceDelivery", {})
            .get("StopMonitoringDelivery") or [{}]
        )[0]
        visits = sd.get("MonitoredStopVisit") or []
        now = self._clock()
        out: list[BusArrival] = []
        for v in visits:
            journey = v.get("MonitoredVehicleJourney") or {}
            call = journey.get("MonitoredCall") or {}
            eta_str = call.get("ExpectedArrivalTime")
            if not eta_str:
                continue
            try:
                eta_dt = datetime.fromisoformat(eta_str).astimezone()
            except (TypeError, ValueError):
                continue
            delta_sec = (eta_dt - now).total_seconds()
            if delta_sec < -PAST_GRACE_SEC:
                continue
            mins = max(0, round(delta_sec / 60))
            ext = (call.get("Extensions") or {}).get("Distances") or {}
            out.append(BusArrival(
                route=str(journey.get("PublishedLineName") or "?"),
                destination=str(journey.get("DestinationName") or "").strip(),
                minutes_from_now=mins,
                presentable_distance=str(ext.get("PresentableDistance") or ""),
                stops_from_call=ext.get("StopsFromCall"),
            ))
        # Sort by arrival time
        out.sort(key=lambda a: a.minutes_from_now)
        return out
