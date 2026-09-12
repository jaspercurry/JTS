# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The web adapter over the v2 status projection.

The derivations live in
:mod:`jasper.active_speaker.crossover_envelope_v2`'s status-projection
section, which may not import this layer. This module supplies the answers
only the web host holds — the loaded state, the volume plan, the review
decision, the banked candidate — and shapes what comes back into
``status["crossover_v2"]``.

The host (:mod:`jasper.web.correction_crossover_v2`) is reached through the
MODULE object, never by name — ``_host.load_v2_state()``, not a from-import.
That keeps this adapter on the same late-bound patch surface it had while it
lived in the host: a test that patches ``load_v2_state`` on the host still
reaches this reader. A from-import here would bind a second name that no such
patch can reach, and the tests would go on passing while patching nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from jasper.active_speaker import crossover_envelope_v2 as _projection
from jasper.active_speaker.grade_coverage import asked_beyond_mark
from jasper.log_event import log_event
from jasper.web import correction_crossover_v2 as _host


def previous_candidate_fingerprint(state: Mapping[str, Any] | None) -> str | None:
    """The measured candidate the applied graph displaced, if one is recorded."""
    value = (state or {}).get("previous_candidate_fingerprint")
    return value if isinstance(value, str) and value else None


def _offerable_previous_candidate(state: Mapping[str, Any] | None) -> str | None:
    """The displaced candidate, when its banked artifact still resolves."""
    from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate

    from jasper.active_speaker.crossover_v2.apply_gate import previously_applied

    fingerprint = previous_candidate_fingerprint(state)
    if fingerprint and previously_applied(fingerprint, (state or {}).get("previous_applied_profile")):
        try:
            return find_banked_candidate(fingerprint).fingerprint
        except CandidateBankRefusal:
            pass
    return None


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    state = _host.load_v2_state()
    session_id = (state or {}).get("session_id")
    # Count is derived from its persistence owner on every state read. Keeping
    # a second copy in journey state made crash recovery and offline store
    # repair observable as two contradictory counts.
    store_count = _host._attempt_loop_store_snapshot().model_error_count
    try:
        needs_recovery = bool(_host.session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    block: dict[str, Any] = {
        "phase": _projection.crossover_v2_phase(
            state, review_declined=_host.review_declined(state),
        ),
        # save_v2_state stamps transitions; polls must not create a second clock (#1947).
        "updated_at": (state or {}).get("updated_at"),
        "candidate": (state or {}).get("candidate"),
        "accepted_sound_revision": (state or {}).get("accepted_sound_revision"),
        # MEASURE's own verdict-time disclosures — today just G1's ripple
        # reservation (#2087). Copied through unvalidated, exactly like
        # ``candidate`` and ``verify`` beside it: the envelope's own accessor
        # is the validating reader, so a state file written by another build
        # cannot 500 this poll path.
        "measure": (state or {}).get("measure"),
        # The coordinator owns the ordinal and adoption receipt (#2537, #2602).
        "round_receipt": (state or {}).get("round_receipt"),
        "tuning_trial": (state or {}).get("tuning_trial"),
        "verify": (state or {}).get("verify"),
        "execution": (state or {}).get("execution"),
        "failure": (state or {}).get("failure"),
        "apply_blocked": (state or {}).get("apply_blocked"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        "previous_candidate_fingerprint": _offerable_previous_candidate(state),
        "session_id": session_id,
        "attempts_loop": {
            "store_count": store_count,
        },
        "cloud": _projection.compact_cloud_status(
            (state or {}).get("cloud"),
            current_session_id=session_id,
        ),
        "cloud_chart": _projection.chart_cloud_status((state or {}).get("cloud")),
        "prediction": _projection.prediction_status(state),
        "findings": _projection.household_findings_status(state),
        # The across-rounds view no single receipt can carry: per spec band,
        # how much of what was commanded arrived, over how many banked rounds,
        # and how much those rounds disagreed. Read from the banked receipts
        # rather than this state file because it is HISTORY — the durable
        # state holds only the last round, and the whole claim here is about
        # the several before it.
        #
        # Disclosure. Nothing reads it back: no adoption row, refusal or
        # prescription consumes it, and this module writes nothing.
        "controllability": _controllability_status(),
    }
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state

    applied = load_applied_baseline_profile_state() or {}
    block["trial_verification"] = applied.get("trial_verification")
    block["post_apply_grade"] = _host._post_apply_grade(block, spatial_required=bool(block["applied"]) and asked_beyond_mark(state or {}))
    return block


def _controllability_status() -> dict[str, Any] | None:
    """The raw per-round controllability rows, or ``None`` when unreadable.

    ``None`` rather than an empty document, and the distinction is the usual
    one: a box with banked rounds that measured nothing still publishes those
    rounds with empty band rows, so ``None`` here means the LEDGER was
    unavailable, never that the speaker is uncontrollable.

    Passed through as banked (ADR-0198). Pooling these rows into a mean, a
    spread or a label is the reader's, so this publishes the numbers and
    computes none of them.

    Guarded because this is the one entry on this block that touches the
    bundle store. The caller's own handler turns any raise into a dropped
    ``crossover_v2`` key for the whole poll, and a history view is never worth
    that: an unreadable bundle root costs this key alone.

    **The import is INSIDE the guard**, and ``ImportError`` is caught with the
    rest. A half-rsynced ``/opt/jasper`` mid-deploy can fail this import, and
    neither this tuple nor the caller's identical one lists ``ImportError`` —
    so an import left outside would 500 the whole status route to publish a
    disclosure.
    """

    try:
        from jasper.active_speaker.controllability_ledger import (
            read_controllability_ledger,
        )

        return read_controllability_ledger()
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        log_event(
            _host.logger,
            "correction.controllability_ledger_unavailable",
            level=logging.WARNING,
            exc_info=True,
        )
        return None
