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


GET_CITIBIKE_STATUS_LLM_DESCRIPTION = (
    "Return live Citi Bike availability for the speaker's saved NYC/Jersey "
    "City/Hoboken stations. Call fresh for Citi Bike, bikeshare, available "
    "bike, e-bike, or dock questions. station_label is optional; empty reports "
    "all saved stations, otherwise pass the user's spoken station phrase. "
    "Speak tight per-station counts; say 'no' instead of zero, respect "
    "ebike_only_mode, and report offline/missing/no_match states. Mention "
    "docks only when asked, full, or only 1-3 are open. On error, speak the "
    "error verbatim."
)


def make_citibike_tools(client: CitiBikeClient | None):
    """Build the citi-bike status tool backed by a CitiBikeClient.

    Returns an empty list when no Citi Bike stations are configured
    (cleared or never set) — so the model never sees a tool whose
    every call would return zero stations."""
    if client is None or not client.enabled:
        return []

    @tool(
        labels=("transit", "nyc", "bikeshare"),
        llm_description=GET_CITIBIKE_STATUS_LLM_DESCRIPTION,
    )
    async def get_citibike_status(station_label: str = "") -> dict:
        """Return live Citi Bike availability for the speaker's
        saved stations.

        Citi Bike is NYC + Jersey City + Hoboken's docked bikeshare.
        Each saved station's response carries separate counts for
        classic (pedal-only) bikes and e-bikes (battery-assisted),
        plus open docks. Call for any question about Citi Bike, bike
        share, available bikes, e-bikes, or docks — both general
        ("what's the Citi Bike situation?") and station-specific
        ("any bikes at 9 Av?"). Call fresh every time — counts
        change minute-to-minute.

        Args:
          station_label (optional): substring matching one of the
            speaker's saved station labels (case-insensitive). Empty
            string => report every saved station. Pass the user's
            spoken phrase verbatim — '9 Av', 'Atlantic', 'the corner
            one' — the filter is forgiving.

        Response shape:
          {
            stations: [
              {label, station_id, ebikes, docks, is_full, is_stale,
               classic_bikes,        # OMITTED when ebike_only_mode=true
               status: "ok" | "offline" | "missing",
               last_reported_age_seconds, no_match},
              ...
            ],
            ebike_only_mode: bool,    # household-wide preference
            filter: str,              # echoed back
            no_match: bool,           # true when filter excluded all
          }

        Voice answer style. Keep responses TIGHT and telegraphic.
        No preamble ("here's the status…"), no closer ("let me know
        if…"), no transitions ("also", "meanwhile"). Just the
        per-station data, comma-separated. The `label` field is
        already speech-friendly (abbreviations expanded, ordinals
        applied); read it verbatim. Use a colon between label and
        counts.

          Format:  '<label>: <ebike phrase>, <classic phrase>'
          Example: '9th Avenue: 3 e-bikes, 5 classic.'
          Multi-station: separate stations with periods.
                   '9th Avenue: 3 e-bikes, 5 classic. Atlantic
                    Avenue: 2 e-bikes, 4 classic.'

        ZERO-COUNT RULE: write "no" not "zero" and not "0". If
        `ebikes` is 0 say "no e-bikes" (not "zero e-bikes" / "0
        e-bikes"). Same for `classic_bikes`. If BOTH are 0, the
        station has no bikes at all → say "<label> has no bikes."

        EBIKE_ONLY_MODE RULE: when `ebike_only_mode` is TRUE the
        `classic_bikes` field is omitted — speak ONLY e-bike counts:
        "9th Avenue: 3 e-bikes. Atlantic: 2." If `ebikes` is 0 in
        this mode, say "<label> has no e-bikes."

        STATUS RULE: when `status` is 'offline' or 'missing', say
        "<label> is offline" / "<label> is gone" and don't read its
        counts.

        DOCKS RULE: don't mention docks unless EITHER (a) the user
        explicitly asked about docks, OR (b) `is_full` is TRUE, OR
        (c) `docks` is 1, 2, or 3. Use these phrases literally:
          is_full=TRUE          → "<label> is full"
          docks=1               → "only 1 dock open at <label>"
          docks=2 or 3          → "only <N> docks open at <label>"
          docks≥4 (not asked)   → don't mention docks
        DO NOT use "running low", "low on docks", "almost full",
        "tight", "limited", or any subjective qualifier. DO NOT say
        "only zero docks" — that's what `is_full` is for.

        STALENESS RULE: ONLY when a station has `is_stale` TRUE,
        preface that station's portion with "as of a few minutes
        ago". When every station has `is_stale` FALSE, do NOT
        mention freshness at all — silence is correct because the
        data is current. The `last_reported_age_seconds` field is
        informational; ignore it for narration unless `is_stale` is
        TRUE.

        NO-MATCH RULE: when `no_match` is true, say "I don't have a
        saved station matching <filter>."

        On error returns {error: ...}; speak the error verbatim so
        the user knows what to clarify.
        """
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
