from __future__ import annotations

import asyncio

from . import tool


def make_subway_tools(subway):
    if subway is None:
        return []

    @tool()
    async def get_subway_arrivals(line: str = "", direction: str = "") -> dict:
        """Return the next subway arrivals at the speaker's home station.

        Both arguments are optional:
          line       — single letter or number, e.g. 'D', 'F', 'N', '4', '7'.
                       Empty string → defaults to the only line that stops
                       at this station. At a multi-line station, returns
                       an error asking which line.
          direction  — 'uptown', 'downtown', 'north', 'south', or
                       station-specific terms like 'toward Manhattan' /
                       'toward Coney Island'. Empty string → defaults to
                       the speaker's configured home direction.

        Bare 'next train' / 'when's the next train' questions should pass
        empty strings for both — the speaker's home station + default
        direction will fill in.

        Response shape:
          {line, direction, direction_label, station, next_arrivals_minutes,
           source}
          next_arrivals_minutes is a list like [5, 12, 19] (minutes from now,
          empty if no upcoming trains).

        Voice answer style:
          'Next uptown D trains at 9 Av in 5, 12, and 19 minutes.'
          'Next train in 4 minutes, then 11 and 17.'  (when context is clear)
          'No upcoming D trains right now — service might be paused.'

        On error returns {error: ...}; speak the error verbatim so the
        user knows what to clarify.
        """
        return await asyncio.to_thread(subway.get_arrivals, line, direction)

    return [get_subway_arrivals]
