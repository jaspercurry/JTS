"""NYC MTA bus real-time arrivals via the BusTime SIRI API.

One source of truth, no fallback (unlike subway, which has both
Subway Now and nyct-gtfs). The MTA's BusTime is the canonical
authority for bus ETAs — what their own bus-prediction signs use
and what every NYC bus app pulls from.

**v2 — multi-stop fan-out.** The client holds a list of configured
stops (typically opposing-direction stops at the same intersection).
Every `get_arrivals` call fans out to all stops in parallel and
unions the results, sorted by ETA and capped at a limit. Each
arrival carries its own `stop_id` + `stop_label` so the voice model
can say which stop each bus is at:

    "B35 westbound in 4 minutes at 4 Av/39 St, B70 eastbound in 7."

API endpoint per stop:
  https://bustime-classic.mta.info/api/siri/stop-monitoring.json
    ?key=<MTA_BUSTIME_KEY>
    &MonitoringRef=<numeric-stop-id, e.g. 302680>
    &OperatorRef=MTA

SIRI is single-`MonitoringRef` per call (no batch endpoint exists
per MTA's BusTime wiki); fan-out is intrinsic when configuring
multiple stops. Per-stop caches stay independent so a fast user
re-query for a different stop doesn't bust the others.

Response surface (the bits we use, paraphrased):

  Siri.ServiceDelivery.StopMonitoringDelivery[0].MonitoredStopVisit[i]
    .MonitoredVehicleJourney
      .PublishedLineName        ("B35" / "B70" / etc.)
      .DestinationName          ("BROWNSVILLE M GASTON via CHURCH")
      .MonitoredCall
        .ExpectedArrivalTime    ISO 8601 wall-clock
        .Extensions.Distances
          .PresentableDistance  ("approaching" / "1 stop away" / ...)
          .StopsFromCall        numeric stops away
          .DistanceFromCall     metres away
"""
from __future__ import annotations

import asyncio
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

# Default voice-answer cap. Picked to match subway's ARRIVAL_LIMIT
# and to fit a one-sentence response without dragging.
DEFAULT_LIMIT = 4


@dataclass
class BusArrival:
    """One upcoming bus visit at one of the configured stops."""
    route: str                  # "B35", "B70" — short name, voice-friendly
    destination: str            # raw DestinationName from MTA
    minutes_from_now: int       # rounded, never negative
    presentable_distance: str   # "approaching" / "0.7 miles away" / etc.
    stops_from_call: int | None  # numeric stops away if available
    stop_id: str                # bare numeric (no MTA_ prefix)
    stop_label: str             # e.g., "4 Av/39 St eastbound"

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "destination": self.destination,
            "minutes_from_now": self.minutes_from_now,
            "presentable_distance": self.presentable_distance,
            "stops_from_call": self.stops_from_call,
            "stop_id": self.stop_id,
            "stop_label": self.stop_label,
        }


def parse_bus_stops(raw: str) -> list[tuple[str, str]]:
    """Parse the JASPER_BUS_STOPS env var into a list of (stop_id, label).

    Each stop is `id|label`; stops are comma-separated. Labels are
    optional (a bare id is fine). Empty entries and whitespace are
    tolerated. MTA stop names don't contain `|` or `,` so this is
    safe; the wizard sanitises labels on save just in case.

    Examples:
        "MTA_302680|4 Av/39 St eastbound,MTA_302682|4 Av/39 St westbound"
        "MTA_302680,MTA_302682"          # no labels
        "MTA_302680|4 Av/39 St"          # one stop with label
    """
    out: list[tuple[str, str]] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "|" in token:
            sid, _, label = token.partition("|")
            out.append((sid.strip(), label.strip()))
        else:
            out.append((token, ""))
    return out


