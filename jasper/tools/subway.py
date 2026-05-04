from __future__ import annotations

import asyncio

from . import tool


def make_subway_tools(subway):
    if subway is None:
        return []

    @tool()
    async def get_subway_arrivals(line: str, direction: str = "") -> dict:
        """Return the next subway arrivals at the speaker's home station for
        the given line and direction.

        Arguments:
          line       — single letter or number, e.g. 'D', 'F', 'N', '4', '7'.
          direction  — optional. Accepts 'uptown', 'downtown', 'north',
                       'south', or station-specific terms like 'toward
                       Manhattan' / 'toward Coney Island'. Defaults to the
                       speaker's configured home direction.

        Response shape:
          {line, direction, direction_label, station, next_arrivals_minutes}
          next_arrivals_minutes is a list like [5, 12, 19] (minutes from now,
          empty if no upcoming trains).

        Voice answer style:
          'Next uptown D trains at 9 Av in 5, 12, and 19 minutes.'
          'No upcoming D trains right now — service might be paused.'

        On error returns {error: ...}; speak the error verbatim.
        """
        return await asyncio.to_thread(subway.get_arrivals, line, direction)

    return [get_subway_arrivals]
