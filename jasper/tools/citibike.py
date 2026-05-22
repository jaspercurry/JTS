"""Citi Bike voice tool.

One tool, `get_citibike_status`, that returns live availability at
the saved stations parsed from `JASPER_CITIBIKE_STATIONS`. The
factory short-circuits to `[]` when no stations are saved, so the
LLM never sees a tool that would always return an empty stations
list — consistent with `make_subway_tools` / `make_bus_tools`.

`CitiBikeClient.get_status` is sync (GBFS is two CDN requests with
in-process caching; no parallel fan-out to warrant an AsyncClient).
The tool wraps it in `asyncio.to_thread` so the realtime LLM
session never blocks — mirrors the `jasper.subway` pattern.
"""
from __future__ import annotations

import asyncio
import logging

from ..citibike import CitiBikeClient
from ..transit.base import TransitError
from . import tool

logger = logging.getLogger(__name__)


def make_citibike_tools(client: CitiBikeClient | None):
    """Build the citi-bike status tool backed by a CitiBikeClient.

    Returns an empty list when no Citi Bike stations are configured
    (cleared or never set) — so the model never sees a tool whose
    every call would return zero stations."""
    if client is None or not client.enabled:
        return []

    @tool()
    async def get_citibike_status(station_label: str = "") -> dict:
        """Return live Citi Bike availability for the speaker's saved stations.

        Citi Bike is NYC + Jersey City + Hoboken's docked bikeshare.
        Each saved station's response carries separate counts for
        classic (pedal-only) bikes and e-bikes (battery-assisted),
        plus open docks. Call this for any question about Citi
        Bike, bike share, available bikes, e-bikes, or docks — both
        general ('what's the Citi Bike situation?') and station-
        specific ('any bikes at 9 Av?').

        Args:
          station_label (optional): a substring matching one of the
            speaker's saved station labels (case-insensitive). Empty
            string => report every saved station. Pass the user's
            spoken phrase verbatim — '9 Av', 'Atlantic', 'the corner
            one' — the filter is forgiving.

        Response shape:
          {
            stations: [
              {label, station_id, ebikes, docks,
               classic_bikes,  # OMITTED when ebike_only_mode=true
               status: "ok" | "offline" | "missing",
               last_reported_age_seconds},
              ...
            ],
            ebike_only_mode: bool,    # household-wide preference
            filter: str,              # echoed back
            no_match: bool,           # true when filter excluded all
          }
          When `ebike_only_mode` is true, the household only rides
          e-bikes — `classic_bikes` is omitted from each station and
          you should speak ONLY the e-bike count for that station.

        Voice answer style:
          # General query, multiple saved stations:
          '9 Av has 3 e-bikes and 5 classic; Atlantic has 2 e-bikes
            and 4 classic.'
          # Single-station query:
          '9 Av: 3 e-bikes, 5 classic.'
          # Station offline:
          'Atlantic is offline right now.'
          # E-bike-only mode (classic_bikes omitted from response):
          '9 Av has 3 e-bikes; Atlantic has 2.'
          # Stale data (last_reported_age_seconds > 120):
          'As of two minutes ago, 9 Av had 3 e-bikes and 5 classic.'
          # No-match filter:
          'I don't have a saved station matching "9 Av and 41 St".'

        Always mention BOTH e-bike and classic counts when both are
        present (unless `ebike_only_mode` is true). When one is zero
        it's fine to name only the non-zero kind ('5 classic bikes'
        rather than '5 classic bikes and no e-bikes'). For docks,
        report one number ('8 open docks') — the e-bike/classic
        split doesn't apply to docks. Mention docks only when the
        user asked about them OR when one of the saved stations has
        3 or fewer docks ('running low on docks at 9 Av').

        ALWAYS call this tool fresh on every Citi Bike question —
        counts change minute-to-minute.

        On error returns {error: ...}; speak the error verbatim so
        the user knows what to clarify."""
        try:
            stations = await asyncio.to_thread(
                client.get_status, station_filter=station_label,
            )
        except TransitError as exc:
            # Both feeds missing AND no cache anywhere — fetcher
            # already logged the underlying outcome bucket. Surface
            # a single LLM-visible error string; voice prompt says
            # "speak the error verbatim".
            logger.warning(
                "event=transit.citibike.tool.error outcome=fetch_failed "
                "filter=%r err=%s",
                station_label, exc,
            )
            return {"error": f"Citi Bike data is unavailable: {exc}"}

        no_match = bool(station_label.strip()) and not stations
        logger.info(
            "event=transit.citibike.tool.result filter=%r returned=%d no_match=%s "
            "ebike_only=%s",
            station_label, len(stations), no_match, client.ebike_only,
        )
        return {
            "stations": [
                s.as_dict(include_classic=not client.ebike_only)
                for s in stations
            ],
            "ebike_only_mode": client.ebike_only,
            "filter": station_label,
            "no_match": no_match,
        }

    return [get_citibike_status]
