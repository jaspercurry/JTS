from __future__ import annotations

import logging

from ..transit.base import TransitError
from . import tool

logger = logging.getLogger(__name__)


def make_bus_tools(bus):
    """Build the bus-arrivals tool backed by a BusClient. Returns an
    empty list when buses aren't configured for the speaker (no API
    key, no stops) — so the model never sees a tool whose every call
    would fail."""
    if bus is None or not bus.enabled:
        return []

    @tool(labels=("transit", "nyc", "bus"))
    async def get_bus_arrivals(route: str = "") -> dict:
        """Return the next bus arrivals across the speaker's
        configured bus stops, sorted by ETA, capped at 4 total.

        Call for any "next bus" / "when's the bus" / "is the bus
        coming" question. Call fresh on every bus question — never
        reuse a prior result, even from seconds ago. Bus arrivals
        are real-time; minutes count down since the last call.

        `route` is optional — a single short route name like 'B35',
        'B70', 'M15'. Empty string returns every route at every
        configured stop. Bare "next bus" questions should pass an
        empty string. The tool unions arrivals across all saved
        stops, so a user with both eastbound and westbound stops at
        their corner gets both directions in one answer.

        Response shape:
          {
            stops_queried: ["302680", "302682"],
            arrivals: [
              {route, destination, minutes_from_now,
               presentable_distance, stops_from_call,
               stop_id, stop_label}, ...
            ]
          }
          Each arrival carries its own `stop_label` so the voice
          answer can say which stop each bus is at — important when
          the user has opposing-direction stops at the same corner.

        Voice answer style. Walk the FULL `arrivals` list and speak
        each bus with route + minutes. Use `minutes_from_now`;
        IGNORE `presentable_distance` and `stops_from_call` — the
        user wants minutes, not stops or miles. Never say "stops
        away" or "miles away".

        For a bus at 0 minutes, say "approaching" or "now" instead
        of "0 minutes".

        Name `stop_label` inline when multiple stops are configured
        AND arrivals from different stops appear in one response.
        Skip the label when all arrivals share one stop OR the
        user's question already pins the stop.

        Examples:
          'B35 westbound at 4 Av/39 St in 4 minutes, B70 eastbound
            in 7.'                                  (multi-stop)
          'B35 at the eastbound stop in 2, then a B35 at the
            westbound stop in 5.'                   (multi-stop)
          'B35 in 3, B70 in 7.'                     (single stop)
          'B35 approaching now.'                    (0 minutes)
          'No buses in the next half hour.'         (empty list)

        On error returns {error: ...}; speak the error verbatim so the
        user knows what happened. The error fires only on a total
        BusTime outage (every configured stop unreachable); a reachable
        feed with nothing coming returns an empty `arrivals` list — say
        'no buses in the next half hour' for that, NOT the error.
        """
        try:
            arrivals = await bus.get_arrivals(route)
        except TransitError as exc:
            # Every configured stop failed AND none had a cache to serve
            # — a total BusTime outage. Surface a single LLM-visible
            # error string rather than narrating it as "no buses"; the
            # voice prompt says "speak the error verbatim".
            logger.warning(
                "event=transit.bus.tool.error outcome=fetch_failed "
                "route=%r err=%s",
                route, exc,
            )
            return {
                "error": (
                    "I can't reach the MTA bus feed right now. "
                    "Try again in a moment."
                )
            }
        return {
            "stops_queried": list(bus.stop_ids),
            "arrivals": [a.as_dict() for a in arrivals],
        }

    return [get_bus_arrivals]
