"""Timer voice tools — set, list, cancel kitchen timers.

Backed by `jasper.timers.TimerScheduler`, which owns persistence,
asyncio task lifecycle, AND the optional pre-render hook (so the
fire-time announcement WAV is cached before fire_at). This module
is the function-tool surface the voice loop sees.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import tool
from ..timers import announcement_text, human_duration

if TYPE_CHECKING:
    from ..timers import Timer, TimerScheduler

logger = logging.getLogger(__name__)


def _serialise(timer: "Timer") -> dict:
    """Tool-response shape for a single timer. Includes both raw
    seconds and pre-formatted strings so the model can pick whichever
    is easier to read aloud without doing duration math."""
    return {
        "id": timer.id,
        "label": timer.label,
        "duration": human_duration(timer.total_seconds),
        "duration_seconds": timer.total_seconds,
        "remaining": human_duration(timer.remaining_seconds),
        "remaining_seconds": timer.remaining_seconds,
    }


def _set_confirm(timer: "Timer") -> str:
    """Natural-English spoken confirmation for a freshly set timer.

    Compound-modifier form ("a 5-minute timer") only composes
    cleanly for single-unit durations; "Your 1-hour-and-30-minute
    pasta timer" reads worse than "Your pasta timer for 1 hour
    and 30 minutes". Use noun form universally for consistency.
    """
    duration = human_duration(timer.total_seconds)
    if timer.label:
        return f"Set a {timer.label} timer for {duration}."
    return f"Set a timer for {duration}."


def _cancel_confirm(timer: "Timer") -> str:
    if timer.label:
        return f"Cancelled the {timer.label} timer."
    duration = human_duration(timer.total_seconds)
    return f"Cancelled the timer for {duration}."


def make_timer_tools(scheduler: "TimerScheduler"):
    """Build the timer CRUD tools. Returns a list of decorated
    coroutines suitable for `ToolRegistry.register(...)`.

    The scheduler handles its own pre-render hook (set via
    `scheduler.set_pre_render(...)` on the daemon side); this
    module doesn't need a cue-manager reference."""

    @tool()
    async def set_timer(seconds: int, label: str = "") -> dict:
        """Schedule a timer that announces when it fires.

        Use for "set a timer for X minutes", "remind me in an hour",
        "ten minute timer", "X-minute pasta timer".

        `seconds` is the timer duration in seconds: "5 minutes" →
        300, "an hour" → 3600, "90 seconds" → 90. `label` is
        optional — when set ('pasta', 'laundry'), the fire-time
        announcement names it ("Your pasta timer is up"); when
        empty, the announcement uses the duration ("Your timer for
        5 minutes is up"). Multiple timers can run concurrently —
        a new one does not cancel existing ones.

        Voice answer style: speak the response's `confirm` field
        verbatim ("Set a pasta timer for 5 minutes."). The speaker
        plays the fire-time announcement automatically — DON'T
        promise to remind the user; the timer does it itself.

        Skip the preamble before calling this tool. The `confirm`
        field IS the spoken answer — a status sentence beforehand
        ("Sure, setting a 5 minute pasta timer…") restates it
        word-for-word.
        """
        try:
            timer = scheduler.add(int(seconds), label or None)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "confirm": _set_confirm(timer),
            **_serialise(timer),
        }

    @tool()
    async def list_timers() -> dict:
        """Return all active timers with remaining time.

        Use for "how much time left?", "what timers do I have?",
        "list my timers".

        Each timer has id, label (may be null), duration, and
        remaining (both as ISO durations and seconds).

        Voice answer style: brief summary, one phrase per timer.
        "Pasta timer has 3 minutes left, laundry timer has 25
        minutes." If `count` is 0: "No timers running."
        """
        timers = scheduler.list_active()
        return {
            "count": len(timers),
            "timers": [_serialise(t) for t in timers],
        }

    @tool()
    async def cancel_timer(timer: str) -> dict:
        """Cancel a timer by label or id.

        Use for "cancel the pasta timer", "stop the timer", "cancel
        the 10-minute timer". `timer` is the label ('pasta') or the
        id returned from set_timer; the user's spoken phrase
        usually maps to the label.

        Voice answer style: speak the response's `confirm` field
        verbatim ("Cancelled the pasta timer.").

        If `reason='ambiguous'` (multiple timers match), read the
        candidate durations from `matches` and ask which to cancel
        — "I have two pasta timers, one for 5 minutes and one for
        10 minutes. Which one?" If `reason='not_found'`, speak the
        `error` field verbatim ("No timer matches 'pasta'.").

        Skip the preamble before calling this tool. The `confirm`
        field IS the spoken answer — a status sentence beforehand
        ("Sure, cancelling your pasta timer…") restates it
        word-for-word.
        """
        cancelled, matches = scheduler.cancel(timer)
        if cancelled:
            t = matches[0]
            return {
                "ok": True,
                "cancelled": _serialise(t),
                "confirm": _cancel_confirm(t),
            }
        if not matches:
            return {
                "ok": False,
                "reason": "not_found",
                "error": f"No timer matches {timer!r}.",
            }
        return {
            "ok": False,
            "reason": "ambiguous",
            "matches": [_serialise(t) for t in matches],
            "error": (
                f"{len(matches)} timers match {timer!r} — ask the "
                f"user which one (offer their durations to disambiguate)."
            ),
        }

    return [set_timer, list_timers, cancel_timer]


__all__ = ["make_timer_tools", "announcement_text"]
