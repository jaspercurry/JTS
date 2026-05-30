"""Diagnostic / self-observation tools.

One tool today: ``flag_recent_issue``. Lets the user point at the
previous wake-event interaction and say "that one was wrong" in
their own words, so the corpus at ``/var/lib/jasper/wake-events/``
accumulates a labeled set of real-world failures that can be
reviewed offline.

The persistence layer is ``WakeEventStore.record_flag`` — this file
is just the LLM-facing surface. See that method's docstring for the
"which event gets flagged" semantics, and ``docs/HANDOFF-wake-
telemetry.md`` for the broader telemetry design.

The tool's description teaches the model WHEN to call (positive
framing — phrases that should trigger it) and WHEN NOT to call
(failure modes we caught in early iteration). Per the prompting
playbook in ``docs/HANDOFF-prompting.md``, per-tool conditional
rules live here, not in ``SYSTEM_INSTRUCTION``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import tool

if TYPE_CHECKING:
    from ..wake_events import WakeEventStore

logger = logging.getLogger(__name__)


def make_diagnostic_tools(wake_event_store: "WakeEventStore | None"):
    """Build the diagnostic-tool factory.

    Returns an empty list when ``wake_event_store`` is None (telemetry
    disabled — the daemon couldn't open the SQLite store at startup,
    rare but possible on a misconfigured disk). With no store, the
    model never sees the flag tool, so it can't promise to flag
    something we can't actually record.
    """
    if wake_event_store is None:
        return []

    @tool()
    async def flag_recent_issue(reason: str) -> dict:
        """Mark the previous wake-event interaction as problematic
        for later offline review.

        Call this when the user explicitly indicates the LAST wake
        event misbehaved. Trigger phrases — call this when the user
        says any of these or close paraphrases:
          - "flag that" / "flag the last one" / "mark that as bad"
          - "you cut me off" / "you didn't let me finish" /
            "you ended my turn too early"
          - "that was wrong" / "you got that wrong" /
            "that response was bad"
          - "you fired incorrectly" / "you weren't supposed to wake up" /
            "that was a false wake"
          - "you didn't hear me" / "you misheard me"
          - "you didn't respond" / "you said nothing back"

        Do NOT call this for:
          - Disagreement with the CONTENT of a tool result ("that's
            the wrong weather" / "that train time is wrong") — those
            are data accuracy issues, handle by checking the tool
            again or explaining the answer source.
          - Requests to UNDO a successful action ("undo that volume
            change" / "put my music back on") — use the relevant
            transport / volume tool.
          - Mid-conversation course-correction ("no, the OTHER one") —
            just continue the conversation.
          - General world dissatisfaction ("ugh this weather sucks") —
            that's a remark, not an issue report.
          - The user asking what their PREVIOUS query was — that's a
            memory question, answer if you can, don't flag.

        Pass the user's complaint as ``reason``, close to their own
        words. Be specific: "user said the speaker cut them off" beats
        "issue reported." The reason text is what shows up when the
        user (or a future review pass) looks at the labeled corpus,
        so it should help disambiguate failure modes at a glance.

        Response shape:
          spoken_response: str          — speak this verbatim
          success: bool                 — true if a prior event was
                                          flagged; false if there's
                                          nothing recent to flag or
                                          the telemetry layer errored
          flagged_event_id: str         — the ID of the flagged event,
                                          empty on failure

        Voice answer style:
          Speak ``spoken_response`` exactly. It's intentionally short
          ("Got it, I flagged it"). Do NOT add an apology, an offer
          to do something else, or a follow-up question — the user
          chose to flag and immediately move on; chattiness on this
          path defeats the "low-friction issue reporting" purpose.

        Skip the preamble — flagging is a fire-and-forget log write
        on a local SQLite file; it completes in well under 100 ms
        and the user gains nothing from a status update."""
        try:
            result = await wake_event_store.record_flag(reason)
        except Exception as e:  # noqa: BLE001
            # Fail-soft per the AGENTS.md "no silent failure paths"
            # rule: the user gets an audible answer even when the
            # store write fails. Log for forensics.
            logger.warning(
                "flag_recent_issue: record_flag raised: %s", e,
            )
            return {
                "spoken_response": (
                    "Sorry, the issue log isn't available right now."
                ),
                "success": False,
                "flagged_event_id": "",
            }

        if result is None:
            return {
                "spoken_response": (
                    "There's no recent event to flag yet."
                ),
                "success": False,
                "flagged_event_id": "",
            }

        logger.info(
            "event=flag.recorded flagged=%s flag_action=%s reason=%r",
            result["flagged_event_id"],
            result["flag_action_event_id"],
            reason,
        )
        # Tier C: the user flagged an issue the daemon may not have logged
        # as a WARNING, so dump the recent DEBUG context (voice's flight
        # recorder) to the journal. Best-effort. See jasper/flight_recorder.py.
        try:
            from .. import flight_recorder
            flight_recorder.dump("voice_flagged")
        except Exception:  # noqa: BLE001
            pass
        return {
            "spoken_response": "Got it, I flagged it.",
            "success": True,
            "flagged_event_id": result["flagged_event_id"],
        }

    return [flag_recent_issue]