class BusClient:
    """SIRI arrivals across one or more configured bus stops.

    Holds a flat list of stops; every query fans out in parallel,
    unions, sorts by ETA, caps at `limit`. Tests inject `http` (an
    `httpx.AsyncClient` wired to MockTransport) and `clock` (for
    deterministic ETA→minutes math)."""

    def __init__(
        self,
        stop_ids: list[str],
        api_key: str,
        stop_labels: dict[str, str] | None = None,
        http: httpx.AsyncClient | None = None,
        clock=None,
    ) -> None:
        # Normalise stop IDs: strip the OBA "MTA_" prefix that SIRI
        # doesn't accept on the MonitoringRef. Accept either form in
        # config so users can paste IDs they see in OBA responses or
        # on physical bus-stop signs.
        normalised: list[str] = []
        for sid in stop_ids or []:
            s = (sid or "").strip().removeprefix("MTA_")
            if s:
                normalised.append(s)
        self._stop_ids: tuple[str, ...] = tuple(normalised)
        # Stop labels are keyed by the NORMALISED id so callers can
        # look them up regardless of whether they pass MTA_ or bare.
        self._stop_labels: dict[str, str] = {
            (k or "").strip().removeprefix("MTA_"): (v or "").strip()
            for k, v in (stop_labels or {}).items()
        }
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(timeout=BUSTIME_TIMEOUT)
        self._owns_http = http is None
        self._clock = clock or (lambda: datetime.now().astimezone())
        # Per-stop cache: stop_id (normalised) -> (mono_ts, list[BusArrival]).
        self._cache: dict[str, tuple[float, list[BusArrival]]] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def stop_ids(self) -> tuple[str, ...]:
        """Normalised stop ids the client polls. Read-only accessor for
        consumers (e.g. the bus tool) that want to surface configured
        stops in their response."""
        return self._stop_ids

    @property
    def enabled(self) -> bool:
        return bool(self._stop_ids and self._api_key)

    def label_for(self, stop_id: str) -> str:
        """Human label for a stop id (bare or MTA_-prefixed). Falls
        back to the bare id when no label was configured."""
        bare = (stop_id or "").strip().removeprefix("MTA_")
        return self._stop_labels.get(bare) or bare

    async def get_arrivals(
        self, route: str = "", limit: int = DEFAULT_LIMIT,
    ) -> list[BusArrival]:
        """Return upcoming arrivals across all configured stops, sorted
        by ETA ascending, capped at `limit`.

        `route` (optional): filter to a single short name like 'B35'.
        Empty string returns every route at every stop. v1 had a
        global `configured_routes` allow-list; that's gone — pick
        direction-specific stops (which are already route-shaped)
        and lean on the post-fetch `route` arg for ad-hoc filtering."""
        if not self.enabled:
            return []

        # Fan out in parallel. asyncio.gather preserves order but we
        # don't depend on it — flat-extend handles whatever ordering.
        tasks = [
            self._fetch_for_stop_cached(sid) for sid in self._stop_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        arrivals: list[BusArrival] = []
        for r in results:
            if isinstance(r, BaseException):
                # Per-stop failure shouldn't kill the others; we logged
                # the cause inside _fetch_for_stop already.
                continue
            arrivals.extend(r)

        if route:
            target = route.strip().upper()
            arrivals = [a for a in arrivals if a.route.upper() == target]

        arrivals.sort(key=lambda a: a.minutes_from_now)
        return arrivals[:limit]

    async def _fetch_for_stop_cached(self, stop_id: str) -> list[BusArrival]:
        now_mono = time.monotonic()
        cached = self._cache.get(stop_id)
        if cached is not None and now_mono - cached[0] < CACHE_WINDOW_SEC:
            return cached[1]
        arrivals = await self._fetch_for_stop(stop_id)
        self._cache[stop_id] = (now_mono, arrivals)
        return arrivals

    async def _fetch_for_stop(self, stop_id: str) -> list[BusArrival]:
        params = {
            "key": self._api_key,
            "MonitoringRef": stop_id,
            "OperatorRef": "MTA",
        }
        headers = {"User-Agent": BUSTIME_AGENT}
        try:
            r = await self._http.get(BUSTIME_URL, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            logger.warning("bus: BusTime fetch failed for %s: %r", stop_id, e)
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning("bus: BusTime JSON parse failed for %s: %r", stop_id, e)
            return []

        return self._parse_siri(data, stop_id)

    def _parse_siri(self, data: dict, stop_id: str) -> list[BusArrival]:
        sd = (
            (data or {}).get("Siri", {})
            .get("ServiceDelivery", {})
            .get("StopMonitoringDelivery") or [{}]
        )[0]
        visits = sd.get("MonitoredStopVisit") or []
        now = self._clock()
        label = self.label_for(stop_id)
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
                stop_id=stop_id,
                stop_label=label,
            ))
        # Sort within the per-stop result so each cache entry is
        # already in ETA order; the outer union resorts after merging.
        out.sort(key=lambda a: a.minutes_from_now)
        return out
