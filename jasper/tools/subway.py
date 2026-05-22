from __future__ import annotations

import asyncio

from . import tool


def make_subway_tools(subway):
    if subway is None:
        return []

    @tool()
    async def get_subway_arrivals(line: str = "", direction: str = "") -> dict:
        """Return the next subway arrivals at the speaker's home station.

        Both arguments are optional. Defaults match the user's natural
        "next train" question: every line stopping at the station, in
        whichever direction(s) the speaker is configured for.

          line       — single letter or number, e.g. 'D', 'F', 'N', '4',
                       '7'. Empty string → all lines stopping at the
                       station (including trains rerouted from other
                       lines during service changes — those still
                       physically arrive at this stop and the tool
                       surfaces them with their actual route name).
          direction  — 'uptown' / 'downtown' / 'north' / 'south' / 'both'
                       / station-specific terms like 'toward Manhattan'
                       / 'toward Coney Island'. Empty string → uses the
                       speaker's configured default direction (or both
                       if no default is configured).

        Bare 'next train' / 'when's the next train' questions should
        pass empty strings for both — defaults fill in.

        Response shape:
          {
            station: str,
            directions_queried: ["N"] / ["S"] / ["N", "S"],
            arrivals: [
              {line: "D", direction: "N",
               direction_label: "Manhattan",
               minutes_from_now: 3},
              ...
            ],
            source: "subwaynow" | "nyct-gtfs",
          }
          Arrivals are sorted by ETA ascending and capped at 4 total
          across all queried directions.

        Voice answer style:
          'Next at 9 Av: D in 3, N in 7 — both Manhattan-bound.'
          'D Manhattan-bound in 3, D Coney-bound in 5.'   (both directions)
          'No upcoming trains right now — service might be paused.'

        Name each train's line + direction when it would otherwise be
        ambiguous (multi-line station, rerouted train, both-directions
        query). Skip naming when the user's question already pins it
        down ("next D uptown" → just "D in 3 and 7.").

        ALWAYS call this tool fresh on every train question — never
        reuse a prior result, even from seconds ago. Train arrivals
        are real-time; minutes count down since the last call.

        On error returns {error: ...}; speak the error verbatim so
        the user knows what to clarify.
        """
        return await asyncio.to_thread(subway.get_arrivals, line, direction)

    return [get_subway_arrivals]
