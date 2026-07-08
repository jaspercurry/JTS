# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Server-computed *screen envelope* for the active-crossover commissioning flow.

Layer A (the speaker layer) is the PRIMARY, foundational layer for an active
speaker and is hidden entirely for a passive speaker (revision plan §1). This
module aligns the crossover flow's step presentation with the room flow's
envelope-driven pattern (:mod:`jasper.correction.envelope`, §3.2): one JSON
object per step describing the logical screen, a plain-language verdict, the
single primary action (nudges never disable it), homeowner nudges, and step
progress — a **dumb frontend** renders it verbatim.

It is a **parallel, minimal** envelope, deliberately NOT an extension of the
room envelope: the two flows are disjoint state machines (the room envelope keys
on :class:`jasper.correction.session.SessionState`; commissioning keys on the
durable active-speaker state files). Coupling them would force a schema/shape
pin across two unrelated machines. Instead this composes the step model the
commissioning coordinator ALREADY builds
(:func:`jasper.active_speaker.commissioning_coordinator.build_commissioning_view`)
into the shared envelope shape — the coordinator's state machine is untouched.

Purely additive + read-only: it derives from the coordinator view + measurement
targets, mutates nothing, and does not touch the existing ``/crossover/status``
payload.

``active`` is the load-bearing gate: ``False`` for a ``full_range_passive``
speaker (no active driver/summed targets), which is how "passive users never see
Layer A" is expressed to the frontend. When ``active`` is ``False`` the envelope
carries no steps and a single explanatory verdict.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event
from .commissioning_coordinator import COMMISSIONING_STEP_IDS

logger = logging.getLogger(__name__)

# Bumped independently of the coordinator's artifact_schema_version; a pinning
# test guards it (mirrors correction.envelope.ENVELOPE_SCHEMA_VERSION).
CROSSOVER_ENVELOPE_SCHEMA_VERSION = 1

# The logical commissioning screens: the coordinator's five step ids (imported,
# never re-typed — a coordinator rename must not silently degrade `_screen_for`
# to its fallback) plus a terminal "done" and a passive "not_applicable". Kept
# coarse: the dumb frontend renders the spine; the coordinator's per-step
# status drives which is active.
SCREEN_NOT_APPLICABLE = "not_applicable"
SCREEN_LAYOUT, SCREEN_RESEARCH, SCREEN_MAP, SCREEN_SAFETY, SCREEN_PROFILE = (
    COMMISSIONING_STEP_IDS
)
SCREEN_DONE = "done"

# Ordered spine for the progress indicator — the coordinator's own tuple.
_PROGRESS_SPINE: tuple[str, ...] = COMMISSIONING_STEP_IDS


def _active_targets(status: Mapping[str, Any]) -> bool:
    """Whether this speaker has any active (Layer A) driver/summed targets.

    Passive (``full_range_passive``) speakers have no active groups, so
    ``targets.drivers`` / ``targets.summed`` are empty — the single honest gate
    for "does Layer A apply here?" (revision plan §1). Reads the already-computed
    ``/crossover/status`` targets so it never re-derives topology.
    """
    targets = status.get("targets") if isinstance(status, Mapping) else None
    if not isinstance(targets, Mapping):
        return False
    drivers = targets.get("drivers")
    summed = targets.get("summed")
    return bool(
        (isinstance(drivers, list) and drivers)
        or (isinstance(summed, list) and summed)
    )


def _screen_for(view: Mapping[str, Any]) -> str:
    """The active commissioning screen from the coordinator's current_step.

    ``applied`` (the terminal coordinator status) folds onto ``done``. An
    unknown/absent current_step falls back to the first spine screen so the
    frontend is never left with no screen.
    """
    if str(view.get("status") or "") == "applied":
        return SCREEN_DONE
    current = str(view.get("current_step") or "")
    if current in _PROGRESS_SPINE:
        return current
    # No active step (all done but not yet "applied"): show the last spine step.
    steps = view.get("steps")
    if isinstance(steps, list) and steps:
        last = steps[-1]
        if isinstance(last, Mapping) and last.get("status") == "done":
            return SCREEN_PROFILE
    return SCREEN_LAYOUT


def _progress(screen: str) -> dict[str, int]:
    if screen == SCREEN_DONE:
        return {"position": len(_PROGRESS_SPINE), "total": len(_PROGRESS_SPINE)}
    try:
        position = _PROGRESS_SPINE.index(screen) + 1
    except ValueError:
        position = 1
    return {"position": position, "total": len(_PROGRESS_SPINE)}


def _steps(view: Mapping[str, Any]) -> list[dict[str, str]]:
    """The coordinator's steps, relayed as the envelope's step spine.

    Each carries id/label/status/message straight from the coordinator (the
    single owner of the step model); this never re-derives them.
    """
    raw = view.get("steps")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for step in raw:
        if not isinstance(step, Mapping):
            continue
        out.append(
            {
                "id": str(step.get("id") or ""),
                "label": str(step.get("label") or ""),
                "status": str(step.get("status") or ""),
                "message": str(step.get("message") or ""),
            }
        )
    return out


def _next_action(view: Mapping[str, Any]) -> dict[str, Any] | None:
    """The single primary action the dumb frontend offers, from the coordinator.

    The coordinator already resolves exactly one ``next_action`` (id/label/
    enabled/endpoint/method/body/message). We relay it unchanged — an empty dict
    (the coordinator's "nothing to do") becomes ``None`` so the frontend shows no
    button. Measurement-quality never blocks here: the action's ``enabled`` flag
    reflects genuine prerequisites (a driver test needs confirmed outputs), not a
    quality nudge.
    """
    action = view.get("next_action")
    if not isinstance(action, Mapping) or not action.get("id"):
        return None
    return dict(action)


