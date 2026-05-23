from __future__ import annotations

import asyncio

from . import tool


def make_subway_tools(subway):
    if subway is None:
        return []

    @tool()
    async def get_subway_arrivals(line: str = "", direction: str = "") -> dict:
        """Return the next subway arrivals at the speaker's home
        station.

        Call for any "next train" / "subway" / "when's the X
        coming" question. Call fresh on every train question —
        never reuse a prior result, even from seconds ago. Train
        arrivals are real-time; minutes count down since the last
        call.

        Both arguments are optional. Defaults match the user's
        natural "next train" question: every line stopping at the
        station, in whichever direction(s) the speaker is configured
        for. Bare "next train" questions should pass empty strings
        for both — defaults fill in.

          line       — single letter or number, e.g. 'D', 'F', 'N',
                       '4', '7'. Empty string → all lines stopping
                       at the station (including trains rerouted
                       from other lines during service changes —
                       those still physically arrive at this stop
                       and the tool surfaces them with their actual
                       route name).
          direction  — 'uptown' / 'downtown' / 'north' / 'south' /
                       'both' / station-specific terms like 'toward
                       Manhattan' / 'toward Coney Island'. Empty
                       string → uses the speaker's configured
                       default direction (or both if no default).

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
          Arrivals are sorted by ETA ascending and capped at 4
          total across all queried directions.

        Voice answer style. Walk the FULL `arrivals` list and speak
        EVERY arrival the tool returned. The tool has already capped
        the list at the right size for a one-sentence answer, so
        count-in equals count-out. Reading fewer hides live data
        from the user.

        Name the line for each train when multiple lines are coming
        (rerouted train mixed with regulars, or multi-line station).
        Name the direction when the query asked for both directions
        or context would be ambiguous. Skip naming when the user's
        question already pinned it ("next D uptown?" → "in 3, 7,
        12, and 16 minutes.").

        Examples:
          'Manhattan-bound D in 3, N in 7, Coney-bound D in 5,
            R in 9.'                       (both directions, mixed)
          'D in 3, N in 7 — both Manhattan-bound.'
          'D Manhattan-bound in 3, D Coney-bound in 5.'
          'In 3, 7, 12, and 16 minutes.'   (user pinned line+dir)
          'No upcoming trains — service might be paused.'  (empty)

        On error returns {error: ...}; speak the error verbatim so
        the user knows what to clarify.
        """
        return await asyncio.to_thread(subway.get_arrivals, line, direction)

    return [get_subway_arrivals]
