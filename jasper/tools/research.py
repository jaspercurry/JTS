"""Async research voice tool."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..conversation_history import read_settings as read_conversation_settings
from . import tool

if TYPE_CHECKING:
    from ..research import ResearchJob, ResearchScheduler
    from ..usage import SpendCap


_ACCEPT_CONFIRM = "On it -- I'll let you know."
_SPEND_CAP_DECLINE = (
    "I've reached today's spend cap, so I can't start paid research right now."
)
_DISMISS_WITH_CAPTURE = "Okay, you can find it in your chat log anytime."
_DISMISS_WITHOUT_CAPTURE = "Okay, I've saved it for you."

logger = logging.getLogger(__name__)

ResearchDeliveryRecorder = Callable[["ResearchJob", str | None, str], None]


def _spend_allowed(spend_cap: "SpendCap | None") -> bool:
    return True if spend_cap is None else bool(spend_cap.allowed())


def make_research_tools(
    scheduler: "ResearchScheduler",
    *,
    spend_cap: "SpendCap | None" = None,
    record_delivery: "ResearchDeliveryRecorder | None" = None,
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

    @tool(labels=("productivity", "research"), log_args=False)
    async def read_research_result(job_id: str, decision: str) -> dict:
        """Resolve the one-shot "want me to read it now?" research prompt.

        Use only when the current turn is answering the speaker's
        proactive research-ready prompt. `job_id` is the exact id from
        the turn instruction. `decision` must be "yes" or "no".

        If the user says yes, call with decision="yes", then speak the
        returned `text` field verbatim and end the turn. If the user
        says no, call with decision="no", speak the returned `text`
        field verbatim, and end the turn. Do not summarize, paraphrase,
        ask a follow-up, or call `research` from this confirmation turn.
        """
        job = scheduler.get((job_id or "").strip())
        if job is None:
            return {
                "ok": False,
                "reason": "not_found",
                "error": "I couldn't find that research result.",
            }
        normalized = (decision or "").strip().lower()
        if normalized not in {"yes", "no"}:
            return {
                "ok": False,
                "reason": "invalid_decision",
                "error": "decision must be 'yes' or 'no'",
            }

        if normalized == "yes":
            scheduler.mark_announced(job.id)
            scheduler.mark_read(job.id)
            text = (job.result or "").strip()
            if not text:
                text = (
                    "Sorry, that research finished without a readable answer. "
                    "Please ask me again."
                )
            _record_delivery(record_delivery, job, text, normalized)
            return {
                "ok": True,
                "decision": normalized,
                "job_id": job.id,
                "text": text,
            }

        scheduler.mark_announced(job.id)
        text = (
            _DISMISS_WITH_CAPTURE
            if _capture_enabled()
            else _DISMISS_WITHOUT_CAPTURE
        )
        _record_delivery(record_delivery, job, text, normalized)
        return {
            "ok": True,
            "decision": normalized,
            "job_id": job.id,
            "text": text,
        }

    return [research, read_research_result]


__all__ = ["make_research_tools"]


def _capture_enabled() -> bool:
    try:
        return bool(read_conversation_settings().capture_enabled)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(
            "research tool: conversation settings unavailable (%s: %s)",
            type(e).__name__,
            e,
        )
        return False


def _record_delivery(
    recorder: "ResearchDeliveryRecorder | None",
    job: "ResearchJob",
    assistant_text: str | None,
    decision: str,
) -> None:
    if recorder is None:
        return
    try:
        recorder(job, assistant_text, decision)
    except (OSError, RuntimeError, TypeError, ValueError) as e:
        logger.warning(
            "research tool: delivery capture failed (id=%s decision=%s): %s",
            job.id,
            decision,
            e,
        )
