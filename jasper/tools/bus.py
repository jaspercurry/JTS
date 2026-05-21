from __future__ import annotations

from . import tool


def make_bus_tools(bus):
    """Build the bus-arrivals tool backed by a BusClient. Returns
    an empty list when buses aren't configured for the speaker
    (no API key, no stop id) — so the model never sees a tool whose
    every call would fail."""
    if bus is None or not bus.enabled:
        return []

    @tool()
    async def get_bus_arrivals(route: str = "") -> dict:
        """Return the next bus arrivals at the speaker's configured
        bus stop.

        `route` is optional — a single short route name like 'B35',
        'B70', 'M15'. Empty string returns all configured routes at
        the stop (the default; for v1 the speaker has one stop with
        multiple routes serving it).

        Bare 'next bus' / 'when's the next bus' questions should pass
        an empty string — the configured stop + route filter fill in.

        Response shape:
          {stop_id, arrivals: [
            {route, destination, minutes_from_now,
             presentable_distance, stops_from_call}, ...
          ]}

        Voice answer style:
          'Next B35 in 5 minutes, B70 in 8.'
          'Next bus is the B35, approaching now.'
          'Next bus in 3 minutes — that's the B35, 1 stop away.'

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
            "stop_id": bus._stop_id,
            "arrivals": [a.as_dict() for a in arrivals],
        }

    return [get_bus_arrivals]
