"""Citi Bike transit provider — keyless, GBFS-backed.

Implements `TransitProvider` for the wizard at `/transit/`. The
GBFS fetcher and runtime client live in `jasper.citibike`; this
module is the thin wizard adapter that uses the same fetcher to
present "nearest stations" with a live snapshot of capacity.

Coverage spans NYC (5 boroughs) + Jersey City + Hoboken — one
network across all three under Lyft's operation. Other GBFS systems
Lyft also runs (Capital Bikeshare DC, BIXI Montreal, BAY Wheels SF)
are intentionally NOT collapsed into a single mega-provider: each
deserves its own bbox, its own provider module, its own wizard card
so users in those cities get an integration that names their system
specifically. Adding one means a new module here + one line in
`jasper.transit.REGISTRY`.
"""
from __future__ import annotations

import logging

from ...citibike import (
    INFO_TTL_SECONDS,
    STATION_INFO_URL,
    STATION_STATUS_URL,
    STATUS_TTL_SECONDS,
    fetch_feed,
)
from ..base import BoundingBox, Stop, haversine_miles

logger = logging.getLogger(__name__)


# Bbox is generous on purpose: rough rectangle from the Verrazzano
# bridge up to Inwood + west to Jersey City/Hoboken. The wizard
# already drops providers whose nearest stop is unreasonably far,
# so over-coverage just means the picker renders with a "no nearby
# stations" line rather than a misleading false-negative.
CITIBIKE_BBOX = BoundingBox(
    lat_min=40.62, lat_max=40.83,
    lon_min=-74.10, lon_max=-73.85,
)


class _CitiBike:
    id = "citibike"
    label = "Citi Bike"
    kind = "bike"
    help_url = "https://citibikenyc.com/system-data"
    bbox = CITIBIKE_BBOX
    env_keys = (
        # Multi-station pipe-list, same format as JASPER_BUS_STOPS.
        "JASPER_CITIBIKE_STATIONS",
        # "1" → suppress classic-bike counts in voice answers (the
        # household only rides e-bikes). Global, not per-station —
        # the per-station variant was considered (different walking
        # distances → different e-bike requirements) but the global
        # flag is simpler and the user explicitly chose it.
        "JASPER_CITIBIKE_EBIKE_ONLY",
    )
    credentials = ()  # GBFS is public; no key needed.

    def find_stops_near(
        self,
        lat: float,
        lon: float,
        *,
        credentials: dict[str, str] | None = None,
        count: int = 10,
    ) -> list[Stop]:
        info = fetch_feed(STATION_INFO_URL, INFO_TTL_SECONDS)
        status = fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS)
        status_by_id = {
            s["station_id"]: s
            for s in (status.get("data") or {}).get("stations", [])
            if isinstance(s, dict) and "station_id" in s
        }
        ranked: list[tuple[float, Stop]] = []
        for s in (info.get("data") or {}).get("stations", []):
            if not isinstance(s, dict):
                continue
            try:
                sid = str(s["station_id"])
                slat = float(s["lat"])
                slon = float(s["lon"])
                name = str(s["name"])
            except (KeyError, TypeError, ValueError):
                continue
            st = status_by_id.get(sid)
            if st is None or not bool(st.get("is_installed", 1)):
                continue
            d = haversine_miles(lat, lon, slat, slon)
            bikes = int(st.get("num_bikes_available", 0) or 0)
            ebikes = int(st.get("num_ebikes_available", 0) or 0)
            docks = int(st.get("num_docks_available", 0) or 0)
            classic = max(0, bikes - ebikes)
            # The wizard's stop renderer surfaces `lines` verbatim so
            # the user picking stations sees representative state at
            # render time. The picker's snapshot is informational only
            # — the live tool re-fetches at every query, so a station
            # showing 0 bikes at picker time still gets reported live.
            snapshot = f"{classic} classic, {ebikes} e-bikes, {docks} docks"
            ranked.append((d, Stop(
                stop_id=sid,
                display_name=name,
                lat=slat, lon=slon,
                distance_mi=d,
                lines=(snapshot,),
                name=name,
            )))
        ranked.sort(key=lambda t: t[0])
        return [stop for _, stop in ranked[:count]]

    def validate_credentials(
        self, credentials: dict[str, str],
    ) -> dict[str, str] | None:
        # Mirror nyc_subway: keyless providers report any pasted key
        # as rejected rather than raising, so the wizard's UX stays
        # consistent across keyless/credentialed providers.
        return {k: "citibike is keyless" for k in credentials} or None


PROVIDER = _CitiBike()
