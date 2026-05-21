"""Current-time tool for the voice loop.

The realtime LLMs have no internal clock — the timestamp injected
into their system prompt is baked at session-open time and goes
stale over the session's lifetime (potentially many hours, with
idle context-reset disabled by default per
docs/HANDOFF-persistent-live-session.md). Without a tool to read
fresh wall-clock time, the model speaks the baked stale timestamp
on every "what time is it?" / "what day is it?" question.

This tool is the fresh-time path. It's deliberately trivial — no
HTTP, no IO, just `datetime.now()` — so it's fast (~1 ms) and has
no failure modes worth handling.
"""
from __future__ import annotations

from datetime import datetime

from . import tool


def make_time_tools():
    """Return the time-tool list (currently one).

    Factory shape mirrors the other tool modules so wiring in
    `jasper.voice_daemon._build_tool_registry` looks uniform: every
    tool module has a `make_*_tools(...)` entry point."""

    @tool()
    async def get_current_time() -> dict:
        """Return the current local date, time, and day-of-week.

        Call this for ANY question about the current time, day, or
        date — "what time is it", "what day is it", "what's today's
        date", "is it morning yet", etc. The realtime model's
        internal clock is the session-open timestamp from the system
        prompt; it goes stale within hours. Always prefer this tool
        over the session-open hint.

        Response shape:
          local_time: ISO 8601 local time, minute-resolution
                      (e.g. "2026-05-21T15:47"). Strip seconds —
                      they aren't useful for the user and adding
                      them invites the model to read them out.
          timezone: IANA-style label or short name as the OS
                    reports it (e.g. "EDT", "PST", "UTC").
          day_of_week: full name (e.g. "Thursday").

        Voice answer style:
          'It's 3:47 PM.'
          'It's Thursday, May 21.'
          'It's a quarter past 7.'  (round if natural)
        """
        now = datetime.now().astimezone()
        return {
            "local_time": now.strftime("%Y-%m-%dT%H:%M"),
            "timezone": now.tzname() or "",
            "day_of_week": now.strftime("%A"),
        }

    return [get_current_time]