def _verdict_text(view: Mapping[str, Any], screen: str, *, active: bool) -> str:
    """One homeowner sentence describing where commissioning stands.

    Terse + screen-scoped (mirrors correction.envelope._verdict_text). For a
    passive speaker this states, in plain language, why Layer A does not apply.
    """
    if not active:
        return (
            "This speaker is a single full-range output, so there is no "
            "crossover to tune here — measure your room instead."
        )
    if screen == SCREEN_DONE:
        return "Your active crossover is commissioned and saved."
    # Prefer the coordinator's message for the active step (it is already
    # homeowner copy), else a spine-default line.
    for step in view.get("steps") or []:
        if isinstance(step, Mapping) and step.get("status") == "active":
            message = str(step.get("message") or "").strip()
            if message:
                return message
    defaults = {
        SCREEN_LAYOUT: "Tell JTS what drivers are wired to each output.",
        SCREEN_RESEARCH: "Save the driver names and crossover points.",
        SCREEN_MAP: "Confirm each DAC output and driver quietly.",
        SCREEN_SAFETY: "Test the combined crossover quietly.",
        SCREEN_PROFILE: "Save the checked speaker profile.",
    }
    return defaults.get(screen, "Set up your active crossover, one step at a time.")


# Coordinator failure/quality signals surfaced as homeowner nudges. Commissioning
# SAFETY gates (an unprotected tweeter, a bad graph) stay HARD in
# graph_safety.py / the L0 emit gate — they are never softened to a nudge. What
# lands here is measurement-quality / retry guidance: a sentence + a checkmark,
# never a block (§0.2). A failed combined test is a retry nudge, not a gate.
def _nudges(view: Mapping[str, Any], *, active: bool) -> list[dict[str, str]]:
    if not active:
        return []
    nudges: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(code: str, severity: str, text: str) -> None:
        if code in seen or not text:
            return
        seen.add(code)
        nudges.append({"code": code, "severity": severity, "text": text})

    # Combined-test retry guidance (the coordinator computes per-group
    # failure_message — retry copy, not a safety block).
    for group in view.get("combined_groups") or []:
        if not isinstance(group, Mapping):
            continue
        failure = str(group.get("failure_message") or "").strip()
        if failure:
            _add(f"combined_test_retry:{group.get('group_id')}", "warn", failure)
    # Revalidation-needed guidance (a saved profile drifted from the drivers).
    revalidation = view.get("revalidation")
    if isinstance(revalidation, Mapping) and revalidation.get("required") is True:
        reason = str(revalidation.get("message") or "").strip() or (
            "Something changed since the last profile — re-run the combined "
            "check and save a fresh profile."
        )
        _add("revalidation_required", "info", reason)
    return nudges


def build_crossover_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    """Build the commissioning screen envelope from a ``/crossover/status`` payload.

    ``status`` is what ``correction_crossover_backend.status_payload`` returns;
    only its ``targets`` drive the passive gate here. The view itself comes from
    the SHARED loader ``commissioning_coordinator.load_commissioning_view`` —
    the same "load every durable state input, then compose" the ``/sound/``
    card uses — because ``build_commissioning_view`` is a pure composer: called
    with only part of its inputs it silently reports a stuck flow (a missing
    ``design_draft`` pins "research" forever; a missing ``baseline_profile``
    makes "done" unreachable). Read-only. When no active targets exist the
    envelope is the passive gate: ``active=False``, no steps, one explanatory
    verdict.
    """
    from jasper.active_speaker.commissioning_coordinator import (
        load_commissioning_view,
    )

    active = _active_targets(status)
    if not active:
        screen = SCREEN_NOT_APPLICABLE
        return {
            "schema_version": CROSSOVER_ENVELOPE_SCHEMA_VERSION,
            "screen": screen,
            "active": False,
            "steps": [],
            "verdict_text": _verdict_text({}, screen, active=False),
            "nudges": [],
            "next_action": None,
            "progress": {"position": 0, "total": len(_PROGRESS_SPINE)},
        }

    # commission=None: it is a runtime-only relay (never consulted for
    # steps/status) and its full payload needs the /sound/ caller's async
    # CamillaDSP probe; the envelope does not surface `runtime`.
    view = load_commissioning_view()
    screen = _screen_for(view)
    return {
        "schema_version": CROSSOVER_ENVELOPE_SCHEMA_VERSION,
        "screen": screen,
        "active": True,
        "steps": _steps(view),
        "verdict_text": _verdict_text(view, screen, active=True),
        "nudges": _nudges(view, active=True),
        "next_action": _next_action(view),
        "progress": _progress(screen),
    }


def build_crossover_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    """`build_crossover_envelope` plus one structured `event=` line.

    Separate from the pure builder so tests pin the shape without log noise; the
    endpoint calls this variant (mirrors correction.envelope.build_envelope_logged).
    """
    envelope = build_crossover_envelope(status)
    log_event(
        logger,
        "correction.crossover_envelope_serve",
        screen=envelope["screen"],
        active=envelope["active"],
        step_count=len(envelope["steps"]),
        nudge_count=len(envelope["nudges"]),
    )
    return envelope
