# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The web adapter over the v2 status projection.

The derivations live in
:mod:`jasper.active_speaker.crossover_envelope_v2`'s status-projection
section, which may not import this layer. This module supplies the answers
only the web host holds — the loaded state, the volume plan, the review
decision, the applied record — and shapes what comes back into
``status["crossover_v2"]``.

"""

from __future__ import annotations

from jasper.web import correction_crossover_v2_grade as v2grade
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web import correction_crossover_v2_volume as v2volume

import logging
from typing import Any, Mapping

from jasper.active_speaker import crossover_envelope_v2 as _projection
from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.seat_level_reference import seat_level_reference_status
from jasper.log_event import log_event


logger = logging.getLogger(__name__)

def rollback_candidate(state: Mapping[str, Any] | None, *,
                       identity: Mapping[str, Any] | None | bool = False) -> str | None:
    """The offerable candidate displaced by this round's apply or by the durable applied record.

    ``identity`` is the already-read ``applied_identity`` (``None`` when nothing is
    applied); ``False`` reads it here.
    """
    state = state or {}
    fingerprint = state.get("previous_candidate_fingerprint")
    candidate = state.get("candidate")
    session = str(candidate.get("fingerprint") or "") if isinstance(candidate, Mapping) else ""
    if identity is False:
        identity = applied_identity(load_applied_baseline_profile_state())
    durable = (identity or {}).get("candidate") if isinstance(identity, Mapping) else None
    displaced_by = state.get("previous_candidate_displaced_by")
    applied = state.get("previous_applied_profile") or {}
    if (isinstance(displaced_by, str) and displaced_by and displaced_by in {p for p in (session, durable) if p}
            and isinstance(fingerprint, str) and fingerprint and applied.get("status") == "applied"
            and (applied.get("source") or {}).get("measured_candidate_fingerprint") == fingerprint
            and (applied.get("config") or {}).get("sha256")):
        return fingerprint
    return None


def crossover_v2_status_block(
    *, controllability: dict[str, Any] | None | bool = False,
) -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).

    ``controllability`` is a ledger an earlier answer carried (``None`` when
    it was unreadable); ``False`` reads it here.
    """
    state = v2state.load_v2_state()
    session_id = (state or {}).get("session_id")
    # Count is derived from its persistence owner on every state read. Keeping
    # a second copy in journey state made crash recovery and offline store
    # repair observable as two contradictory counts.
    store_count = v2state._attempt_loop_store_snapshot().model_error_count
    try:
        needs_recovery = bool(v2volume.session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    applied_profile = load_applied_baseline_profile_state()
    identity = applied_identity(applied_profile)
    block: dict[str, Any] = {
        "phase": _projection.crossover_v2_phase(
            state, review_declined=v2state.review_declined(state),
        ),
        # save_v2_state stamps transitions; polls must not create a second clock (#1947).
        "updated_at": (state or {}).get("updated_at"),
        "candidate": (state or {}).get("candidate"),
        "accepted_sound_revision": (state or {}).get("accepted_sound_revision"),
        "level": seat_level_reference_status(),
        # MEASURE's own verdict-time disclosures — today just G1's ripple
        # reservation (#2087). Copied through unvalidated, exactly like
        # ``candidate`` and ``verify`` beside it: the envelope's own accessor
        # is the validating reader, so a state file written by another build
        # cannot 500 this poll path.
        "measure": (state or {}).get("measure"),
        # The coordinator owns the ordinal and adoption receipt (#2537, #2602).
        "round_receipt": (state or {}).get("round_receipt"),
        "verify": (state or {}).get("verify"),
        "execution": (state or {}).get("execution"),
        "failure": (state or {}).get("failure"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        "applied_identity": identity,
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
        "controllability": _controllability_status() if controllability is False else controllability,
    }
    block["post_apply_grade"] = v2grade.post_apply_grade(state, applied_profile=applied_profile)
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
            logger,
            "correction.controllability_ledger_unavailable",
            level=logging.WARNING,
            exc_info=True,
        )
        return None
