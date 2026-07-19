# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 conductor screen envelope (schema 7, Wave 5a).

``docs/crossover-measurement-productization-design.md`` §5.9/§5.10 defines the
v2 screen sequence — ``("speaker_setup", "microphone_check", "measure",
"review_apply", "verify")`` — and the four failure-screen TEMPLATES the flow
renders (silent auto-retry banner / fix-and-retry / hard stop / session
restart), plus the two special screens (``volume_recovery`` and the VERIFY-fail
one-default screen). This module is the pure ``status → envelope`` function for
that flow, dispatched from
:func:`jasper.active_speaker.crossover_envelope.build_crossover_envelope` only
when ``JASPER_CROSSOVER_FLOW=v2`` (the legacy flow returns schema 6
byte-identically). It emits the SAME envelope dict shape the legacy renderer
returns (``schema_version`` / ``screen`` / ``steps`` / ``verdict_text`` /
``nudges`` / ``relay`` / ``next_action`` / ``alternate_actions`` / ``progress``
/ ``applied``) so the generic data-driven JS renderer needs no v2-specific code.

The v2-specific state the backend threads onto the status lives under
``status["crossover_v2"]`` (phase / failure / verify / candidate /
apply_blocked / needs_recovery / applied); this module never re-derives it — the conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`) owns those decisions and their
reason codes, and this module maps a reason code to its template copy through
the shared :data:`~jasper.active_speaker.crossover_v2_flow.REASON_REGISTRY`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event
from .crossover_v2_flow import (
    PHASE_CHECK,
    PHASE_DONE,
    PHASE_MEASURE,
    PHASE_REVIEW_APPLY,
    PHASE_VERIFY,
    REASON_REGISTRY,
    TEMPLATE_HARD_STOP,
    TEMPLATE_SESSION_RESTART,
    TEMPLATE_SILENT_AUTO_RETRY,
    TEMPLATE_VERIFY_FAIL,
)

logger = logging.getLogger(__name__)

CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION = 7

# Below this alignment-estimator confidence (see AlignmentEstimate.confidence
# in program_analysis.py), the review_apply screen carries a warn nudge
# suggesting a re-measure at a cleaner mic position (W6.7 ruling 4 — the
# run-7 hardware pass applied a candidate at confidence 0.485). This is
# informed consent, NOT a gate: Apply stays available regardless. PROVISIONAL
# pending W6 bench distributions on confidence-vs-outcome correlation.
ALIGNMENT_CONFIDENCE_NUDGE_FLOOR = 0.6

# The v2 step tuple (§5.9). The step machinery inside each step is gone; these
# five are the whole journey.
_STEP_IDS = (
    "speaker_setup",
    "microphone_check",
    "measure",
    "review_apply",
    "verify",
)
_STEP_LABELS = {
    "speaker_setup": "Protected speaker setup",
    "microphone_check": "Microphone check",
    "measure": "Measure",
    "review_apply": "Review and apply",
    "verify": "Verify",
}

# Which step is active for a given conductor phase.
_PHASE_STEP = {
    PHASE_CHECK: "microphone_check",
    PHASE_MEASURE: "measure",
    PHASE_REVIEW_APPLY: "review_apply",
    PHASE_VERIFY: "verify",
    PHASE_DONE: "verify",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _v2(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(status.get("crossover_v2"))


def _step_payload(active_step: str, done_steps: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for step_id in _STEP_IDS:
        rows.append({
            "id": step_id,
            "label": _STEP_LABELS[step_id],
            "status": (
                "done" if step_id in done_steps
                else "active" if step_id == active_step
                else "pending"
            ),
        })
    return rows


def _progress(active_step: str) -> dict[str, int]:
    try:
        position = _STEP_IDS.index(active_step) + 1
    except ValueError:
        position = len(_STEP_IDS)
    return {"position": position, "total": len(_STEP_IDS)}


def _done_before(active_step: str) -> set[str]:
    """Every step strictly before the active one is done (monotonic journey)."""
    try:
        frontier = _STEP_IDS.index(active_step)
    except ValueError:
        frontier = len(_STEP_IDS)
    return set(_STEP_IDS[:frontier])


def _applied_chip(status: Mapping[str, Any]) -> dict[str, str]:
    """Durable applied-crossover chip — reuse the legacy contract shape."""
    contract = _mapping(_mapping(status.get("setup")).get("applied_crossover"))
    if contract.get("valid") is not True:
        return {"state": "none", "label": "No speaker profile applied"}
    owner = str(contract.get("owner") or "")
    if owner == "automatic":
        return {"state": "automatic", "label": "Automatic crossover applied"}
    if owner == "manual":
        return {"state": "manual", "label": "Manual crossover applied"}
    return {"state": "applied", "label": "Speaker profile applied"}


def _setup_ready(status: Mapping[str, Any]) -> bool:
    setup = _mapping(status.get("setup"))
    return setup.get("active") is True and setup.get("status") == "ready"


def _envelope(
    *,
    screen: str,
    active_step: str,
    verdict: str,
    nudges: list[dict[str, str]] | None = None,
    next_action: dict[str, Any] | None = None,
    alternate_actions: list[dict[str, Any]] | None = None,
    status: Mapping[str, Any],
    candidate_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
        "flow": "v2",
        "screen": screen,
        "active": True,
        "steps": _step_payload(active_step, _done_before(active_step)),
        "verdict_text": verdict,
        "nudges": nudges or [],
        "relay": _mapping(status.get("relay")) or None,
        "next_action": next_action,
        "alternate_actions": alternate_actions or [],
        "progress": _progress(active_step),
        "applied": _applied_chip(status),
        "candidate_review": dict(candidate_review) if candidate_review else None,
    }


def _verify_fail_envelope(
    code: str, message: str, status: Mapping[str, Any],
) -> dict[str, Any]:
    """The VERIFY-fail screen (§5.2): one default "Try again" + "Undo".

    Shared by ``REASON_VERIFY_OUT_OF_TOLERANCE`` / ``REASON_VERIFY_INCONCLUSIVE``
    (whose own REASON_REGISTRY template is already ``verify_fail``) AND the
    VERIFY-phase override in :func:`_failure_envelope` (W6.7 ruling 3) for any
    OTHER code surfacing once the candidate is applied — the household is
    entitled to the Undo affordance the moment something is live on the
    speaker, regardless of which check failed.
    """
    return _envelope(
        screen="verify_fail", active_step="verify",
        verdict=message,
        nudges=[{"code": code, "severity": "warn", "text": message}],
        next_action={
            "id": "verify_retry",
            "label": "Try again",
            "endpoint": "/correction/crossover/v2/verify",
            "body": {},
        },
        alternate_actions=[
            {
                "id": "verify_undo",
                "label": "Undo (restore previous sound)",
                # W5b CHECKLIST ITEM: this rides the legacy restore path
                # and does NOT yet clear the durable v2 applied/candidate
                # state — after an undo the v2 state still says applied.
                # W5b owns v2-aware restore semantics (clear the stale
                # applied flag + candidate on successful restore).
                "endpoint": "/correction/crossover/restore",
                "body": {},
            },
            {
                "id": "verify_remeasure",
                "label": "Re-measure",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
                "expert": True,
            },
        ],
        status=status,
    )


def _failure_envelope(
    code: str, status: Mapping[str, Any], active_step: str,
) -> dict[str, Any]:
    """Render one of the four §5.10 templates from a reason code.

    VERIFY-phase override (W6.7 ruling 3): once ``active_step`` is
    ``"verify"`` the candidate is already applied — ``_phase_from_state``
    (jasper.web.correction_crossover_v2) only reports the VERIFY phase once
    ``applied`` is True — so ANY failure code surfacing there renders through
    the ``verify_fail`` template regardless of REASON_REGISTRY's own owning
    template. fix_and_retry / hard_stop / session_restart / silent_auto_retry
    all hide the Undo affordance the household is entitled to the moment
    something is live on the speaker (the run-7 hardware bug: an
    ``agc_behavioral_fail`` during VERIFY rendered ``fix_and_retry`` and
    displaced the VERIFY-fail screen's Undo action). REASON_REGISTRY stays
    the single copy source — only the template choice is overridden here.
    """
    spec = REASON_REGISTRY.get(code)
    if spec is None:  # defensive — an unknown code still names a retry, never a bare code
        if active_step == "verify":
            return _verify_fail_envelope(
                code, "Something went wrong with that measurement. Try again.", status,
            )
        return _envelope(
            screen="fix_and_retry", active_step=active_step,
            verdict="Something went wrong with that measurement. Try again.",
            next_action={"id": "retry", "label": "Try again"},
            status=status,
        )
    if active_step == "verify" and spec.template != TEMPLATE_VERIFY_FAIL:
        return _verify_fail_envelope(code, spec.message or spec.banner, status)
    template = spec.template
    if template == TEMPLATE_SILENT_AUTO_RETRY:
        # No decision screen: stay on the phase screen with a banner; the phone
        # auto-retries (§5.10 template 1).
        return _envelope(
            screen=active_step, active_step=active_step,
            verdict=spec.banner,
            nudges=[{"code": code, "severity": "info", "text": spec.banner}],
            next_action=None,
            status=status,
        )
    if template == TEMPLATE_HARD_STOP:
        return _envelope(
            screen="hard_stop", active_step=active_step,
            verdict=spec.message,
            nudges=[{"code": code, "severity": "warn", "text": spec.message}],
            next_action={"id": "speaker_setup", "label": "Back to speaker setup", "href": "/sound/"},
            status=status,
        )
    if template == TEMPLATE_SESSION_RESTART:
        return _envelope(
            screen="session_restart", active_step="microphone_check",
            verdict=spec.message,
            nudges=[{"code": code, "severity": "warn", "text": spec.message}],
            next_action={
                "id": "restart_session",
                "label": "Start over",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
            },
            status=status,
        )
    if template == TEMPLATE_VERIFY_FAIL:
        # One default — "Try again" (internally re-verify once, then re-measure)
        # — plus "Undo (restore previous sound)"; the explicit trio lives behind
        # the expert disclosure (§5.2).
        return _verify_fail_envelope(code, spec.message, status)
    # TEMPLATE_FIX_AND_RETRY (the default decision screen).
    return _envelope(
        screen="fix_and_retry", active_step=active_step,
        verdict=spec.message,
        nudges=[{"code": code, "severity": "warn", "text": spec.message}],
        next_action={
            "id": "retry",
            "label": "Try again",
            "endpoint": "/correction/crossover/v2/session",
            "body": {},
        },
        status=status,
    )


def build_crossover_envelope_v2(status: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 conductor envelope (schema 7) for the served status."""
    if not bool(status.get("active")):
        return {
            "schema_version": CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
            "flow": "v2",
            "screen": "not_applicable",
            "active": False,
            "steps": [],
            "verdict_text": (
                "This speaker has no active crossover. Continue with room correction."
            ),
            "nudges": [],
            "relay": _mapping(status.get("relay")) or None,
            "next_action": {
                "id": "room", "label": "Correct the room", "href": "/correction/room/",
            },
            "alternate_actions": [],
            "progress": {"position": 0, "total": len(_STEP_IDS)},
            "applied": _applied_chip(status),
            "candidate_review": None,
        }

    v2 = _v2(status)
    phase = str(v2.get("phase") or PHASE_CHECK)
    active_step = _PHASE_STEP.get(phase, "microphone_check")

    # Volume recovery keys on needs_recovery, NOT unresolved_volume_safety alone
    # (the W2 gate ruling — a crash-hydrated active plan surfaces no unresolved
    # payload but still needs draining).
    if bool(v2.get("needs_recovery")):
        spec = REASON_REGISTRY["volume_unresolved"]
        return _envelope(
            screen="volume_recovery", active_step="microphone_check",
            verdict=spec.message,
            nudges=[{
                "code": "crossover_v2_volume_unresolved",
                "severity": "warn",
                "text": spec.message,
            }],
            next_action={
                "id": "recover_volume",
                "label": "Recover safe listening volume",
                "endpoint": "/correction/crossover/recover-volume",
                "body": {},
            },
            status=status,
        )

    # Speaker setup must be proven before any measurement plays.
    if not _setup_ready(status):
        return _envelope(
            screen="speaker_setup", active_step="speaker_setup",
            verdict=(
                "Finish the protected speaker setup first. This proves the output "
                "map and tweeter protection before the microphone check can play."
            ),
            next_action={"id": "speaker_setup", "label": "Finish speaker setup", "href": "/sound/"},
            status=status,
        )

    failure = _mapping(v2.get("failure"))
    failure_code = str(failure.get("code") or "")
    if failure_code:
        env = _failure_envelope(failure_code, status, active_step)
        log_event(
            logger, "correction.crossover_v2_envelope_serve",
            screen=env["screen"], phase=phase, failure=failure_code,
        )
        return env

    if phase == PHASE_CHECK:
        env = _envelope(
            screen="microphone_check", active_step="microphone_check",
            verdict=(
                "Place the microphone about 1 m in front of the speaker at tweeter "
                "height (see the picture), then tap Start on your phone. JTS runs a "
                "quick microphone check first."
            ),
            next_action={
                "id": "start_v2_session",
                "label": "Start measurement",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
            },
            status=status,
        )
    elif phase == PHASE_MEASURE:
        env = _envelope(
            screen="measure", active_step="measure",
            verdict=(
                "Keep the phone still — JTS is measuring both drivers. Follow the "
                "phone; the measurement continues automatically."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_REVIEW_APPLY:
        candidate = _mapping(v2.get("candidate"))
        # Finding N: a blocked apply must not be a silent dead end — the last
        # blocked-apply issue (persisted by the apply endpoint) surfaces here
        # as a nudge so a repeated "nothing happened" Apply tap has an
        # explanation, without inventing a new screen/template.
        apply_blocked = _mapping(v2.get("apply_blocked"))
        nudges: list[dict[str, str]] = []
        if apply_blocked:
            nudges.append({
                "code": str(apply_blocked.get("id") or "apply_blocked"),
                "severity": "warn",
                "text": str(
                    apply_blocked.get("message")
                    or "Applying the reviewed crossover was blocked. Try again."
                ),
            })
        # Low-confidence nudge (W6.7 ruling 4) — informed consent, not a gate:
        # Apply below is untouched either way.
        confidence = candidate.get("alignment_confidence")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence < ALIGNMENT_CONFIDENCE_NUDGE_FLOOR
        ):
            nudges.append({
                "code": "crossover_v2_alignment_low_confidence",
                "severity": "warn",
                "text": (
                    "Alignment is less certain at this mic position — for best "
                    "results, place the mic about 1 m in front of the speaker at "
                    "tweeter height and re-measure."
                ),
            })
        env = _envelope(
            screen="review_apply", active_step="review_apply",
            verdict=(
                "Review the measured crossover below. Frequency and slope stay as "
                "you set them; the measured level, delay, and polarity are applied."
            ),
            nudges=nudges,
            next_action={
                "id": "apply_measured_candidate",
                "label": "Apply reviewed crossover",
                # The v2 apply endpoint: reopens the published candidate
                # artifact (tamper-checked) and rides the existing atomic
                # apply-with-rollback transaction via the W4
                # measured_candidate seam; on success it arms VERIFY.
                "endpoint": "/correction/crossover/v2/apply",
                "body": {
                    "expected_candidate_fingerprint": str(candidate.get("fingerprint") or ""),
                },
            },
            status=status,
            candidate_review=candidate or None,
        )
    elif phase == PHASE_VERIFY:
        env = _envelope(
            screen="verify", active_step="verify",
            verdict=(
                "The crossover is applied. Keep the microphone where it was and "
                "tap Verify on your phone to confirm the result."
            ),
            next_action=None,
            status=status,
        )
    elif phase == PHASE_DONE:
        verify = _mapping(v2.get("verify"))
        env = _envelope(
            screen="done", active_step="verify",
            verdict=(
                "The measured crossover is applied and verified. Room correction "
                "is now available."
            ),
            next_action={
                "id": "room", "label": "Continue to Room correction", "href": "/correction/room/",
            },
            nudges=(
                [{"code": "crossover_v2_verified", "severity": "ok", "text": "Verified."}]
                if verify.get("outcome") == "pass" else []
            ),
            status=status,
        )
        # Terminal: mark every step done.
        env["steps"] = _step_payload("", set(_STEP_IDS))
        env["progress"] = {"position": len(_STEP_IDS), "total": len(_STEP_IDS)}
    else:
        env = _envelope(
            screen="microphone_check", active_step="microphone_check",
            verdict="Start the measurement on your phone.",
            next_action={
                "id": "start_v2_session",
                "label": "Start measurement",
                "endpoint": "/correction/crossover/v2/session",
                "body": {},
            },
            status=status,
        )

    log_event(
        logger, "correction.crossover_v2_envelope_serve",
        screen=env["screen"], phase=phase, failure="",
    )
    return env
