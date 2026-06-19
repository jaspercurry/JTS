"""Async research voice tool."""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import tool

if TYPE_CHECKING:
    from ..research import ResearchScheduler
    from ..usage import SpendCap


_ACCEPT_CONFIRM = "On it -- I'll let you know."
_SPEND_CAP_DECLINE = (
    "I've reached today's spend cap, so I can't start paid research right now."
)


def _spend_allowed(spend_cap: "SpendCap | None") -> bool:
    return True if spend_cap is None else bool(spend_cap.allowed())


def make_research_tools(
    scheduler: "ResearchScheduler",
    *,
    spend_cap: "SpendCap | None" = None,
):
    @tool(labels=("productivity", "research"), log_args=False)
    async def research(query: str) -> dict:
        """Start a background research job and return immediately.

        Use when the user asks to "research X and let me know", "look
        into X and tell me later", or otherwise wants a short researched
        answer after this voice turn. `query` is the concrete research
        question to hand to the background text model.

        Voice answer style: if `ok` is true, speak the response's
        `confirm` field verbatim ("On it -- I'll let you know.") and end
        the turn. Do NOT promise to hold the line, wait, keep listening,
        or answer the research question now. The speaker automatically
        announces the result later.

        If `ok` is false, speak `confirm` verbatim as a brief decline.
        Do not retry in a loop; ask the user to try again later only when
        the decline says so.

        Skip the preamble before calling this tool. The `confirm` field
        IS the spoken answer.
        """
        if not _spend_allowed(spend_cap):
            return {
                "ok": False,
                "reason": "spend_cap_reached",
                "confirm": _SPEND_CAP_DECLINE,
                "error": _SPEND_CAP_DECLINE,
            }

        started = scheduler.submit(query)
        if not started.accepted:
            message = started.message
            return {
                "ok": False,
                "reason": "declined",
                "confirm": message,
                "error": message,
            }
        return {
            "ok": True,
            "confirm": _ACCEPT_CONFIRM,
            "job_id": started.job.id if started.job is not None else None,
        }

    return [research]


__all__ = ["make_research_tools"]
