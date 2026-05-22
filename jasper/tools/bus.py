from __future__ import annotations

from . import tool


def make_bus_tools(bus):
    """Build the bus-arrivals tool backed by a BusClient. Returns an
    empty list when buses aren't configured for the speaker (no API
    key, no stops) — so the model never sees a tool whose every call
    would fail."""
    if bus is None or not bus.enabled:
        return []

    @tool()
    async def get_bus_arrivals(route: str = "") -> dict:
        """Return the next bus arrivals across the speaker's configured
        bus stops, sorted by ETA, capped at 4 total.

        `route` is optional — a single short route name like 'B35',
        'B70', 'M15'. Empty string returns every route at every
        configured stop.

        Bare 'next bus' / 'when's the next bus' questions should pass
        an empty string. The tool unions arrivals across all saved
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
          the user has opposing-direction stops saved at the same
          intersection.

        Voice answer style:
          'B35 westbound in 4 minutes at 4 Av/39 St, B70 eastbound in 7.'
          'Next bus is the B35, approaching now at 4 Av/39 St eastbound.'
          'B35 in 3 minutes (1 stop away).'  (single stop, no ambiguity)

        Name the stop_label inline when multiple stops are configured
        OR when arrivals from different stops appear in one response;
        skip it when only one stop is queried OR the user's question
        already pins the stop.

        The `presentable_distance` field is the MTA's NYC-specific
        format ('approaching', '1 stop away', '0.7 miles away'). For
        nearby buses it's more honest than minute estimates — NYC
        traffic makes pure ETA unreliable, but 'approaching' /
        '1 stop away' is concrete. Prefer it when it's available
        AND the bus is close (≤ 3 stops or ≤ 0.5 miles); fall back
        to minutes for buses farther out.

        ALWAYS call this tool fresh on every bus question — never
        reuse a prior result, even from seconds ago. Bus arrivals
        are real-time; minutes count down since the last call.

        On error returns {error: ...}; speak the error verbatim so
        the user knows what to clarify."""
        arrivals = await bus.get_arrivals(route)
        return {
            "stops_queried": list(bus.stop_ids),
            "arrivals": [a.as_dict() for a in arrivals],
        }

    return [get_bus_arrivals]
